"""
test_bteq_extension_discovery.py — Verify .bteq and .btq are
discovered by every SHIPS stage that walks for SQL files.

Many legacy Teradata codebases name their CREATE TABLE / CREATE
VIEW scripts ``.bteq`` or ``.btq`` even when the body is pure SQL
with no BTEQ commands. Without these in the discovery allowlists
the pipeline silently skips the entire file:

    - harvest      → no objects ingested at all
    - inspect      → no rules fired against the file
    - prereq scan  → CREATE DATABASE in a .bteq missed by Phase 1
    - deploy glob  → file shipped but never executed

This test file pins the inclusion in one place so a future
'cleanup' that drops these extensions immediately fails CI.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from td_release_packager.classifier import EXTENSION_TO_EXPECTED, classify
from td_release_packager.ingest import _discover_files
from td_release_packager.validate import (
    _collect_package_prereqs,
    validate_directory,
)
from database_package_deployer.deployer import _deploy_package_impl as deploy_package


# ---------------------------------------------------------------
# Harvest discovery (ingest._discover_files)
# ---------------------------------------------------------------


class TestHarvestDiscoversBteq:
    """The harvest entry point must include .bteq and .btq files."""

    def test_bteq_file_discovered(self, tmp_path):
        f = tmp_path / "MyDB.Customer.bteq"
        f.write_text(
            "CREATE MULTISET TABLE MyDB.Customer (Id INTEGER) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        files = _discover_files(str(tmp_path))

        assert str(f) in files

    def test_btq_file_discovered(self, tmp_path):
        f = tmp_path / "MyDB.SalesView.btq"
        f.write_text(
            "CREATE VIEW MyDB.SalesView AS SELECT 1 AS dummy;",
            encoding="utf-8",
        )

        files = _discover_files(str(tmp_path))

        assert str(f) in files

    def test_bteq_alongside_other_extensions(self, tmp_path):
        """Mixed-extension trees see both the canonical and BTEQ
        files; nothing in the allowlist gets dropped."""
        (tmp_path / "MyDB.A.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.A (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )
        (tmp_path / "MyDB.B.bteq").write_text(
            "CREATE MULTISET TABLE MyDB.B (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )
        (tmp_path / "MyDB.C.btq").write_text(
            "CREATE MULTISET TABLE MyDB.C (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        files = _discover_files(str(tmp_path))

        names = {Path(f).name for f in files}
        assert names == {"MyDB.A.tbl", "MyDB.B.bteq", "MyDB.C.btq"}


# ---------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------


class TestClassifierAcceptsBteq:
    """The classifier's filename hint map must accept .bteq / .btq
    as generic — content classification wins, no spurious filename-
    mismatch warning is emitted for a CREATE TABLE in foo.bteq."""

    def test_bteq_in_extension_map(self):
        assert ".bteq" in EXTENSION_TO_EXPECTED
        assert EXTENSION_TO_EXPECTED[".bteq"] is None  # generic

    def test_btq_in_extension_map(self):
        assert ".btq" in EXTENSION_TO_EXPECTED
        assert EXTENSION_TO_EXPECTED[".btq"] is None

    def test_create_table_in_bteq_classifies_as_table(self, tmp_path):
        path = tmp_path / "MyDB.Customer.bteq"
        path.write_text(
            "CREATE MULTISET TABLE MyDB.Customer (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        result = classify(str(path), path.read_text())

        assert result.type == "TABLE"
        # No filename-mismatch warning — generic extension means
        # any DDL type is acceptable.
        mismatch_warnings = [w for w in result.warnings if "Filename mismatch" in w]
        assert mismatch_warnings == []

    def test_create_view_in_btq_classifies_as_view(self, tmp_path):
        path = tmp_path / "MyDB.SalesView.btq"
        path.write_text(
            "CREATE VIEW MyDB.SalesView AS SELECT 1;",
            encoding="utf-8",
        )

        result = classify(str(path), path.read_text())

        assert result.type == "VIEW"


# ---------------------------------------------------------------
# Inspect (validate_directory)
# ---------------------------------------------------------------


class TestInspectScansBteq:
    """validate_directory must walk .bteq / .btq files so rules
    actually fire against them."""

    def test_bteq_file_scanned(self, tmp_path):
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        # CREATE TABLE without SET/MULTISET fires set_multiset rule.
        (ddl_dir / "MyDB.T.bteq").write_text(
            "CREATE TABLE MyDB.T (Id INTEGER) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        assert result.files_scanned == 1
        assert any(i.rule == "set_multiset" for i in result.issues)

    def test_btq_file_scanned(self, tmp_path):
        ddl_dir = tmp_path / "DDL" / "views"
        ddl_dir.mkdir(parents=True)
        # REPLACE VIEW fires deploy_intent (ERROR by default).
        (ddl_dir / "MyDB.V.btq").write_text(
            "REPLACE VIEW MyDB.V AS SELECT 1;",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        assert result.files_scanned == 1
        assert any(i.rule == "deploy_intent" for i in result.issues)


# ---------------------------------------------------------------
# Prereq scan (validate._collect_package_prereqs)
# ---------------------------------------------------------------


class TestPrereqScanReadsBteq:
    """Phase 1's intra_package_dependency rule needs to see CREATE
    DATABASE statements regardless of file extension."""

    def test_create_database_in_bteq_collected(self, tmp_path):
        prereq_dir = tmp_path / "pre-requisites" / "databases"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "MyDB.bteq").write_text(
            "CREATE DATABASE MyDB AS PERMANENT = 1024;",
            encoding="utf-8",
        )

        prereqs = _collect_package_prereqs(str(tmp_path))

        assert prereqs == {"MYDB"}

    def test_create_user_in_btq_collected(self, tmp_path):
        prereq_dir = tmp_path / "pre-requisites" / "users"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "MyUser.btq").write_text(
            'CREATE USER MyUser AS PERM = 1024 PASSWORD = "x";',
            encoding="utf-8",
        )

        prereqs = _collect_package_prereqs(str(tmp_path))

        assert prereqs == {"MYUSER"}


# ---------------------------------------------------------------
# Deployer default file_patterns
# ---------------------------------------------------------------


class TestDeployerGlobsBteq:
    """deploy_package's default glob patterns must include .bteq
    and .btq so packages that ship them actually deploy."""

    def test_bteq_in_default_patterns(self):
        source = inspect.getsource(deploy_package)
        assert '"*.bteq"' in source, (
            ".bteq is missing from deploy_package's default file_patterns"
        )

    def test_btq_in_default_patterns(self):
        source = inspect.getsource(deploy_package)
        assert '"*.btq"' in source, (
            ".btq is missing from deploy_package's default file_patterns"
        )

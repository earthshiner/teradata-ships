"""
test_prereq_ordering.py — Dependency ordering for DATABASE and USER
pre-requisite files.

Teradata's ``CREATE DATABASE x FROM y`` / ``CREATE USER x FROM y``
syntax requires the parent ``y`` to exist before ``x`` can be
created. Without explicit ordering, SHIPS deploys prereqs
alphabetically — which can put a child before its parent and cause
the deploy to fail.

SHIPS harvest now extracts the ``FROM <parent>`` dependency from
every ``.db`` / ``.usr`` file and writes a ``_order.txt`` in the
``pre-requisites/`` directory that the deployer reads to enforce
the correct order.

Three layers:

  1. ``_extract_prereq_parent`` unit tests.
  2. ``_emit_prereq_order`` unit tests (topological sort).
  3. Integration through ``ingest_directory`` — confirms _order.txt
     is present and ordered correctly after harvest.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.ingest import (
    _emit_prereq_order,
    _extract_prereq_parent,
    ingest_directory,
)


# ---------------------------------------------------------------
# _extract_prereq_parent
# ---------------------------------------------------------------


class TestExtractPrereqParent:
    def test_create_database_from(self):
        ddl = "CREATE DATABASE MyDB FROM ParentDB AS PERM=0;"
        result = _extract_prereq_parent(ddl)
        assert result == ("MYDB", "PARENTDB")

    def test_create_user_from(self):
        ddl = (
            "CREATE USER PDE_D01_00_GCFR_ETL_USR FROM PDE_D01_00\n"
            "AS PASSWORD=PDE_D01_00_GCFR_ETL_USR\n   PERM=0;"
        )
        result = _extract_prereq_parent(ddl)
        assert result == ("PDE_D01_00_GCFR_ETL_USR", "PDE_D01_00")

    def test_case_insensitive(self):
        ddl = "create database child from parent as perm=0;"
        result = _extract_prereq_parent(ddl)
        assert result == ("CHILD", "PARENT")

    def test_no_from_clause(self):
        """Some DDL may lack a FROM clause — returns None."""
        ddl = "CREATE DATABASE MyDB AS PERM=0;"
        assert _extract_prereq_parent(ddl) is None

    def test_bteq_stripped_content(self):
        """Works on already-BTEQ-stripped content."""
        ddl = (
            "CREATE DATABASE PDE_D01_00_GCFR_OPR_0_M FROM PDE_D01_00_GCFR_API\n"
            "AS PERM=15e6/2*(HASHAMP()+1)\n;\n"
        )
        result = _extract_prereq_parent(ddl)
        assert result == ("PDE_D01_00_GCFR_OPR_0_M", "PDE_D01_00_GCFR_API")

    def test_token_form_extracted(self):
        """Tokenised names are compared verbatim — matching still works."""
        ddl = "CREATE DATABASE {{CHILD_DB}} FROM {{PARENT_DB}} AS PERM=0;"
        result = _extract_prereq_parent(ddl)
        assert result is not None
        name, parent = result
        assert "CHILD_DB" in name
        assert "PARENT_DB" in parent

    def test_compound_prefix_token_with_literal_suffix(self):
        """A prefix-tokenised compound name like ``{{DB_PREFIX}}_DOM_BUS_V``
        (token atom + literal suffix) must extract correctly.

        Regression for the harvest path where every ``{{PFX}}_<suffix>.db``
        was reported as UNRESOLVED with a spurious
        "no CREATE DATABASE/USER FROM <parent> clause found" warning,
        even though the FROM clause was present. Cause: the old regex
        only accepted a bare ``{{TOKEN}}`` OR a plain identifier, not a
        compound. Verified on the live YetAnotherDataProduct package.
        """
        ddl = (
            "create database {{DB_PREFIX}}_DOM_BUS_V from {{DB_PREFIX}} "
            "as perm = 0.0 spool = 4.46581964E8 fallback ;"
        )
        result = _extract_prereq_parent(ddl)
        assert result is not None
        name, parent = result
        # The compound child should round-trip — both the token atom and
        # the literal suffix are preserved (uppercased).
        assert name == "{{DB_PREFIX}}_DOM_BUS_V"
        assert parent == "{{DB_PREFIX}}"

    def test_compound_literal_prefix_token_suffix(self):
        """Compound names with a literal prefix and a token suffix
        (e.g. ``Prefix_{{TOK}}``) also match."""
        ddl = "CREATE DATABASE Prefix_{{TOK}}_suffix FROM {{PARENT}}_lit;"
        result = _extract_prereq_parent(ddl)
        assert result is not None
        name, parent = result
        assert name == "PREFIX_{{TOK}}_SUFFIX"
        assert parent == "{{PARENT}}_LIT"

    def test_lowercase_create_with_compound_token(self):
        """Lowercase ``create database`` (as emitted by some Teradata
        exports) plus a compound tokenised name still matches under the
        ``re.IGNORECASE`` flag."""
        ddl = "create database {{PFX}}_CHILD from {{PFX}} as perm = 0;"
        assert _extract_prereq_parent(ddl) == (
            "{{PFX}}_CHILD",
            "{{PFX}}",
        )


# ---------------------------------------------------------------
# _emit_prereq_order
# ---------------------------------------------------------------


def _make_prereq_dir(tmp_path: Path) -> Path:
    prereq = tmp_path / "pre-requisites"
    (prereq / "databases").mkdir(parents=True)
    (prereq / "users").mkdir(parents=True)
    return prereq


class TestEmitPrereqOrder:
    def test_simple_parent_child_ordering(self, tmp_path):
        """Child must appear AFTER parent in the ordered list."""
        prereq = _make_prereq_dir(tmp_path)
        # Write child first alphabetically to prove order is by dependency
        (prereq / "databases" / "B_CHILD.db").write_text(
            "CREATE DATABASE B_CHILD FROM A_PARENT AS PERM=0;",
            encoding="utf-8",
        )
        (prereq / "databases" / "A_PARENT.db").write_text(
            "CREATE DATABASE A_PARENT FROM DBC AS PERM=0;",
            encoding="utf-8",
        )

        r = _emit_prereq_order(str(prereq))

        parent_idx = r.ordered.index("databases/A_PARENT.db")
        child_idx = r.ordered.index("databases/B_CHILD.db")
        assert parent_idx < child_idx

    def test_three_level_chain(self, tmp_path):
        """Grandparent → parent → child ordering."""
        prereq = _make_prereq_dir(tmp_path)
        (prereq / "databases" / "C_CHILD.db").write_text(
            "CREATE DATABASE C_CHILD FROM B_PARENT AS PERM=0;", encoding="utf-8"
        )
        (prereq / "databases" / "B_PARENT.db").write_text(
            "CREATE DATABASE B_PARENT FROM A_GRANDPARENT AS PERM=0;",
            encoding="utf-8",
        )
        (prereq / "databases" / "A_GRANDPARENT.db").write_text(
            "CREATE DATABASE A_GRANDPARENT FROM DBC AS PERM=0;",
            encoding="utf-8",
        )

        r = _emit_prereq_order(str(prereq))

        assert (
            r.ordered.index("databases/A_GRANDPARENT.db")
            < r.ordered.index("databases/B_PARENT.db")
            < r.ordered.index("databases/C_CHILD.db")
        )

    def test_user_dependent_on_database(self, tmp_path):
        """A user whose parent database is in the same package: DB first."""
        prereq = _make_prereq_dir(tmp_path)
        (prereq / "databases" / "ParentDB.db").write_text(
            "CREATE DATABASE ParentDB FROM DBC AS PERM=0;", encoding="utf-8"
        )
        (prereq / "users" / "ChildUser.usr").write_text(
            "CREATE USER ChildUser FROM ParentDB AS PERM=0;",
            encoding="utf-8",
        )

        r = _emit_prereq_order(str(prereq))

        db_idx = r.ordered.index("databases/ParentDB.db")
        usr_idx = r.ordered.index("users/ChildUser.usr")
        assert db_idx < usr_idx

    def test_external_parent_not_in_package(self, tmp_path):
        """When a parent is NOT in the package (already exists on target),
        the file is still included — just with no in-package dependency
        to resolve."""
        prereq = _make_prereq_dir(tmp_path)
        (prereq / "databases" / "MyDB.db").write_text(
            "CREATE DATABASE MyDB FROM DBC AS PERM=0;", encoding="utf-8"
        )

        r = _emit_prereq_order(str(prereq))

        assert r.ordered == ["databases/MyDB.db"]

    def test_order_txt_written(self, tmp_path):
        """``_order.txt`` is written to prereq_dir root."""
        prereq = _make_prereq_dir(tmp_path)
        (prereq / "databases" / "X.db").write_text(
            "CREATE DATABASE X FROM DBC AS PERM=0;", encoding="utf-8"
        )

        _emit_prereq_order(str(prereq))

        order_file = prereq / "_order.txt"
        assert order_file.exists()
        content = order_file.read_text(encoding="utf-8")
        assert "databases/X.db" in content

    def test_order_txt_has_comment_header(self, tmp_path):
        prereq = _make_prereq_dir(tmp_path)
        (prereq / "databases" / "X.db").write_text(
            "CREATE DATABASE X FROM DBC AS PERM=0;", encoding="utf-8"
        )
        _emit_prereq_order(str(prereq))
        content = (prereq / "_order.txt").read_text(encoding="utf-8")
        assert content.startswith("#")

    def test_empty_prereq_dir_returns_empty(self, tmp_path):
        prereq = _make_prereq_dir(tmp_path)
        r = _emit_prereq_order(str(prereq))
        assert r.ordered == []
        assert r.unresolvable == []

    def test_all_files_included_in_order(self, tmp_path):
        """Every file placed is represented in the ordering."""
        prereq = _make_prereq_dir(tmp_path)
        files = {
            "databases/A.db": "CREATE DATABASE A FROM DBC AS PERM=0;",
            "databases/B.db": "CREATE DATABASE B FROM A AS PERM=0;",
            "users/U.usr": "CREATE USER U FROM B AS PERM=0;",
        }
        for rel, ddl in files.items():
            sub, name = rel.split("/")
            (prereq / sub / name).write_text(ddl, encoding="utf-8")

        r = _emit_prereq_order(str(prereq))

        assert set(r.ordered) == {"databases/A.db", "databases/B.db", "users/U.usr"}

    def test_no_from_clause_reported_as_unresolvable(self, tmp_path):
        """A file with no FROM clause is included in the ordering but
        listed as unresolvable. We cannot guarantee its position is
        correct — we warn so the DBA can verify manually."""
        prereq = _make_prereq_dir(tmp_path)
        (prereq / "databases" / "Mystery.db").write_text(
            "CREATE DATABASE Mystery AS PERM=0;",  # no FROM clause
            encoding="utf-8",
        )

        r = _emit_prereq_order(str(prereq))

        assert len(r.unresolvable) == 1
        assert "Mystery.db" in r.unresolvable[0][0]
        # Still in the ordered list so the deployer runs it.
        assert any("Mystery.db" in p for p in r.ordered)

    def test_unresolvable_warning_appears_in_order_txt(self, tmp_path):
        """The WARNING block appears in _order.txt so the DBA sees
        the caveat when they inspect the file. We cannot silently
        pretend the order is correct when we don't know."""
        prereq = _make_prereq_dir(tmp_path)
        (prereq / "databases" / "Mystery.db").write_text(
            "CREATE DATABASE Mystery AS PERM=0;",
            encoding="utf-8",
        )

        _emit_prereq_order(str(prereq))

        content = (prereq / "_order.txt").read_text(encoding="utf-8")
        assert "WARNING" in content
        assert "Mystery.db" in content


# ---------------------------------------------------------------
# Integration: ingest_directory writes _order.txt
# ---------------------------------------------------------------


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for sub in (
        "payload/database/pre-requisites/databases",
        "payload/database/pre-requisites/users",
        "payload/database/DDL/tables",
        "config/env",
    ):
        (project / sub).mkdir(parents=True, exist_ok=True)
    (project / ".build_counter").write_text("0\n", encoding="utf-8")
    return project


class TestIngestProducesPrereqOrder:
    def test_order_txt_written_after_harvest(self, tmp_path):
        """After harvesting a DATABASE file, _order.txt appears."""
        project = _make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "ParentDB.db").write_text(
            "CREATE DATABASE ParentDB FROM DBC AS PERM=0;\n",
            encoding="utf-8",
        )
        (source / "ChildDB.db").write_text(
            "CREATE DATABASE ChildDB FROM ParentDB AS PERM=0;\n",
            encoding="utf-8",
        )

        ingest_directory(str(source), str(project), detect_tokens=False)

        order_file = project / "payload" / "database" / "pre-requisites" / "_order.txt"
        assert order_file.exists()

    def test_parent_before_child_in_order_txt(self, tmp_path):
        project = _make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        # Alphabetically child (B_CHILD) would come before parent (P_PARENT)
        # but dependency-ordering must put parent first.
        (source / "P_PARENT.db").write_text(
            "CREATE DATABASE P_PARENT FROM DBC AS PERM=0;\n",
            encoding="utf-8",
        )
        (source / "B_CHILD.db").write_text(
            "CREATE DATABASE B_CHILD FROM P_PARENT AS PERM=0;\n",
            encoding="utf-8",
        )

        ingest_directory(str(source), str(project), detect_tokens=False)

        order_file = project / "payload" / "database" / "pre-requisites" / "_order.txt"
        lines = [
            line
            for line in order_file.read_text(encoding="utf-8").splitlines()
            if not line.startswith("#") and line.strip()
        ]
        parent_idx = next(i for i, line in enumerate(lines) if "P_PARENT" in line)
        child_idx = next(i for i, line in enumerate(lines) if "B_CHILD" in line)
        assert parent_idx < child_idx

    def test_no_order_txt_when_no_prereqs(self, tmp_path):
        """Packages with no DATABASE/USER files get no _order.txt
        (no need to clutter the payload)."""
        project = _make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.T (Id INT) PRIMARY INDEX (Id);\n",
            encoding="utf-8",
        )

        ingest_directory(str(source), str(project), detect_tokens=False)

        order_file = project / "payload" / "database" / "pre-requisites" / "_order.txt"
        assert not order_file.exists()

    def test_unresolvable_file_produces_harvest_warning(self, tmp_path):
        """When a DATABASE file has no FROM clause, a classification
        warning appears in the harvest result so the DBA knows the
        ordering cannot be guaranteed. Acknowledging the limit is
        more honest than silently placing the file and hoping for
        the best."""
        project = _make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "Mystery.db").write_text(
            "CREATE DATABASE Mystery AS PERM=0;\n",  # no FROM clause
            encoding="utf-8",
        )

        result = ingest_directory(str(source), str(project), detect_tokens=False)

        unresolvable_warnings = [
            w for w in result.classification_warnings if "UNRESOLVED" in w
        ]
        assert len(unresolvable_warnings) == 1
        assert "Mystery.db" in unresolvable_warnings[0]

"""
test_orphan_database.py — Project-level orphan-database detector (#475).

The detector walks ``payload/database/pre-requisites/`` and flags any
declared database the rest of the payload doesn't reference.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.orphan_database import check_orphan_databases


def _scaffold(project: Path) -> None:
    """Create a minimum payload skeleton — pre-requisites + sibling phase dirs."""
    for sub in (
        "payload/database/pre-requisites/databases",
        "payload/database/pre-requisites/users",
        "payload/database/DDL/tables",
        "payload/database/DDL/views",
        "payload/database/DCL/inter_db",
    ):
        (project / sub).mkdir(parents=True, exist_ok=True)


def _write_db(project: Path, name: str, body: str) -> None:
    """Write a ``.db`` declaration file."""
    (
        project / "payload" / "database" / "pre-requisites" / "databases" / f"{name}.db"
    ).write_text(body, encoding="utf-8")


def _write_user(project: Path, name: str, body: str) -> None:
    (
        project / "payload" / "database" / "pre-requisites" / "users" / f"{name}.usr"
    ).write_text(body, encoding="utf-8")


def _write_payload_file(project: Path, relpath: str, body: str) -> None:
    """Write any non-prereq payload file under payload/database/."""
    p = project / "payload" / "database" / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


class TestOrphanDatabaseDetection:
    def test_orphan_flagged_when_nothing_references(self, tmp_path):
        project = tmp_path / "proj"
        _scaffold(project)
        _write_db(
            project,
            "Lonely",
            "CREATE DATABASE Lonely FROM DBC AS PERM = 0;\n",
        )

        issues = check_orphan_databases(str(project))
        assert len(issues) == 1
        assert issues[0].rule == "orphan_database"
        assert "Lonely" in issues[0].message
        assert "pre-requisites/databases/Lonely.db" in issues[0].file

    def test_database_referenced_by_table_not_flagged(self, tmp_path):
        project = tmp_path / "proj"
        _scaffold(project)
        _write_db(
            project,
            "Used",
            "CREATE DATABASE Used FROM DBC AS PERM = 100M;\n",
        )
        _write_payload_file(
            project,
            "DDL/tables/Used.Customer.tbl",
            "CREATE MULTISET TABLE Used.Customer (id INTEGER) PRIMARY INDEX (id);\n",
        )

        issues = check_orphan_databases(str(project))
        assert issues == []

    def test_database_referenced_by_view_body_not_flagged(self, tmp_path):
        project = tmp_path / "proj"
        _scaffold(project)
        _write_db(
            project,
            "Source",
            "CREATE DATABASE Source FROM DBC AS PERM = 0;\n",
        )
        _write_db(
            project,
            "Sink",
            "CREATE DATABASE Sink FROM DBC AS PERM = 0;\n",
        )
        _write_payload_file(
            project,
            "DDL/views/Sink.MyView.viw",
            "REPLACE VIEW Sink.MyView AS SELECT * FROM Source.Customer;\n",
        )

        issues = check_orphan_databases(str(project))
        # Both Source (referenced via FROM) and Sink (referenced via
        # CREATE VIEW header) are referenced; neither is orphan.
        assert issues == []

    def test_database_referenced_as_grant_target_not_flagged(self, tmp_path):
        project = tmp_path / "proj"
        _scaffold(project)
        _write_db(
            project, "GranteeDb", "CREATE DATABASE GranteeDb FROM DBC AS PERM = 0;\n"
        )
        _write_payload_file(
            project,
            "DCL/inter_db/GranteeDb.grt",
            "GRANT SELECT ON Other.t TO GranteeDb;\n",
        )

        issues = check_orphan_databases(str(project))
        # GranteeDb is the GRANT...TO target, so it's referenced.
        assert issues == []

    def test_database_referenced_as_parent_of_child_not_flagged(self, tmp_path):
        project = tmp_path / "proj"
        _scaffold(project)
        _write_db(
            project,
            "Parent",
            "CREATE DATABASE Parent FROM DBC AS PERM = 100M;\n",
        )
        _write_db(
            project,
            "Child",
            "CREATE DATABASE Child FROM Parent AS PERM = 0;\n",
        )
        # Child has nothing in it, so Child is orphan, but Parent is
        # referenced (as Child's parent) so Parent is NOT orphan.
        issues = check_orphan_databases(str(project))
        rules = [(i.rule, i.file) for i in issues]
        assert (
            "orphan_database",
            "payload/database/pre-requisites/databases/Child.db",
        ) in rules
        assert not any("Parent.db" in f for _, f in rules)

    def test_users_also_scanned(self, tmp_path):
        project = tmp_path / "proj"
        _scaffold(project)
        _write_user(
            project,
            "OrphanUser",
            "CREATE USER OrphanUser FROM DBC AS PERM = 50M, PASSWORD = '...';\n",
        )

        issues = check_orphan_databases(str(project))
        assert len(issues) == 1
        assert "OrphanUser" in issues[0].message
        assert issues[0].file.endswith("OrphanUser.usr")

    def test_case_insensitive_matching(self, tmp_path):
        project = tmp_path / "proj"
        _scaffold(project)
        _write_db(
            project, "MixedCase", "CREATE DATABASE MixedCase FROM DBC AS PERM = 0;\n"
        )
        # Reference uses different case — Teradata is case-insensitive,
        # so the detector must treat them as equivalent.
        _write_payload_file(
            project,
            "DDL/tables/x.tbl",
            "CREATE MULTISET TABLE MIXEDCASE.foo (id INTEGER) PRIMARY INDEX (id);\n",
        )

        issues = check_orphan_databases(str(project))
        assert issues == []

    def test_tokenised_name_matched_via_normalisation(self, tmp_path):
        """Tokenised database names match across declaration and reference.

        The user's CustomerDNA project uses ``{{DB_PREFIX}}_DOM_STD_V`` as
        both the declared name and the view-body reference. The detector
        normalises by upper-casing and stripping quotes but keeps the
        ``{{TOKEN}}_suffix`` shape intact for matching.
        """
        project = tmp_path / "proj"
        _scaffold(project)
        _write_db(
            project,
            "{{DB_PREFIX}}_DOM_STD_V",
            "CREATE DATABASE {{DB_PREFIX}}_DOM_STD_V FROM DBC AS PERM = 0;\n",
        )
        _write_payload_file(
            project,
            "DDL/views/{{DB_PREFIX}}_DOM_STD_V.foo.viw",
            "REPLACE VIEW {{DB_PREFIX}}_DOM_STD_V.foo AS SELECT 1;\n",
        )

        issues = check_orphan_databases(str(project))
        assert issues == []

    def test_competing_naming_conventions_flags_unreferenced_one(self, tmp_path):
        """The real reporting-user scenario: ``_DOM_STD_V`` referenced by
        views, ``_Domain_STD_V`` declared but orphan."""
        project = tmp_path / "proj"
        _scaffold(project)
        _write_db(
            project,
            "{{DB_PREFIX}}_DOM_STD_V",
            "CREATE DATABASE {{DB_PREFIX}}_DOM_STD_V FROM DBC AS PERM = 0;\n",
        )
        _write_db(
            project,
            "{{DB_PREFIX}}_Domain_STD_V",
            "CREATE DATABASE {{DB_PREFIX}}_Domain_STD_V FROM DBC AS PERM = 0;\n",
        )
        # Views reference only the abbreviated form.
        _write_payload_file(
            project,
            "DDL/views/{{DB_PREFIX}}_DOM_STD_V.booking.viw",
            "REPLACE VIEW {{DB_PREFIX}}_DOM_STD_V.booking AS\n"
            "SELECT * FROM {{DB_PREFIX}}_DOM_STD_T.booking;\n",
        )

        issues = check_orphan_databases(str(project))
        names = {Path(i.file).name for i in issues}
        # The orphan is the un-referenced full-name database.
        assert "{{DB_PREFIX}}_Domain_STD_V.db" in names
        # The referenced (abbreviated) one is not flagged.
        assert "{{DB_PREFIX}}_DOM_STD_V.db" not in names
        # And the _T database the view selects from doesn't exist as a
        # declaration, so we don't generate spurious findings about it.

    def test_reference_inside_comment_does_not_count(self, tmp_path):
        """A database name mentioned only in a ``-- comment`` is still orphan."""
        project = tmp_path / "proj"
        _scaffold(project)
        _write_db(
            project,
            "Lonely",
            "CREATE DATABASE Lonely FROM DBC AS PERM = 0;\n",
        )
        _write_payload_file(
            project,
            "DDL/tables/Other.t.tbl",
            "-- TODO: maybe move this to Lonely.t later\n"
            "CREATE MULTISET TABLE Other.t (id INTEGER) PRIMARY INDEX (id);\n",
        )

        issues = check_orphan_databases(str(project))
        # Lonely is mentioned only in a comment, so it's still orphan.
        assert len(issues) == 1
        assert "Lonely" in issues[0].message

    def test_severity_threaded_through(self, tmp_path):
        project = tmp_path / "proj"
        _scaffold(project)
        _write_db(project, "X", "CREATE DATABASE X FROM DBC AS PERM = 0;\n")

        for sev in ("ERROR", "WARNING", "INFO"):
            issues = check_orphan_databases(str(project), severity=sev)
            assert len(issues) == 1
            assert issues[0].severity == sev

    def test_no_prereq_tree_returns_no_findings(self, tmp_path):
        project = tmp_path / "proj"
        (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
        # No pre-requisites/ tree at all.
        issues = check_orphan_databases(str(project))
        assert issues == []

"""
test_inspect_pr5_rules.py — PR-5 Inspect rules for issue #365.

Three new rules cover the tokenised filename eponymy invariant plus
a separate Teradata best-practice check on grant granularity:

  * ``filename_token_format`` — malformed ``{{...}}`` markers in
    filenames. ERROR.
  * ``eponymous`` (extended) — filename↔body identity match now
    works on tokenised payloads via the canonical
    ``derive_filename``.
  * ``object_level_grant`` — WARN on ``ON db.obj`` or column-list
    GRANT/REVOKE statements. Best practice is to grant at the
    database level.

Tests are unit-level on the individual ``_check_*`` functions plus
one integration probe per rule via ``validate_directory``.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.validate import (
    _check_eponymous,
    _check_filename_token_format,
    _check_object_level_grant,
    validate_directory,
)


# ===========================================================================
# filename_token_format
# ===========================================================================


def test_filename_token_format_flags_orphan_open_brace() -> None:
    issues = _check_filename_token_format(
        "DDL/tables/{{DB_PREFIX}_T.X.tbl",
        "DDL/tables/{{DB_PREFIX}_T.X.tbl",
    )
    assert len(issues) >= 1
    assert all(i.rule == "filename_token_format" for i in issues)
    assert all(i.severity == "ERROR" for i in issues)


def test_filename_token_format_passes_clean_tokenised_name() -> None:
    issues = _check_filename_token_format(
        "DDL/tables/{{DB_PREFIX}}_T.X.tbl",
        "DDL/tables/{{DB_PREFIX}}_T.X.tbl",
    )
    assert issues == []


def test_filename_token_format_passes_literal_name() -> None:
    issues = _check_filename_token_format(
        "DDL/tables/PRD_T.Customer.tbl",
        "DDL/tables/PRD_T.Customer.tbl",
    )
    assert issues == []


# ===========================================================================
# eponymous (extended for tokenised payloads)
# ===========================================================================


def test_eponymous_matching_tokenised_name_and_body_passes() -> None:
    body = "CREATE TABLE {{DB_PREFIX}}_T.Customer (Id INTEGER);\n"
    issues = _check_eponymous(
        "DDL/tables/{{DB_PREFIX}}_T.Customer.tbl",
        body,
        "DDL/tables/{{DB_PREFIX}}_T.Customer.tbl",
    )
    assert issues == []


def test_eponymous_tokenised_name_body_drift_flagged() -> None:
    # Body says Customer; filename says Account. Real drift, must surface.
    body = "CREATE TABLE {{DB_PREFIX}}_T.Customer (Id INTEGER);\n"
    issues = _check_eponymous(
        "DDL/tables/{{DB_PREFIX}}_T.Account.tbl",
        body,
        "DDL/tables/{{DB_PREFIX}}_T.Account.tbl",
    )
    assert len(issues) == 1
    assert issues[0].rule == "eponymous"
    assert "Customer" in issues[0].message
    assert "Account" in issues[0].message


def test_eponymous_whole_name_token_unqualified_passes() -> None:
    body = "CREATE DATABASE {{BASE_NODE}} FROM DATAPRODUCTS;\n"
    # CREATE DATABASE is unqualified — the qualified-name extractor
    # doesn't match it, so the rule short-circuits to no-finding.
    issues = _check_eponymous(
        "pre-requisites/databases/{{BASE_NODE}}.db",
        body,
        "pre-requisites/databases/{{BASE_NODE}}.db",
    )
    assert issues == []


# ===========================================================================
# object_level_grant
# ===========================================================================


def test_object_level_grant_db_level_passes() -> None:
    body = "GRANT SELECT ON {{DOM_STD_T}} TO {{DOM_STD_V}} WITH GRANT OPTION;\n"
    issues = _check_object_level_grant("DCL/inter_db/{{DOM_STD_T}}.dcl", body)
    assert issues == []


def test_object_level_grant_object_level_warns() -> None:
    body = "GRANT SELECT ON {{DOM_STD_T}}.Customer TO {{DOM_STD_V}};\n"
    issues = _check_object_level_grant("DCL/inter_db/{{DOM_STD_T}}.dcl", body)
    assert len(issues) == 1
    assert issues[0].rule == "object_level_grant"
    assert issues[0].severity == "WARNING"
    assert "Object-level" in issues[0].message


def test_object_level_grant_column_level_warns() -> None:
    body = "GRANT SELECT (Customer_Id) ON {{DOM_STD_T}} TO {{DOM_STD_V}};\n"
    issues = _check_object_level_grant("DCL/inter_db/{{DOM_STD_T}}.dcl", body)
    assert any(i.rule == "object_level_grant" for i in issues)
    assert any("Column-level" in i.message for i in issues)


def test_object_level_grant_skips_ddl_files() -> None:
    # The rule should NOT fire on a procedure body that happens to
    # contain a GRANT statement — DDL discipline differs.
    body = (
        "CREATE PROCEDURE db.foo() BEGIN\n"
        "  CALL DBC.SYSEXECSQL('GRANT SELECT ON db.t TO u');\n"
        "END;\n"
    )
    issues = _check_object_level_grant("DDL/procedures/db.foo.spl", body)
    assert issues == []


# ===========================================================================
# Integration via validate_directory
# ===========================================================================


def _scaffold_payload(root: Path) -> Path:
    payload = root / "payload" / "database"
    for sub in [
        "DDL/tables",
        "DDL/views",
        "DCL/inter_db",
        "pre-requisites/databases",
    ]:
        (payload / sub).mkdir(parents=True, exist_ok=True)
    return root


def test_integration_filename_token_format_surfaced(tmp_path: Path) -> None:
    project = _scaffold_payload(tmp_path / "project")
    # Orphan closing brace in the filename — malformed.
    bad = project / "payload/database/DDL/tables" / "{{DB_PREFIX}}_T.Customer}}.tbl"
    bad.write_text(
        "CREATE TABLE {{DB_PREFIX}}_T.Customer (Id INTEGER);\n",
        encoding="utf-8",
    )
    result = validate_directory(str(project))
    codes = {i.rule for i in result.issues}
    assert "filename_token_format" in codes


def test_integration_object_level_grant_surfaced(tmp_path: Path) -> None:
    project = _scaffold_payload(tmp_path / "project")
    (project / "payload/database/DCL/inter_db" / "{{DOM_STD_T}}.dcl").write_text(
        "GRANT SELECT ON {{DOM_STD_T}}.Customer TO {{DOM_STD_V}};\n",
        encoding="utf-8",
    )
    result = validate_directory(str(project))
    object_findings = [i for i in result.issues if i.rule == "object_level_grant"]
    assert object_findings, "Expected at least one object_level_grant finding"
    assert object_findings[0].severity == "WARNING"


def test_integration_clean_payload_has_no_pr5_findings(tmp_path: Path) -> None:
    project = _scaffold_payload(tmp_path / "project")
    (
        project / "payload/database/DDL/tables" / "{{DB_PREFIX}}_T.Customer.tbl"
    ).write_text(
        "CREATE TABLE {{DB_PREFIX}}_T.Customer (Id INTEGER);\n",
        encoding="utf-8",
    )
    (project / "payload/database/DCL/inter_db" / "{{DB_PREFIX}}_T.dcl").write_text(
        "GRANT SELECT ON {{DB_PREFIX}}_T TO {{DB_PREFIX}}_V WITH GRANT OPTION;\n",
        encoding="utf-8",
    )
    result = validate_directory(str(project))
    pr5_codes = {"filename_token_format", "object_level_grant"}
    pr5_findings = [i for i in result.issues if i.rule in pr5_codes]
    assert pr5_findings == [], (
        f"Clean payload should produce no PR-5 findings, got: {pr5_findings}"
    )

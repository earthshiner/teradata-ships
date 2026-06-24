"""
test_dcl_discipline.py — G5 + G6 golden-regression for issue #365 (handover §7).

G5 — DCL discipline:
  * Multiple grants ON one database → exactly one ``.dcl`` file
    named after the ON-object.
  * Compatible privileges (same action / ON-object / grantee /
    WITH GRANT OPTION) merge into a single comma-separated statement.
  * ``GRANT`` and ``REVOKE`` never merge.
  * Privilege-grants and role-grants never merge (different shape).
  * Statement order is deterministic across runs.

G6 — extension normalisation:
  * Source ``.grt`` files are harvested to ``.dcl``.
  * No ``.grt`` file survives anywhere under ``payload/``.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.grant_merger import (
    PrivilegeGrant,
    RoleGrant,
    merge_statements,
)
from td_release_packager.ingest import ingest_directory
from td_release_packager.view_layer_generator import run as generate_run


# ===========================================================================
# G5 — DCL grouping + privilege merge
# ===========================================================================


def _scaffold_ships_project(root: Path) -> Path:
    for sub in [
        "payload/database/DDL/tables",
        "payload/database/DDL/views",
        "payload/database/pre-requisites/databases",
        "payload/database/DCL/inter_db",
        "payload/database/DCL/roles",
    ]:
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    (root / ".ships").mkdir(parents=True, exist_ok=True)
    (root / ".ships" / ".build_counter").write_text("0", encoding="utf-8")
    return root


def test_g5_multiple_grants_on_one_db_collapse_to_one_dcl_file(tmp_path: Path) -> None:
    """Three GRANT statements ON the same database → one .dcl file
    named after that ON-object."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "grants.grt").write_text(
        "GRANT SELECT ON CallCentre_DOM_STD_T TO {{DOM_STD_V}} WITH GRANT OPTION;\n"
        "GRANT SELECT ON CallCentre_DOM_STD_T TO {{SEM_STD_V}} WITH GRANT OPTION;\n"
        "GRANT INSERT ON CallCentre_DOM_STD_T TO {{DOM_STD_V}} WITH GRANT OPTION;\n",
        encoding="utf-8",
    )
    project = _scaffold_ships_project(tmp_path / "project")
    result = ingest_directory(str(source), str(project), detect_tokens=False)
    assert result.errors == []

    dcl_dir = project / "payload" / "database" / "DCL" / "inter_db"
    dcl_files = list(dcl_dir.glob("*.dcl"))
    assert len(dcl_files) == 1, (
        f"Expected exactly one .dcl file (grouped by ON-object), got "
        f"{[p.name for p in dcl_files]}"
    )
    # Filename carries the ON-object literal.
    assert dcl_files[0].name == "CallCentre_DOM_STD_T.dcl"


def test_g5_compatible_privileges_merge() -> None:
    """Two grants with matching (action, on-object, grantee, with-grant)
    merge into one statement with a sorted, comma-separated privilege
    list. Unit-level check on the canonical merger."""
    statements = [
        PrivilegeGrant("GRANT", ("SELECT",), "{{DOM_T}}", "{{DOM_V}}", True),
        PrivilegeGrant("GRANT", ("INSERT",), "{{DOM_T}}", "{{DOM_V}}", True),
    ]
    merged = merge_statements(statements)
    assert len(merged) == 1
    assert merged[0].privileges == ("INSERT", "SELECT")


def test_g5_grant_and_revoke_never_merge() -> None:
    statements = [
        PrivilegeGrant("GRANT", ("SELECT",), "{{X}}", "{{Y}}", True),
        PrivilegeGrant("REVOKE", ("SELECT",), "{{X}}", "{{Y}}", True),
    ]
    assert len(merge_statements(statements)) == 2


def test_g5_role_grant_and_privilege_grant_never_merge() -> None:
    statements = [
        PrivilegeGrant("GRANT", ("SELECT",), "{{X}}", "{{Y}}", True),
        RoleGrant("GRANT", "{{some_role}}", "{{Y}}", False),
    ]
    out = merge_statements(statements)
    assert len(out) == 2


def test_g5_deterministic_statement_order_across_runs() -> None:
    """Same input multiset → byte-identical output, independent of
    insertion order. Locks the determinism property at the merger."""
    a = PrivilegeGrant("GRANT", ("SELECT",), "B", "U", True)
    b = PrivilegeGrant("GRANT", ("SELECT",), "A", "U", True)
    c = PrivilegeGrant("REVOKE", ("INSERT",), "A", "U", False)
    assert (
        merge_statements([a, b, c])
        == merge_statements([c, a, b])
        == merge_statements([b, c, a])
    )


def test_g5_generate_emits_dcl_grouped_by_on_object(tmp_path: Path) -> None:
    """Generate's Phase 5 emits one DCL file per ON-object (the
    grantor), not per grantee. This is the architectural inversion
    that PR-4 lands."""
    root = _scaffold_ships_project(tmp_path / "Project")
    (
        root / "payload/database/DDL/tables" / "{{DOM_DATABASE_T}}.Customer.tbl"
    ).write_text(
        "CREATE MULTISET TABLE {{DOM_DATABASE_T}}.Customer\n"
        "    ,FALLBACK\n(\n  Id INTEGER NOT NULL\n)\nPRIMARY INDEX (Id);\n",
        encoding="utf-8",
    )
    generate_run(root, requested_modules=None, dry_run=False)

    dcl_dir = root / "payload/database/DCL/inter_db"
    files = {p.name for p in dcl_dir.glob("*.dcl")}
    # The same-module _T → _V access grant lives in the _T database's
    # DCL file (the ON-object), not the _V's (the grantee).
    assert "{{DOM_DATABASE_T}}.dcl" in files
    assert "{{DOM_DATABASE_V}}.dcl" not in files


# ===========================================================================
# G6 — .grt source is normalised to .dcl in payload
# ===========================================================================


def test_g6_source_grt_is_harvested_to_dcl(tmp_path: Path) -> None:
    """Source ``.grt`` files are recognised as DCL by classifier and
    placed in the payload under ``.dcl``. No ``.grt`` survives."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "my_grants.grt").write_text(
        "GRANT SELECT ON CallCentre_DOM_STD_T TO {{DOM_STD_V}} WITH GRANT OPTION;\n",
        encoding="utf-8",
    )
    project = _scaffold_ships_project(tmp_path / "project")
    result = ingest_directory(str(source), str(project), detect_tokens=False)
    assert result.errors == []

    payload = project / "payload"
    grt_files = list(payload.rglob("*.grt"))
    dcl_files = list(payload.rglob("*.dcl"))
    assert grt_files == [], (
        f".grt must not survive into payload: found {[p.name for p in grt_files]}"
    )
    assert dcl_files, "Expected a .dcl file from harvested .grt source"


def test_g6_no_grt_anywhere_after_generate(tmp_path: Path) -> None:
    """Generate's output contains no ``.grt`` files — the canonical
    DCL extension everywhere is ``.dcl``."""
    root = _scaffold_ships_project(tmp_path / "Project")
    (
        root / "payload/database/DDL/tables" / "{{DOM_DATABASE_T}}.Customer.tbl"
    ).write_text(
        "CREATE MULTISET TABLE {{DOM_DATABASE_T}}.Customer\n"
        "    ,FALLBACK\n(\n  Id INTEGER NOT NULL\n)\nPRIMARY INDEX (Id);\n",
        encoding="utf-8",
    )
    generate_run(root, requested_modules=None, dry_run=False)
    assert list((root / "payload").rglob("*.grt")) == []

"""
test_generate_no_collapse.py — G4 golden-regression for issue #365.

Generate must emit tokenised-eponymous filenames from first write —
not literal-first-then-tokenised. The same canonical derive_filename
that names Harvest's atomic files names the ones Generate creates
(locking views, views databases, consolidated grants), so the same
``(qualifier, object)`` uniqueness key applies.

This probe asserts two things for every file Generate emits:

  1. The filename equals what ``derive_filename`` produces from the
     identity parsed out of the file's body. No drift between name
     and content.
  2. N tables in one tokenised database produce N distinct view
     filenames in the companion ``_V`` database — same structural
     property G1 asserts for Harvest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from td_release_packager.atomic_filename import derive_filename_from_text
from td_release_packager.view_layer_generator import run as generate_run


# A small but non-trivial set: four tables in one tokenised database
# plus one in a second module, so we exercise both the same-token
# locking-view path and a multi-database emit. If derive_filename
# ever keys on the qualifier alone, the four same-database views
# collapse.
_OBJECTS = ["Customer", "Account", "Branch", "Product"]
_CROSS_OBJECT = "Channel"


def _make_table(token: str, obj: str) -> str:
    return (
        f"CREATE MULTISET TABLE {token}.{obj}\n"
        "    ,FALLBACK\n"
        "(\n"
        f"     {obj}_Id INTEGER NOT NULL\n"
        "    ,label VARCHAR(100)\n"
        ")\n"
        f"PRIMARY INDEX ({obj}_Id)\n;\n"
    )


def _scaffold_project(root: Path) -> Path:
    for sub in [
        "payload/database/DDL/tables",
        "payload/database/DDL/views",
        "payload/database/pre-requisites/databases",
        "payload/database/DCL/inter_db",
    ]:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def generate_fixture(tmp_path: Path) -> Path:
    """N tables in {{DOM_DATABASE_T}} + 1 in {{SEM_DATABASE_T}}."""
    root = _scaffold_project(tmp_path / "Project")
    for obj in _OBJECTS:
        (
            root / "payload/database/DDL/tables" / f"{{{{DOM_DATABASE_T}}}}.{obj}.tbl"
        ).write_text(
            _make_table("{{DOM_DATABASE_T}}", obj),
            encoding="utf-8",
        )
    (
        root
        / "payload/database/DDL/tables"
        / f"{{{{SEM_DATABASE_T}}}}.{_CROSS_OBJECT}.tbl"
    ).write_text(
        _make_table("{{SEM_DATABASE_T}}", _CROSS_OBJECT),
        encoding="utf-8",
    )
    return root


def test_generate_emits_distinct_view_files_per_object(generate_fixture: Path) -> None:
    """G4 — N tables in one tokenised db → N locking views in the
    companion db. Collapse would leave fewer files."""
    result = generate_run(generate_fixture, requested_modules=None, dry_run=False)
    assert result.errors == []

    views_dir = generate_fixture / "payload/database/DDL/views"
    locking_views = sorted(views_dir.glob("{{DOM_DATABASE_V}}.*.viw"))
    assert len(locking_views) == len(_OBJECTS), (
        f"Expected {len(_OBJECTS)} locking views in DOM_DATABASE_V, got "
        f"{[p.name for p in locking_views]}"
    )

    expected_names = {
        derive_filename_from_text(f"{{{{DOM_DATABASE_V}}}}.{obj}", ".viw")
        for obj in _OBJECTS
    }
    actual_names = {p.name for p in locking_views}
    assert actual_names == expected_names


def test_generate_view_filename_matches_body_identity(generate_fixture: Path) -> None:
    """For every emitted view, the filename derived from the parsed
    body identity equals the on-disk filename. No name↔body drift."""
    generate_run(generate_fixture, requested_modules=None, dry_run=False)

    views_dir = generate_fixture / "payload/database/DDL/views"
    create_view_re = re.compile(
        r"CREATE\s+VIEW\s+(\{\{[A-Z_]+\}\}\.\w+)", re.IGNORECASE
    )

    found_any = False
    for viw in views_dir.glob("*.viw"):
        body = viw.read_text(encoding="utf-8")
        match = create_view_re.search(body)
        assert match, f"View {viw.name} has no recognisable CREATE VIEW identity"
        qualified = match.group(1)
        expected_filename = derive_filename_from_text(qualified, ".viw")
        assert viw.name == expected_filename, (
            f"Name↔body drift: file {viw.name!r} carries identity "
            f"{qualified!r} which derives filename {expected_filename!r}"
        )
        found_any = True
    assert found_any, "Fixture produced no view files to check"


def test_generate_database_files_are_tokenised_eponymous(
    generate_fixture: Path,
) -> None:
    """The views-database CREATE DATABASE files are named via
    derive_filename. Whole-name token in an unqualified slot is
    allowed (system-scope object — see issue #365 edge case)."""
    generate_run(generate_fixture, requested_modules=None, dry_run=False)

    db_dir = generate_fixture / "payload/database/pre-requisites/databases"
    db_files = sorted(db_dir.glob("*.db"))
    # Both modules trigger a views-database create.
    expected = {
        derive_filename_from_text("{{DOM_DATABASE_V}}", ".db"),
        derive_filename_from_text("{{SEM_DATABASE_V}}", ".db"),
    }
    actual = {p.name for p in db_files}
    assert expected.issubset(actual), f"Expected {expected} ⊆ {actual}"


def test_generate_grant_files_route_through_derive_filename(
    generate_fixture: Path,
) -> None:
    """Consolidated grant files are keyed on the ON-object (the
    protected database). Filename routes through derive_filename;
    canonical extension is ``.dcl`` after PR-4 (handover §7)."""
    generate_run(generate_fixture, requested_modules=None, dry_run=False)

    grants_dir = generate_fixture / "payload/database/DCL/inter_db"
    grant_files = sorted(grants_dir.glob("*.dcl"))
    # Same-module grant ON DOM_DATABASE_T to DOM_DATABASE_V lands in
    # DOM_DATABASE_T's DCL file.
    expected_dom = derive_filename_from_text("{{DOM_DATABASE_T}}", ".dcl")
    assert expected_dom in {p.name for p in grant_files}, (
        f"Expected {expected_dom!r} in {[p.name for p in grant_files]}"
    )
    # And no .grt file survives Generate.
    assert not list(grants_dir.glob("*.grt"))

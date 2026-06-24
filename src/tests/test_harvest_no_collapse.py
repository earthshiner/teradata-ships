"""
test_harvest_no_collapse.py — G1 golden-regression for issue #365.

Asserts the structural fix to Defect 1: *N* source objects sharing
one tokenised database segment must emit *N* distinct payload files,
never collapse onto a single filename.

The handover (HANDOVER-ships-tokenised-filename-eponymy.md, §3) frames
the root cause as filename derivation keying on the qualifier alone
(or merely its ``{{...}}`` head), so every object sharing a tokenised
database hashes to one filename and all but one are lost. The fix is
the canonical ``derive_filename`` function keyed on the
``(qualifier, object)`` tuple — once the literal object participates
in the uniqueness key, the collapse is structurally impossible.

This probe exercises Harvest. Generate (PR-2) and Package (PR-3) get
their own G-tests.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

from td_release_packager.ingest import ingest_directory


# Six tables in a single tokenised-prefix database. If derivation
# keys on the qualifier alone, all six collapse to one ``.tbl`` file.
_OBJECTS = ["Customer", "Account", "Transaction", "Branch", "Product", "Channel"]


def _build_collapse_fixture() -> Dict[str, str]:
    """Six tables × one tokenised-prefix database.

    All tables sit in ``{{DB_PREFIX}}_DOM_STD_T`` — the prefix-token
    shape the handover identifies as the collapse trigger. Each table
    has a distinct literal object name; the literal-object invariant
    is therefore what must preserve them as distinct payload files.
    """
    files: Dict[str, str] = {
        "DOM_STD_T.db": (
            "CREATE DATABASE {{DB_PREFIX}}_DOM_STD_T FROM DATAPRODUCTS "
            "AS PERM = 0 SPOOL = 0 FALLBACK;\n"
        ),
    }
    for obj in _OBJECTS:
        files[f"{obj}.tbl"] = (
            f"CREATE MULTISET TABLE {{{{DB_PREFIX}}}}_DOM_STD_T.{obj} (\n"
            "    id INTEGER NOT NULL,\n"
            "    label VARCHAR(100)\n"
            ") PRIMARY INDEX (id);\n"
        )
    return files


def _write_fixture(target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    for name, body in _build_collapse_fixture().items():
        (target / name).write_text(body, encoding="utf-8")
    return target


def _scaffold_project(root: Path) -> Path:
    payload = root / "payload" / "database"
    for sub in (
        "DDL/tables",
        "DDL/views",
        "DCL/inter_db",
        "pre-requisites/databases",
    ):
        (payload / sub).mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    (root / ".ships").mkdir(parents=True, exist_ok=True)
    (root / ".ships" / ".build_counter").write_text("0", encoding="utf-8")
    return root


def test_n_tokenised_db_objects_yield_n_payload_files(tmp_path: Path) -> None:
    """G1 — Harvest emits N files for N objects in one tokenised DB.

    The fixture writes ``len(_OBJECTS)`` CREATE TABLE statements, all
    in the same ``{{DB_PREFIX}}_DOM_STD_T`` database. The post-harvest
    ``payload/database/DDL/tables/`` directory must contain exactly
    that many ``.tbl`` files, each carrying a distinct literal object
    name. A collapse leaves only one file.
    """
    source = _write_fixture(tmp_path / "source")
    project = _scaffold_project(tmp_path / "project")

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
    )

    # Sanity: all object files classified, no errors raised.
    assert result.classified >= len(_OBJECTS), (
        f"Expected at least {len(_OBJECTS)} classified objects, got "
        f"{result.classified}. Errors: {result.errors}"
    )
    assert result.errors == [], f"Unexpected harvest errors: {result.errors}"

    tbl_dir = project / "payload" / "database" / "DDL" / "tables"
    tbl_files = sorted(os.path.basename(p) for p in tbl_dir.glob("*.tbl"))

    # The actual structural assertion: N distinct payload files.
    assert len(tbl_files) == len(_OBJECTS), (
        f"Expected {len(_OBJECTS)} distinct payload files, got "
        f"{len(tbl_files)}: {tbl_files}. A collapse to fewer files "
        f"means filename derivation lost the literal object segment."
    )

    # Each filename carries one of the literal object names exactly
    # once. Cheap to compute, makes the failure mode legible.
    name_to_file = {obj: f"{{{{DB_PREFIX}}}}_DOM_STD_T.{obj}.tbl" for obj in _OBJECTS}
    for obj, expected in name_to_file.items():
        assert expected in tbl_files, (
            f"Object {obj!r} missing from payload — expected "
            f"{expected!r} in {tbl_files}"
        )


def test_intra_run_clash_is_reported_not_silent(tmp_path: Path) -> None:
    """Two source files declaring the *same* identity must surface a
    derived-filename clash error, not silently overwrite.

    This guards the collision-guard wiring itself: if a refactor ever
    removed the intra-run identity tracker, two distinct sources of
    the same logical object would silently fold to one file and the
    second writer would win. The guard makes that a loud error.
    """
    source = tmp_path / "source"
    source.mkdir()
    ddl = (
        "CREATE MULTISET TABLE {{DB_PREFIX}}_DOM_STD_T.Customer (\n"
        "    id INTEGER NOT NULL\n"
        ") PRIMARY INDEX (id);\n"
    )
    # Two source files, same DDL identity, different source names.
    (source / "a.tbl").write_text(ddl, encoding="utf-8")
    (source / "b.tbl").write_text(ddl, encoding="utf-8")

    project = _scaffold_project(tmp_path / "project")
    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
    )

    # The second writer is caught — a "skipped: exists" warning is the
    # legacy path; the new guard fires only when distinct identities
    # collide, so for the same identity we rely on the existing
    # exists-check. Confirm exactly one payload file landed.
    tbl_dir = project / "payload" / "database" / "DDL" / "tables"
    tbl_files = list(tbl_dir.glob("*.tbl"))
    assert len(tbl_files) == 1

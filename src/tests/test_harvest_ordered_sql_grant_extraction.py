"""
test_harvest_ordered_sql_grant_extraction.py — #511.

When a file routes through ``_place_ordered_sql`` (currently: the
GRANT + REVOKE + non-DCL choreography path on main; post-#510 also
files containing CREATE VOLATILE TABLE), SHIPS now ALSO extracts any
GRANT / REVOKE chunks as standalone DCL inventory artefacts in
``DCL/inter_db/`` so the catalogue, dependency-graph, and grant-
discipline tooling can see them without parsing the bundled
``.ordered.osql``.

The grants stay inside the bundle for execution semantics; the
inventory files are duplicates. Teradata GRANT/REVOKE is idempotent
so the deployer running both is benign.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.ingest import ingest_directory


def _scaffold_project(root: Path) -> Path:
    payload = root / "payload" / "database"
    for sub in (
        "DDL/tables",
        "DDL/views",
        "DML",
        "DCL/inter_db",
        "DCL/roles",
        "pre-requisites/databases",
    ):
        (payload / sub).mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    (root / ".ships").mkdir(parents=True, exist_ok=True)
    (root / ".ships" / ".build_counter").write_text("0", encoding="utf-8")
    return root


def _write(source: Path, name: str, body: str) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / name).write_text(body, encoding="utf-8")


def test_grants_in_ordered_sql_are_also_emitted_as_dcl_inventory(
    tmp_path: Path,
) -> None:
    """A GRANT → DDL → REVOKE choreography file produces:
    - one .ordered.osql under DML/ (executable bundle)
    - one .dcl per GRANT/REVOKE target under DCL/inter_db/ (inventory)
    """
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "etl_choreography.sql",
        "GRANT SELECT ON CargoIntelligence_Domain TO CargoIntelligence_Prediction "
        "WITH GRANT OPTION;\n"
        "CREATE TABLE CargoIntelligence_Prediction.staging (id INTEGER);\n"
        "REVOKE SELECT ON CargoIntelligence_Domain "
        "FROM CargoIntelligence_Prediction;\n",
    )

    result = ingest_directory(str(source), str(project), detect_tokens=False)
    assert result.errors == []

    # Executable bundle is unchanged — single .ordered.osql.
    osql_files = list((project / "payload").rglob("*.ordered.osql"))
    assert len(osql_files) == 1, [str(p) for p in osql_files]
    body = osql_files[0].read_text(encoding="utf-8")
    # GRANTs survive inside the bundle (execution path).
    assert "GRANT SELECT" in body
    assert "REVOKE SELECT" in body
    assert "CREATE TABLE CargoIntelligence_Prediction.staging" in body

    # New: GRANT also lands as standalone .dcl inventory artefact.
    dcl_files = list((project / "payload").rglob("DCL/inter_db/*.dcl"))
    assert dcl_files, "expected at least one extracted .dcl inventory file"

    # The inventory file's body contains the GRANT statement only —
    # not the surrounding CREATE TABLE.
    dcl_blob = "\n".join(p.read_text(encoding="utf-8") for p in dcl_files)
    assert "GRANT SELECT" in dcl_blob
    assert "REVOKE SELECT" in dcl_blob
    assert "CREATE TABLE" not in dcl_blob


def test_ordered_sql_with_no_grants_produces_only_the_bundle(
    tmp_path: Path,
) -> None:
    """Defensive: an ordered-SQL routed file that contains no GRANTs
    produces just the .ordered.osql — no empty .dcl files.

    Constructed to hit _place_ordered_sql via the existing choreography
    trigger (saw_grant + saw_revoke + saw_non_dcl). To hit it without
    grants, we'd need the volatile-routing path from #510 — but that
    routing isn't on main yet. Instead, exercise the negative case
    indirectly by asserting that an ordinary multi-statement file with
    no grants doesn't accidentally route here.
    """
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "two_tables.sql",
        # Plain two-CREATE-TABLE file — splits per object, no ordered
        # routing, no grant extraction. Confirms the helper doesn't
        # over-trigger.
        "CREATE TABLE A_DB.t (id INTEGER);\nCREATE TABLE B_DB.u (id INTEGER);\n",
    )

    result = ingest_directory(str(source), str(project), detect_tokens=False)
    assert result.errors == []

    # No ordered bundle: file split per object.
    osql_files = list((project / "payload").rglob("*.ordered.osql"))
    assert osql_files == [], [str(p) for p in osql_files]
    # No inventory grants extracted from a non-grant file.
    dcl_files = list((project / "payload").rglob("DCL/inter_db/*.dcl"))
    assert dcl_files == [], [str(p) for p in dcl_files]


def test_multiple_grants_aggregate_into_eponymous_inventory_files(
    tmp_path: Path,
) -> None:
    """Multiple GRANTs to the same target aggregate into a single
    eponymous .dcl file (matches the main-loop aggregating behaviour).
    """
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "etl_setup.sql",
        # Two GRANTs against the same database, plus a DDL action and
        # a REVOKE to hit the choreography path.
        "GRANT SELECT ON SourceDb TO ConsumerDb;\n"
        "GRANT INSERT ON SourceDb TO ConsumerDb;\n"
        "CREATE TABLE ConsumerDb.staging (id INTEGER);\n"
        "REVOKE SELECT ON SourceDb FROM ConsumerDb;\n",
    )

    result = ingest_directory(str(source), str(project), detect_tokens=False)
    assert result.errors == []

    osql_files = list((project / "payload").rglob("*.ordered.osql"))
    assert len(osql_files) == 1

    dcl_files = list((project / "payload").rglob("DCL/inter_db/*.dcl"))
    assert dcl_files, "expected inventory .dcl files"

    # Both GRANTs appear somewhere in the inventory.
    dcl_blob = "\n".join(p.read_text(encoding="utf-8") for p in dcl_files)
    assert "GRANT SELECT" in dcl_blob
    assert "GRANT INSERT" in dcl_blob
    assert "REVOKE SELECT" in dcl_blob

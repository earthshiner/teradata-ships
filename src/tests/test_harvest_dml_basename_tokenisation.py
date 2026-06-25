"""
test_harvest_dml_basename_tokenisation.py — golden regression for
the multi-target DML / ordered-SQL filename tokenisation gap (the
P2 half of the apply_tokens filename eponymy bug).

These two helpers compose their destination filename from the
literal source basename rather than from a parsed object identity,
because statement-order is the artefact's identity (FK ordering,
sequenced operations, GRANT → action → REVOKE choreography).
Historically that meant the basename stayed literal in every
mode — including ``prefix_tokens``, which is otherwise the
reference path for tokenised filenames. The fix threads
``prefix_tokens`` through and applies both maps to the basename
before composing the final filename, so name and body land in the
same tokenisation state.
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


def _multi_target_dml(prefix: str) -> str:
    """A DML script touching two distinct tables. The pre-pass counts
    distinct targets; two or more keeps the source as
    ``<basename>.multi_table.dml``."""
    return (
        f"INSERT INTO {prefix}_DOM_STD_T.booking (id) VALUES (1);\n"
        f"INSERT INTO {prefix}_SEM_STD_T.value_domain (id) VALUES (2);\n"
    )


def _ordered_sql_script(prefix: str) -> str:
    """A GRANT → DDL → REVOKE script that hits the ordered-SQL path
    (saw_grant + saw_revoke + saw_non_dcl)."""
    return (
        f"GRANT SELECT ON {prefix}_DOM_STD_T TO some_user;\n"
        f"CREATE TABLE {prefix}_DOM_STD_T.booking (id INTEGER);\n"
        f"REVOKE SELECT ON {prefix}_DOM_STD_T FROM some_user;\n"
    )


# ===========================================================================
# Case 10 (handover) — apply_tokens tokenises the multi-target DML basename
# ===========================================================================


def test_apply_tokens_tokenises_multi_target_dml_basename(tmp_path: Path) -> None:
    """A multi-target DML source named ``CustomerDNA_OBS_load.sql``
    must land as ``{{...}}_OBS_load.multi_table.dml`` (db-name
    segment of the basename tokenised) when ``apply_tokens`` is in
    play."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "CustomerDNA_OBS_load.sql",
        _multi_target_dml("CustomerDNA"),
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        apply_tokens={
            "CustomerDNA_DOM_STD_T": "{{DOM_STD_T}}",
            "CustomerDNA_SEM_STD_T": "{{SEM_STD_T}}",
            "CustomerDNA": "{{DB_PREFIX}}",
        },
    )
    assert result.errors == []

    dml_files = [p.name for p in (project / "payload").rglob("*.multi_table.dml")]
    assert dml_files == ["{{DB_PREFIX}}_OBS_load.multi_table.dml"], dml_files


def test_prefix_tokens_tokenises_multi_target_dml_basename(tmp_path: Path) -> None:
    """The same fix applies under ``prefix_tokens``: the helper used
    to leave the basename literal in every mode."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "CustomerDNA_OBS_load.sql",
        _multi_target_dml("CustomerDNA"),
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        prefix_tokens={"CustomerDNA": "DB_PREFIX"},
    )
    assert result.errors == []

    dml_files = [p.name for p in (project / "payload").rglob("*.multi_table.dml")]
    assert dml_files == ["{{DB_PREFIX}}_OBS_load.multi_table.dml"], dml_files


def test_apply_tokens_longest_first_avoids_partial_shadowing(tmp_path: Path) -> None:
    """A map containing both ``CallCentre`` and ``CallCentre_DOM_STD_T``
    must consume the longer literal first. A shorter-first pass would
    leave ``CallCentre_DOM_STD_T_load`` partially substituted to
    ``{{P}}_DOM_STD_T_load`` instead of ``{{DOM_STD_T}}_load``."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "CallCentre_DOM_STD_T_load.sql",
        _multi_target_dml("CallCentre"),
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        apply_tokens={
            "CallCentre_DOM_STD_T": "{{DOM_STD_T}}",
            "CallCentre_SEM_STD_T": "{{SEM_STD_T}}",
            "CallCentre": "{{P}}",
        },
    )
    assert result.errors == []

    dml_files = [p.name for p in (project / "payload").rglob("*.multi_table.dml")]
    assert dml_files == ["{{DOM_STD_T}}_load.multi_table.dml"], dml_files


# ===========================================================================
# Regression — no-flags path leaves the basename literal
# ===========================================================================


def test_no_flags_leaves_multi_target_dml_basename_literal(tmp_path: Path) -> None:
    """Without ``prefix_tokens`` or ``apply_tokens``, the basename
    must remain literal — the helper used to behave this way and the
    fix must not regress it (a literal source is the operator's
    intent in this configuration)."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "CustomerDNA_OBS_load.sql",
        _multi_target_dml("CustomerDNA"),
    )

    result = ingest_directory(str(source), str(project), detect_tokens=False)
    assert result.errors == []

    dml_files = [p.name for p in (project / "payload").rglob("*.multi_table.dml")]
    assert dml_files == ["CustomerDNA_OBS_load.multi_table.dml"], dml_files


def test_unmapped_literal_in_multi_target_dml_basename_stays_literal(
    tmp_path: Path,
) -> None:
    """If the literal db name in the basename is NOT covered by the
    map, the basename stays literal — same posture as the parsed-
    identity P1 path (#374 case 9)."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "Unmapped_OBS_load.sql",
        _multi_target_dml("CustomerDNA"),
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        apply_tokens={"CustomerDNA_DOM_STD_T": "{{DOM_STD_T}}"},
    )
    assert result.errors == []

    dml_files = [p.name for p in (project / "payload").rglob("*.multi_table.dml")]
    assert dml_files == ["Unmapped_OBS_load.multi_table.dml"], dml_files


# ===========================================================================
# Ordered SQL helper — same fix
# ===========================================================================


def test_apply_tokens_tokenises_ordered_sql_basename(tmp_path: Path) -> None:
    """The choreography (GRANT → DDL → REVOKE) script lands as
    ``<basename>.ordered.osql``. After P2 the basename's db-name
    segment is tokenised in lockstep with the body."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "CustomerDNA_DOM_choreography.sql",
        _ordered_sql_script("CustomerDNA"),
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        apply_tokens={
            "CustomerDNA_DOM_STD_T": "{{DOM_STD_T}}",
            "CustomerDNA": "{{DB_PREFIX}}",
        },
    )
    assert result.errors == []

    osql_files = [p.name for p in (project / "payload").rglob("*.ordered.osql")]
    assert osql_files == ["{{DB_PREFIX}}_DOM_choreography.ordered.osql"], osql_files


def test_prefix_tokens_tokenises_ordered_sql_basename(tmp_path: Path) -> None:
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "CustomerDNA_DOM_choreography.sql",
        _ordered_sql_script("CustomerDNA"),
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        prefix_tokens={"CustomerDNA": "DB_PREFIX"},
    )
    assert result.errors == []

    osql_files = [p.name for p in (project / "payload").rglob("*.ordered.osql")]
    assert osql_files == ["{{DB_PREFIX}}_DOM_choreography.ordered.osql"], osql_files


def test_no_flags_leaves_ordered_sql_basename_literal(tmp_path: Path) -> None:
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "CustomerDNA_DOM_choreography.sql",
        _ordered_sql_script("CustomerDNA"),
    )

    result = ingest_directory(str(source), str(project), detect_tokens=False)
    assert result.errors == []

    osql_files = [p.name for p in (project / "payload").rglob("*.ordered.osql")]
    assert osql_files == ["CustomerDNA_DOM_choreography.ordered.osql"], osql_files

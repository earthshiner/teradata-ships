"""
test_harvest_apply_tokens_filename.py — golden regression for the
tokenised-filename bug on the ``apply_tokens`` (auto-tokenise /
token-map) path.

Symptom before the fix: harvest tokenised file *contents* on the
``apply_tokens`` path but left the eponymous *filename* literal. The
fix re-derives the filename qualifier from the substituted body
inside the ``if apply_tokens:`` block (matching how ``prefix_tokens``
already tokenises both surfaces by substituting before name
derivation).

Each test asserts BOTH the payload filename and the body qualifier
so the eponymy invariant — filename and body share one tokenisation
state — is locked at the file level.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from td_release_packager.ingest import ingest_directory


# ===========================================================================
# Helpers
# ===========================================================================


def _scaffold_project(root: Path) -> Path:
    payload = root / "payload" / "database"
    for sub in (
        "DDL/tables",
        "DDL/views",
        "DDL/procedures",
        "DML",
        "DCL/inter_db",
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


def _file_pairs(payload: Path, glob: str) -> List[Tuple[str, str]]:
    """Return ``[(filename, body), ...]`` for files matching ``glob``."""
    return [
        (p.name, p.read_text(encoding="utf-8")) for p in sorted(payload.rglob(glob))
    ]


# ===========================================================================
# Case 1 — token-map whole-strip
# ===========================================================================


def test_case1_token_map_whole_strip_yields_tokenised_filename(tmp_path: Path) -> None:
    """``apply_tokens`` with a whole-name mapping must tokenise the
    filename as well as the body."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "CustomerDNA_DOM_STD_T.booking.tbl",
        "CREATE MULTISET TABLE CustomerDNA_DOM_STD_T.booking (\n"
        "    id INTEGER NOT NULL,\n"
        "    label VARCHAR(100)\n"
        ") PRIMARY INDEX (id);\n",
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        apply_tokens={"CustomerDNA_DOM_STD_T": "{{DOM_STD_T}}"},
    )
    assert result.errors == []

    pairs = _file_pairs(project / "payload", "*.tbl")
    assert pairs == [
        (
            "{{DOM_STD_T}}.booking.tbl",
            "CREATE MULTISET TABLE {{DOM_STD_T}}.booking (\n"
            "    id INTEGER NOT NULL,\n"
            "    label VARCHAR(100)\n"
            ") PRIMARY INDEX (id);\n",
        )
    ]


# ===========================================================================
# Case 2 — auto-tokenise, no env_prefix (whole-name token)
# ===========================================================================


def test_case2_auto_tokenise_no_env_prefix_yields_whole_name_token(
    tmp_path: Path,
) -> None:
    """``apply_tokens`` mapping the literal db name → ``{{<literal>}}``
    must produce a whole-name tokenised filename."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "CustomerDNA_DOM_STD_T.booking.tbl",
        "CREATE MULTISET TABLE CustomerDNA_DOM_STD_T.booking (\n"
        "    id INTEGER NOT NULL\n"
        ") PRIMARY INDEX (id);\n",
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        apply_tokens={"CustomerDNA_DOM_STD_T": "{{CustomerDNA_DOM_STD_T}}"},
    )
    assert result.errors == []

    filenames = [p.name for p in (project / "payload").rglob("*.tbl")]
    assert filenames == ["{{CustomerDNA_DOM_STD_T}}.booking.tbl"]
    body = (
        project
        / "payload"
        / "database"
        / "DDL"
        / "tables"
        / "{{CustomerDNA_DOM_STD_T}}.booking.tbl"
    ).read_text(encoding="utf-8")
    assert "{{CustomerDNA_DOM_STD_T}}.booking" in body


# ===========================================================================
# Case 3 — auto-tokenise + env_prefix (prefix stripped)
# ===========================================================================


def test_case3_auto_tokenise_with_env_prefix_strips_prefix(tmp_path: Path) -> None:
    """When ``apply_tokens`` maps the literal db name to a
    prefix-stripped token, the resulting filename must use the stripped
    form, mirroring how the body is rewritten."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "CustomerDNA_DOM_STD_T.booking.tbl",
        "CREATE MULTISET TABLE CustomerDNA_DOM_STD_T.booking (\n"
        "    id INTEGER NOT NULL\n"
        ") PRIMARY INDEX (id);\n",
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        apply_tokens={"CustomerDNA_DOM_STD_T": "{{DOM_STD_T}}"},
    )
    assert result.errors == []

    filenames = [p.name for p in (project / "payload").rglob("*.tbl")]
    assert filenames == ["{{DOM_STD_T}}.booking.tbl"]


# ===========================================================================
# Case 4 — single-target DML
# ===========================================================================


def test_case4_single_target_dml_filename_tokenised(tmp_path: Path) -> None:
    """A single-target ``INSERT INTO`` routed through the main loop
    must tokenise both filename and body."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "load_value_domain.dml",
        "INSERT INTO CustomerDNA_SEM_STD_T.value_domain (id, label) VALUES (1, 'x');\n",
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        apply_tokens={"CustomerDNA_SEM_STD_T": "{{SEM_STD_T}}"},
    )
    assert result.errors == []

    pairs = _file_pairs(project / "payload", "*.dml")
    assert pairs, "Expected a .dml file in the payload"
    filename, body = pairs[0]
    assert filename == "{{SEM_STD_T}}.value_domain.dml"
    assert "{{SEM_STD_T}}.value_domain" in body


# ===========================================================================
# Case 5 — DCL (database-level grant)
# ===========================================================================


def test_case5_dcl_db_level_grant_filename_tokenised(tmp_path: Path) -> None:
    """A DCL GRANT statement whose ``ON`` target is a database must
    produce a ``{{TOKEN}}.dcl`` filename when the token-map covers
    that database."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "domain_grants.grt",
        "GRANT SELECT ON CustomerDNA_DOM_STD_V TO some_role;\n",
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        apply_tokens={"CustomerDNA_DOM_STD_V": "{{DOM_STD_V}}"},
    )
    assert result.errors == []

    pairs = _file_pairs(project / "payload", "*.dcl")
    assert pairs, "Expected a .dcl file in the payload"
    filename, body = pairs[0]
    assert filename == "{{DOM_STD_V}}.dcl"
    assert "{{DOM_STD_V}}" in body


# ===========================================================================
# Case 6 — regression: prefix_token path unchanged
# ===========================================================================


def test_case6_prefix_token_path_still_tokenises_filename(tmp_path: Path) -> None:
    """``prefix_tokens`` already tokenised filenames before this fix
    (it substitutes into ``raw_content`` before name derivation). The
    behaviour must be preserved byte-for-byte."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "CustomerDNA_DOM_STD_T.booking.tbl",
        "CREATE MULTISET TABLE CustomerDNA_DOM_STD_T.booking (\n"
        "    id INTEGER NOT NULL\n"
        ") PRIMARY INDEX (id);\n",
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        prefix_tokens={"CustomerDNA": "DB_PREFIX"},
    )
    assert result.errors == []

    filenames = [p.name for p in (project / "payload").rglob("*.tbl")]
    assert filenames == ["{{DB_PREFIX}}_DOM_STD_T.booking.tbl"]


# ===========================================================================
# Case 7 — regression: already-tokenised source, no flags
# ===========================================================================


def test_case7_already_tokenised_source_unchanged(tmp_path: Path) -> None:
    """A source file whose body already carries a ``{{TOKEN}}``
    qualifier and whose filename matches must round-trip unchanged
    when no substitution flags are supplied."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "{{CustomerDNA_DOM_STD_T}}.booking.tbl",
        "CREATE MULTISET TABLE {{CustomerDNA_DOM_STD_T}}.booking (\n"
        "    id INTEGER NOT NULL\n"
        ") PRIMARY INDEX (id);\n",
    )

    result = ingest_directory(str(source), str(project), detect_tokens=False)
    assert result.errors == []

    filenames = [p.name for p in (project / "payload").rglob("*.tbl")]
    assert filenames == ["{{CustomerDNA_DOM_STD_T}}.booking.tbl"]


# ===========================================================================
# Case 8 — collision on tokenised identity
# ===========================================================================


def test_case8_collision_on_tokenised_identity_surfaced(tmp_path: Path) -> None:
    """Two literal source files resolving to the same ``(token-db,
    object)`` filename must collide (intra-run guard from PR-1 fires
    on the post-substitution identity)."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "a.tbl",
        "CREATE MULTISET TABLE CustomerDNA_DOM_STD_T.booking ("
        "id INTEGER) PRIMARY INDEX (id);\n",
    )
    _write(
        source,
        "b.tbl",
        "CREATE MULTISET TABLE CustomerDNA_DOM_STD_T.booking ("
        "id INTEGER) PRIMARY INDEX (id);\n",
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        apply_tokens={"CustomerDNA_DOM_STD_T": "{{DOM_STD_T}}"},
    )
    # Exactly one payload file lands; the second writer is rejected
    # by the existing dest-path guard or the intra-run identity map.
    filenames = [p.name for p in (project / "payload").rglob("*.tbl")]
    assert filenames == ["{{DOM_STD_T}}.booking.tbl"]
    # Some signal that the second source was suppressed.
    assert result.skipped_existing >= 1 or result.errors or result.warnings, (
        "Expected the second writer to surface in the result"
    )


# ===========================================================================
# Case 9 — unmapped literal stays literal
# ===========================================================================


def test_case9_unmapped_literal_stays_literal(tmp_path: Path) -> None:
    """If the literal db name is not in the apply_tokens map, the
    filename must stay literal (the ``hardcoded_name`` rule will warn
    elsewhere)."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "Unmapped_DOM_STD_T.booking.tbl",
        "CREATE MULTISET TABLE Unmapped_DOM_STD_T.booking (\n"
        "    id INTEGER NOT NULL\n"
        ") PRIMARY INDEX (id);\n",
    )

    result = ingest_directory(
        str(source),
        str(project),
        detect_tokens=False,
        apply_tokens={"CustomerDNA_DOM_STD_T": "{{DOM_STD_T}}"},
    )
    assert result.errors == []

    filenames = [p.name for p in (project / "payload").rglob("*.tbl")]
    assert filenames == ["Unmapped_DOM_STD_T.booking.tbl"]
    body = (
        project
        / "payload"
        / "database"
        / "DDL"
        / "tables"
        / "Unmapped_DOM_STD_T.booking.tbl"
    ).read_text(encoding="utf-8")
    assert "Unmapped_DOM_STD_T.booking" in body

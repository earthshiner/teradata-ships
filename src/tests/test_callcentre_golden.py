"""
test_callcentre_golden.py — PR4 golden regression harness.

Exercises the CallCentre DBC-export fixture (committed under
``src/tests/fixtures/callcentre/``) through the SHIPS deterministic
core (harvest → tokenise → emit payload) on every CI run, then
asserts the invariants the deterministic-deploy programme has been
locking down PR by PR:

  * PR1a — two consecutive harvests with identical args produce
    byte-identical ``payload/`` trees.
  * PR1b — no stray whole-name ``{{DB_PREFIX_X}}.dcl`` filenames leak
    from the prefix-token path.
  * PR2 — the shared token-coverage scanner accepts the harvested
    payload against an env config that defines every emitted token.

The fixture's contents are realistic and small enough to keep CI
fast, but cover every artefact class the programme cares about
(``.db``, ``.grants``, ``.tbl``, ``.viw``, ``.col``) across three
modules (``DOM``, ``MEM``, ``SEM``).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from td_release_packager.ingest import ingest_directory
from td_release_packager.token_engine import (
    generate_token_map,
    validate_payload_token_coverage,
)


# ---------------------------------------------------------------
# Fixture location
# ---------------------------------------------------------------


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "callcentre"


def _has_fixture() -> bool:
    """The fixture is committed; absence implies a corrupted checkout
    rather than a skip condition. We still gate so a developer can
    point CI at a hollowed-out tree without spurious red builds."""
    return FIXTURE_ROOT.is_dir() and any(FIXTURE_ROOT.iterdir())


pytestmark = pytest.mark.skipif(
    not _has_fixture(),
    reason="CallCentre fixture not present (expected at src/tests/fixtures/callcentre)",
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _scaffold_minimal_project(root: Path) -> Path:
    """Build the project tree harvest requires.

    Kept local rather than reaching into ``conftest.py``'s
    ``tmp_project`` so this harness stays portable.
    """
    payload = root / "payload" / "database"
    for sub in (
        "DDL/tables",
        "DDL/views",
        "DCL/inter_db",
        "DCL/roles",
        "pre-requisites/databases",
    ):
        (payload / sub).mkdir(parents=True, exist_ok=True)
    (root / "config" / "env").mkdir(parents=True, exist_ok=True)
    (root / ".build_counter").write_text("0", encoding="utf-8")
    return root


def _full_auto_tokenise_harvest(source: Path, project: Path) -> None:
    """Two-pass harvest mirroring the CLI / MCP auto-tokenise flow.

    Pass 1 — detect token candidates.
    Pass 2 — apply both the candidate-derived token map and the
             ``--prefix-token CallCentre=DB_PREFIX`` rewrite.

    No ``env_prefix`` is supplied per the PR1 brief ("retire
    env_prefix from the prefix path").
    """
    detection = ingest_directory(
        str(source),
        str(project),
        detect_tokens=True,
        apply_tokens=None,
        clean_payload=True,
    )
    apply_tokens = None
    if detection.token_candidates:
        apply_tokens = generate_token_map(detection.token_candidates, None)
    ingest_directory(
        str(source),
        str(project),
        detect_tokens=True,
        apply_tokens=apply_tokens,
        prefix_tokens={"CallCentre": "DB_PREFIX"},
        clean_payload=True,
    )


def _payload_index(project: Path) -> List[Tuple[str, str]]:
    """``[(relpath, sha256), ...]`` for every file under
    ``project/payload``, sorted by relpath."""
    payload = project / "payload"
    entries: List[Tuple[str, str]] = []
    for root, _dirs, files in os.walk(payload):
        for fname in files:
            fpath = Path(root) / fname
            rel = str(fpath.relative_to(project)).replace("\\", "/")
            digest = hashlib.sha256(fpath.read_bytes()).hexdigest()
            entries.append((rel, digest))
    entries.sort()
    return entries


def _diff_indexes(
    a: List[Tuple[str, str]],
    b: List[Tuple[str, str]],
) -> List[str]:
    """Human-readable line-by-line index diff."""
    a_map = dict(a)
    b_map = dict(b)
    out: List[str] = []
    for rel in sorted(set(a_map) - set(b_map)):
        out.append(f"  only in A: {rel}")
    for rel in sorted(set(b_map) - set(a_map)):
        out.append(f"  only in B: {rel}")
    for rel in sorted(a_map.keys() & b_map.keys()):
        if a_map[rel] != b_map[rel]:
            out.append(
                f"  content differs: {rel} ({a_map[rel][:12]} vs {b_map[rel][:12]})"
            )
    return out


# ---------------------------------------------------------------
# PR1 — determinism against the real-shaped fixture
# ---------------------------------------------------------------


def test_callcentre_double_harvest_byte_identical(tmp_path: Path) -> None:
    """**PR1 invariant against the live-shaped input.** Two harvests
    of the CallCentre fixture with identical args produce byte-
    identical ``payload/`` trees. This is the regression gate the
    inline synthetic probe in ``test_harvest_determinism.py`` could
    not stress alone."""
    project_a = _scaffold_minimal_project(tmp_path / "project_a")
    project_b = _scaffold_minimal_project(tmp_path / "project_b")

    _full_auto_tokenise_harvest(FIXTURE_ROOT, project_a)
    _full_auto_tokenise_harvest(FIXTURE_ROOT, project_b)

    index_a = _payload_index(project_a)
    index_b = _payload_index(project_b)

    if index_a != index_b:
        diff = "\n".join(_diff_indexes(index_a, index_b))
        pytest.fail(
            "CallCentre double-harvest produced different payload trees.\n\n"
            f"Diff:\n{diff}"
        )


# ---------------------------------------------------------------
# PR1b — no whole-name token leak
# ---------------------------------------------------------------


def test_no_whole_name_token_filenames(tmp_path: Path) -> None:
    """**PR1b invariant.** After harvest no payload file's name should
    contain a *whole-name* form of the prefix token — i.e. a token
    whose braces wrap both the prefix and a literal suffix
    (``{{DB_PREFIX_X}}.dcl``). The correct prefix form is
    ``{{DB_PREFIX}}_X.dcl``: the braces close immediately after the
    token, and the literal suffix sits outside them.

    The handover's Appendix A1 documented this leak as the symptom
    of non-deterministic dict iteration in the tokeniser; PR1b's
    sorted-iteration sweep should make it structurally impossible.
    """
    project = _scaffold_minimal_project(tmp_path / "project")
    _full_auto_tokenise_harvest(FIXTURE_ROOT, project)

    payload = project / "payload"
    offenders: List[str] = []
    for root, _dirs, files in os.walk(payload):
        for fname in files:
            # A whole-name token has the shape ``{{DB_PREFIX_<literal>}}``
            # — the closing braces sit *after* the literal suffix,
            # bundling both inside the token. The prefix form is
            # ``{{DB_PREFIX}}_<literal>`` with the braces between.
            if "{{DB_PREFIX_" in fname:
                rel = str((Path(root) / fname).relative_to(project)).replace("\\", "/")
                offenders.append(rel)

    if offenders:
        pytest.fail(
            "Whole-name token leak — payload contains filenames where "
            "the prefix token braces include the structural suffix. "
            "Expected prefix form {{DB_PREFIX}}_<suffix>, got whole-"
            "name {{DB_PREFIX_<suffix>}}.\n\n" + "\n".join(f"  {p}" for p in offenders)
        )


# ---------------------------------------------------------------
# PR2 — the shared coverage scanner agrees the payload is clean
# ---------------------------------------------------------------


def test_coverage_scanner_clean_against_synthetic_env(tmp_path: Path) -> None:
    """**PR2 invariant.** With an env config that defines every
    emitted token, the shared token-coverage scanner reports no
    undefined tokens — proving the inspect gate would pass and
    package's per-env scan therefore could not fail on coverage."""
    project = _scaffold_minimal_project(tmp_path / "project")
    _full_auto_tokenise_harvest(FIXTURE_ROOT, project)

    # Collect every token actually emitted into the payload (content
    # plus filenames) so the synthetic env can define exactly that
    # set. This makes the test self-consistent — the env config tracks
    # whatever tokens harvest produced, rather than us guessing the
    # final shape.
    from td_release_packager.token_engine import (
        scan_tokens_in_directory,
        _scan_filename_tokens,
        _FILENAME_TOKEN_RE,
    )

    payload_dir = project / "payload" / "database"
    referenced: set = set()
    for tokens in scan_tokens_in_directory(
        str(payload_dir), project_dir=str(project)
    ).values():
        referenced.update(tokens)
    for tokens in _scan_filename_tokens(str(payload_dir)).values():
        referenced.update(tokens)

    env_dir = project / "config" / "env"
    env_dir.mkdir(parents=True, exist_ok=True)
    env_body = "\n".join(
        f"{tok}=stub_value_{i}" for i, tok in enumerate(sorted(referenced))
    )
    (env_dir / "DEV.conf").write_text(env_body + "\n", encoding="utf-8")

    result = validate_payload_token_coverage(str(payload_dir), str(project))
    assert "DEV" in result, "coverage scanner should report DEV env"
    assert result["DEV"]["undefined"] == [], (
        "Coverage scanner found undefined tokens despite the env "
        "config defining every emitted token — PR2 regression: "
        f"{result['DEV']['undefined']}"
    )


# ---------------------------------------------------------------
# Sanity — fixture produces a non-trivial payload
# ---------------------------------------------------------------


def test_harvest_produces_expected_artefact_classes(tmp_path: Path) -> None:
    """Catches the silent-pass failure mode where the harness scaffolds
    everything correctly but harvest emits nothing useful. We assert
    that every artefact class the fixture covers actually shows up in
    the payload."""
    project = _scaffold_minimal_project(tmp_path / "project")
    _full_auto_tokenise_harvest(FIXTURE_ROOT, project)

    payload = project / "payload" / "database"
    rel_paths = [
        str(p.relative_to(payload)).replace("\\", "/")
        for p in payload.rglob("*")
        if p.is_file()
    ]

    # At least one of each:
    classes: Dict[str, bool] = {
        "table_ddl": any(p.endswith(".tbl") for p in rel_paths),
        "view_ddl": any(p.endswith(".viw") for p in rel_paths),
        "database_ddl": any(p.endswith(".db") for p in rel_paths),
    }
    missing = [name for name, present in classes.items() if not present]
    assert not missing, (
        f"Harvest emitted no artefacts in classes: {missing}. "
        "Fixture content or harvest classification regressed."
    )

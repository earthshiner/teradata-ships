"""
test_harvest_determinism.py — PR1a probe (handover 2026-06-19).

Locks down the SHIPS programme's foundational invariant: **two
consecutive harvests of the same source with identical arguments must
produce byte-identical ``payload/`` trees.**

The handover's Appendix A1 documents the bug this probe is *aimed* at —
two runs of the live CallCentre DBC export with identical args produced
69 vs 81 analysed objects, the 12-object delta being stray whole-name
``{{DB_PREFIX_X}}.dcl`` grant files leaking intermittently from the
prefix-token path.

**Important caveat on observed behaviour.** The handover's "write the
test FIRST, it must fail today" rule was based on the live live
reproduction against the real CallCentre DBC export. On the inline
synthetic fixture built into this file (6 modules, T/V/BUS_V triad
each, source-side ``GRANT`` DCL, cross-module references), the probe
does NOT fail on main today — even under ``PYTHONHASHSEED=random``,
across five iterations, and including the full CLI two-pass
auto-tokenise flow that the live failure exercised.

Two interpretations of that null:
  (a) recent merges (#346 rmtree harvest clean, #348 prereq regex,
      #349 keyword_case OFF) incidentally tightened the harvest path;
  (b) the synthetic fixture, while shaped like a DBC export, lacks
      whichever specific structural feature of the real CallCentre
      export triggers the non-determinism — most likely some
      density/cardinality of inter-database grants or a specific
      identifier-shape combination this fixture doesn't reproduce.

The honest call: ship the probe as a **standing guard** against the
invariant rather than fake a failure. The PR4 harness will rerun this
probe against the curated CallCentre slice (Option B); if A1's bug
is real and the synthetic fixture was the only reason it didn't fire,
PR4 will catch it. Until then, this file locks the invariant on
synthetic inputs.

Two probes:

1. ``test_double_harvest_byte_identical`` — the main invariant.
2. ``test_prefix_token_applies_without_auto_tokenise`` — verifies
   the handover's Appendix A2 footgun. The current API-level audit
   indicates the substitution is already unconditional, so this is
   primarily a guard test — if it ever starts failing, the regression
   reintroduces the footgun.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from td_release_packager.ingest import ingest_directory


# ---------------------------------------------------------------
# Minimal inline fixture — shaped like a DBC export
# ---------------------------------------------------------------
#
# The literal prefix ``CallCentre`` mirrors the handover's golden
# fixture exactly so the prefix tokenisation under test exercises the
# same code path. Coverage:
#
#   * a base-table database (``CallCentre_DOM_STD_T``)
#   * a business-view database (``CallCentre_DOM_BUS_V``)
#   * a table in the base database
#   * a view in the business-view database that joins the table —
#     the path that infers a cross-database grant, which is what
#     produced the stray whole-name ``.dcl`` files in A1
#   * a pre-requisite ``CREATE DATABASE`` for each database with an
#     explicit ``FROM <parent>`` clause so the deploy-order analyser
#     is satisfied (the parent here is the literal ``DATAPRODUCTS``
#     that CallCentre actually deploys under — kept literal because
#     it's external to the package's tokenised scope)
#
# Total: 5 files. Enough to surface the non-determinism, small enough
# that a diff is human-readable when this fails.


# Six databases × multiple objects each — A1's live CallCentre export
# had ~19 databases and 81 analysed objects. Three was not enough to
# expose the iteration-order leak (the dict insertion order was
# implicitly stable for a tiny set). This shape mirrors the per-module
# T/V/BUS_V triad CallCentre uses and seeds enough cross-database
# grants to surface the same code path.
_MODULES = ["DOM", "MEM", "PRE", "OBS", "STG", "SEM"]


def _build_fixture_files() -> Dict[str, str]:
    """Assemble the inline DBC-shaped fixture as a dict of relpath → body.

    For each module M, emits:
      * ``CallCentre_M_STD_T.db`` — base-table database
      * ``CallCentre_M_BUS_V.db`` — business-view database
      * ``CallCentre_M_STD_T.Customer.tbl`` — a base table
      * ``CallCentre_M_BUS_V.Customer.viw`` — a view joining the
        same-module base table (intra-module grant)
      * ``CallCentre_M_BUS_V.CrossRef.viw`` — a view joining a
        DIFFERENT module's base table (cross-module grant — the
        path that generates ``inter_db`` ``.dcl`` files, which is
        where the whole-name leak surfaced in A1)
    """
    files: Dict[str, str] = {}
    for i, mod in enumerate(_MODULES):
        cross = _MODULES[(i + 1) % len(_MODULES)]
        files[f"CallCentre_{mod}_STD_T.db"] = (
            f"CREATE DATABASE CallCentre_{mod}_STD_T FROM DATAPRODUCTS "
            "AS PERM = 0 SPOOL = 0 FALLBACK;\n"
        )
        files[f"CallCentre_{mod}_BUS_V.db"] = (
            f"CREATE DATABASE CallCentre_{mod}_BUS_V FROM DATAPRODUCTS "
            "AS PERM = 0 SPOOL = 0 FALLBACK;\n"
        )
        files[f"CallCentre_{mod}_STD_T.Customer.tbl"] = (
            f"CREATE MULTISET TABLE CallCentre_{mod}_STD_T.Customer (\n"
            "    customer_id INTEGER NOT NULL,\n"
            "    name VARCHAR(100),\n"
            "    created DATE\n"
            ") PRIMARY INDEX (customer_id);\n"
        )
        files[f"CallCentre_{mod}_BUS_V.Customer.viw"] = (
            f"CREATE VIEW CallCentre_{mod}_BUS_V.Customer "
            "(customer_id, name, created) AS\n"
            "LOCKING ROW FOR ACCESS\n"
            "SELECT customer_id, name, created\n"
            f"FROM CallCentre_{mod}_STD_T.Customer;\n"
        )
        files[f"CallCentre_{mod}_BUS_V.CrossRef.viw"] = (
            f"CREATE VIEW CallCentre_{mod}_BUS_V.CrossRef "
            "(customer_id, other_name) AS\n"
            "LOCKING ROW FOR ACCESS\n"
            f"SELECT customer_id, name FROM CallCentre_{cross}_STD_T.Customer;\n"
        )
        # Source-side DCL — mirrors what a DBC export carries: explicit
        # inter-database GRANT for each business-view database's read
        # access to its own module's tables and to the cross-module
        # base it references via CrossRef.viw above. This is the path
        # that emitted whole-name ``{{DB_PREFIX_X}}.dcl`` files in A1.
        files[f"CallCentre_{mod}_BUS_V.dcl"] = (
            f"GRANT SELECT ON CallCentre_{mod}_STD_T "
            f"TO CallCentre_{mod}_BUS_V WITH GRANT OPTION;\n"
            f"GRANT SELECT ON CallCentre_{cross}_STD_T "
            f"TO CallCentre_{mod}_BUS_V WITH GRANT OPTION;\n"
        )
    return files


_FIXTURE_FILES: Dict[str, str] = _build_fixture_files()


def _write_fixture(target: Path) -> Path:
    """Materialise the inline fixture into ``target`` and return it."""
    target.mkdir(parents=True, exist_ok=True)
    for filename, body in _FIXTURE_FILES.items():
        (target / filename).write_text(body, encoding="utf-8")
    return target


# ---------------------------------------------------------------
# Minimal SHIPS project scaffolding
# ---------------------------------------------------------------


def _scaffold_minimal_project(root: Path) -> Path:
    """Create just enough of a SHIPS project tree for harvest to run.

    Mirrors the structure conftest.py's ``tmp_project`` fixture builds,
    but stripped to the minimum directories ``ingest_directory`` will
    populate. Kept local so a future change to the conftest fixture
    cannot silently destabilise this probe.
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
    (root / "config").mkdir(exist_ok=True)
    (root / ".build_counter").write_text("0", encoding="utf-8")
    return root


# ---------------------------------------------------------------
# Tree comparison
# ---------------------------------------------------------------


def _payload_file_index(project_root: Path) -> List[Tuple[str, str]]:
    """Return ``[(relpath, sha256), ...]`` for every file under
    ``payload/`` of ``project_root``, sorted by relpath.

    Sorting on the comparison side ensures the diff captures content
    and filename-set differences without being noisy about traversal
    order. The harvest run itself is the system under test for
    determinism — not this index.
    """
    payload = project_root / "payload"
    entries: List[Tuple[str, str]] = []
    for root, _dirs, files in os.walk(payload):
        for fname in files:
            fpath = Path(root) / fname
            rel = str(fpath.relative_to(project_root)).replace("\\", "/")
            digest = hashlib.sha256(fpath.read_bytes()).hexdigest()
            entries.append((rel, digest))
    entries.sort()
    return entries


def _diff_indexes(
    a: List[Tuple[str, str]],
    b: List[Tuple[str, str]],
) -> List[str]:
    """Return human-readable diff lines between two payload indexes."""
    a_map = dict(a)
    b_map = dict(b)
    a_only = sorted(set(a_map) - set(b_map))
    b_only = sorted(set(b_map) - set(a_map))
    common_diff = sorted(
        rel for rel in a_map.keys() & b_map.keys() if a_map[rel] != b_map[rel]
    )
    out = []
    for rel in a_only:
        out.append(f"  only in run A: {rel}")
    for rel in b_only:
        out.append(f"  only in run B: {rel}")
    for rel in common_diff:
        out.append(f"  content differs: {rel} ({a_map[rel][:12]} vs {b_map[rel][:12]})")
    return out


# ---------------------------------------------------------------
# Probes
# ---------------------------------------------------------------


def _full_auto_tokenise_harvest(
    source: Path,
    project: Path,
    prefix_tokens: Dict[str, str],
) -> None:
    """Mirror the CLI / MCP auto-tokenise harvest flow.

    The live failure observed in handover A1 happened via the full
    auto-tokenise path, which is a two-pass invocation:

      Pass 1 — detect_tokens=True, no apply_tokens, no prefix_tokens.
               Collects token candidates from the source.
      Pass 2 — detect_tokens=True, apply_tokens=<map derived from
               pass-1 candidates>, prefix_tokens=<as supplied>.

    Calling ``ingest_directory`` once with just ``prefix_tokens`` (as
    the original probe did) skips the candidate-driven ``apply_tokens``
    branch, which is the path whose non-deterministic dict iteration
    is the prime suspect in §PR1 of the handover.
    """
    from td_release_packager.token_engine import generate_token_map

    # Pass 1 — detection only. ``clean_payload=True`` here is correct;
    # the production flow also wipes between passes inside the same
    # logical harvest (the second pass re-runs from clean).
    detection = ingest_directory(
        str(source),
        str(project),
        detect_tokens=True,
        apply_tokens=None,
        clean_payload=True,
    )

    apply_tokens = None
    if detection.token_candidates:
        # ``env_prefix=None`` matches the handover's PR1 brief
        # ("retire env_prefix from the prefix path"); the candidate
        # name becomes the token name directly.
        apply_tokens = generate_token_map(detection.token_candidates, None)

    # Pass 2 — apply.
    ingest_directory(
        str(source),
        str(project),
        detect_tokens=True,
        apply_tokens=apply_tokens,
        prefix_tokens=prefix_tokens,
        clean_payload=True,
    )


@pytest.mark.parametrize("iteration", range(5))
def test_double_harvest_byte_identical(tmp_path: Path, iteration: int) -> None:
    """**PR1 invariant.** Two harvests of the same source with identical
    arguments produce byte-identical payload trees.

    Parametrised across 5 iterations because Python's dict and set
    iteration order is implicitly stable within a single process for
    small inputs even when the *intent* is non-deterministic — A1's
    bug surfaced run-to-run on a richer payload, so we sample enough
    times to catch the order-sensitive emission paths.

    The probe runs each harvest into its own project root via the
    full CLI/MCP auto-tokenise flow (two passes), indexes every file
    under ``payload/`` by SHA-256, and asserts the indexes are equal.
    Any difference — extra file, missing file, or differing bytes —
    fails with a readable diff.
    """
    source = _write_fixture(tmp_path / "source")
    project_a = _scaffold_minimal_project(tmp_path / "project_a")
    project_b = _scaffold_minimal_project(tmp_path / "project_b")

    prefix_tokens = {"CallCentre": "DB_PREFIX"}
    _full_auto_tokenise_harvest(source, project_a, prefix_tokens)
    _full_auto_tokenise_harvest(source, project_b, prefix_tokens)

    index_a = _payload_file_index(project_a)
    index_b = _payload_file_index(project_b)

    if index_a != index_b:
        diff = "\n".join(_diff_indexes(index_a, index_b))
        pytest.fail(
            f"Iteration {iteration}: two consecutive harvests with "
            "identical args produced different payload trees. This is "
            "the PR1 invariant — fixing it is the gate for the rest "
            f"of the programme.\n\nDiff:\n{diff}"
        )


def test_prefix_token_applies_without_auto_tokenise(tmp_path: Path) -> None:
    """**Handover Appendix A2 guard.** ``prefix_token`` must apply even
    when the detection pass is disabled — the handover documents a
    footgun where it was silently inert without ``auto_tokenise=true``.

    The current API-level audit found the substitution applies
    unconditionally at ``ingest._ingest_directory_impl`` regardless of
    the detection toggle, so this probe is expected to PASS on main
    today. It exists as a regression guard: if a future refactor
    re-couples the application path to the detection toggle, this
    will fail loudly.
    """
    source = _write_fixture(tmp_path / "source")
    project = _scaffold_minimal_project(tmp_path / "project")

    # ``detect_tokens=False`` corresponds to ``auto_tokenise=False`` at
    # the CLI / MCP surface — see ``cli.py::_cmd_ingest`` where the
    # detection pass is gated on the same flag.
    ingest_directory(
        str(source),
        str(project),
        prefix_tokens={"CallCentre": "DB_PREFIX"},
        detect_tokens=False,
        clean_payload=True,
    )

    # Walk every emitted file and assert the literal source prefix
    # is absent everywhere — both in filenames and in contents. If a
    # single ``CallCentre`` literal survives, prefix tokenisation
    # didn't run.
    payload = project / "payload" / "database"
    survivors: List[str] = []
    for root, _dirs, files in os.walk(payload):
        for fname in files:
            fpath = Path(root) / fname
            rel = str(fpath.relative_to(project)).replace("\\", "/")
            if "CallCentre" in fname:
                survivors.append(f"  filename retains literal prefix: {rel}")
                continue
            try:
                body = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "CallCentre" in body:
                survivors.append(f"  contents retain literal prefix: {rel}")

    if survivors:
        pytest.fail(
            "prefix_token did not apply — literal source prefix "
            "survived in the payload despite a prefix_tokens dict "
            "being supplied. The handover (Appendix A2) flagged this "
            "as a footgun; the API-level path was believed to be "
            "unconditional. A regression has reintroduced it.\n\n"
            + "\n".join(survivors)
        )

"""Grants fixer via the shared fix registry (#526).

Thin coverage layer over ``td_release_packager.fixers.grants`` —
delegates most behaviour testing to ``test_validate_grants.py`` (the
underlying ``fix_grants`` inferrer is exercised there). What this
module owns:

* The wrapper returns a :class:`FixResult` with the right ``rule_id``
  and the aggregate count folded into ``totals``.
* ``dry_run=True`` performs no writes even in the presence of a
  fixable drift.
* The registry entry is default-on so ``ships fix`` (with no flags)
  picks it up alongside ``ddl_terminator``.

CLI-level end-to-end tests for the ``ships fix`` verb live in
``test_ships_fix_cli.py``; this module tests the fixer function
directly.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.fixers import FIX_REGISTRY, FixResult
from td_release_packager.fixers.grants import fix_grants


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _setup_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "ships.yaml").write_text("name: testpkg\n", encoding="utf-8", newline="")
    (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
    _write(
        project / "payload/database/DDL/tables/Prod.T.tbl",
        "CREATE MULTISET TABLE Prod.T (Id INTEGER) PRIMARY INDEX (Id);\n"
        "GRANT SELECT ON Prod.T TO Reader;\n",
    )
    return project


class TestRegistryEntry:
    def test_grants_derivation_is_registered(self):
        assert "grants_derivation" in FIX_REGISTRY

    def test_grants_derivation_is_default_on(self):
        """A bare ``ships fix`` (no --rules / --all) should include
        grants derivation. Rationale: same as the historical
        ``--fix-grants`` default before #526 removed the inspect flag."""
        assert FIX_REGISTRY["grants_derivation"].default_on is True

    def test_grants_derivation_writes_to_payload(self):
        assert FIX_REGISTRY["grants_derivation"].write_scope == "payload"


class TestFixerReturnShape:
    def test_apply_returns_fix_result_with_correct_rule_id(self, tmp_path):
        project = _setup_project(tmp_path)
        result = fix_grants(str(project))
        assert isinstance(result, FixResult)
        assert result.rule_id == "grants_derivation"
        assert result.dry_run is False

    def test_totals_carries_files_written(self, tmp_path):
        project = _setup_project(tmp_path)
        result = fix_grants(str(project))
        # `files_written` is always present in totals so the CLI can
        # report a count without a KeyError on a clean-tree run.
        assert "files_written" in result.totals

    def test_dry_run_flag_is_reflected_in_result(self, tmp_path):
        project = _setup_project(tmp_path)
        result = fix_grants(str(project), dry_run=True)
        assert result.dry_run is True
        # files_written is a projection under dry_run — never negative.
        assert result.totals.get("files_written", 0) >= 0


class TestDryRunMakesNoWrites:
    def test_dry_run_leaves_dcl_dir_untouched(self, tmp_path):
        """Central promise of dry-run: no files under payload/ are
        created, deleted, or modified. Snapshot the tree, run dry-run,
        and diff — anything different fails the test."""
        project = _setup_project(tmp_path)

        def _snapshot() -> dict[Path, tuple[bytes, float]]:
            out: dict[Path, tuple[bytes, float]] = {}
            for p in (project / "payload").rglob("*"):
                if p.is_file():
                    out[p] = (p.read_bytes(), p.stat().st_mtime)
            return out

        before = _snapshot()
        fix_grants(str(project), dry_run=True)
        after = _snapshot()

        added = sorted(str(p) for p in after.keys() - before.keys())
        removed = sorted(str(p) for p in before.keys() - after.keys())
        changed = sorted(
            str(p) for p in before.keys() & after.keys() if before[p] != after[p]
        )
        assert not added, f"dry_run added files: {added}"
        assert not removed, f"dry_run deleted files: {removed}"
        assert not changed, f"dry_run modified files: {changed}"

"""
test_scaffolder_placement_defaults.py — Workstream #307 regression tests.

The scaffolder must emit an ``object_placement.yaml`` whose default
block can be loaded by :class:`ObjectPlacement` without further user
intervention.  The defaults are the SHIPS Teradata field standard:

    strategy: separated
    database_pattern_tables: "{BASE}_T"
    database_pattern_views:  "{BASE}_V"
    locking_views: true

This guards against:

* Future edits to the template that accidentally drop one of the
  required ``database_pattern_*`` keys under the separated strategy
  (which would raise :class:`PlacementConfigError`).
* Silent regression of the locking-views default back to ``False``.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from td_release_packager.object_placement import ObjectPlacement


def _read_scaffolded_yaml(tmp_path: Path) -> dict:
    """Run the scaffolder and parse the active (non-commented) block."""
    from td_release_packager.scaffolder import _generate_object_placement_yaml

    _generate_object_placement_yaml(str(tmp_path), skip_existing=False)
    path = tmp_path / "object_placement.yaml"
    assert path.exists(), "scaffolder did not write object_placement.yaml"
    # yaml.safe_load skips comment lines automatically; what remains
    # is the single active block we want to assert on.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestScaffolderDefaults:
    def test_default_strategy_is_separated(self, tmp_path):
        data = _read_scaffolded_yaml(tmp_path)
        assert data["strategy"] == "separated"

    def test_default_patterns_use_T_V_suffix(self, tmp_path):
        data = _read_scaffolded_yaml(tmp_path)
        assert data["database_pattern_tables"] == "{BASE}_T"
        assert data["database_pattern_views"] == "{BASE}_V"

    def test_default_locking_views_true(self, tmp_path):
        data = _read_scaffolded_yaml(tmp_path)
        assert data["locking_views"] is True

    def test_default_block_loads_via_object_placement(self, tmp_path):
        """The full end-to-end contract: the scaffolded yaml must
        construct a working :class:`ObjectPlacement` without the user
        having to fill anything in."""
        data = _read_scaffolded_yaml(tmp_path)
        op = ObjectPlacement(data)
        assert op.strategy == "separated"
        assert op.locking_views is True
        # And the patterns actually resolve a sample database name.
        views_db = op.resolve_views_database("ACME_DOM_T")
        assert views_db == "ACME_DOM_V"

    def test_existing_file_is_not_overwritten(self, tmp_path):
        path = tmp_path / "object_placement.yaml"
        path.write_text("strategy: colocated\nlocking_views: false\n", encoding="utf-8")
        from td_release_packager.scaffolder import _generate_object_placement_yaml

        _generate_object_placement_yaml(str(tmp_path), skip_existing=False)
        # User-owned file preserved untouched.
        content = path.read_text(encoding="utf-8")
        assert "colocated" in content
        assert "separated" not in content

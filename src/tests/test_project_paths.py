"""
test_project_paths.py — Single source of truth for SHIPS path conventions.

Covers the resolver helpers in ``td_release_packager.project_paths``:
locations, idempotent dir creation, and the rule that hand-edited
config stays under ``config/`` while machine-managed state lives
under ``.ships/``.
"""

from __future__ import annotations

import os
from pathlib import Path

from td_release_packager.project_paths import (
    SHIPS_STATE_DIRNAME,
    BUILD_COUNTER_FILENAME,
    DECISIONS_FILENAME,
    WAVES_FILENAME,
    OBJECT_PLACEMENT_YAML_FILENAME,
    build_counter_path,
    decisions_json_path,
    ensure_ships_state_dir,
    object_placement_yaml_path,
    ships_state_dir,
    waves_txt_path,
)


class TestResolvers:
    """The resolvers return paths inside ``<project>/.ships/`` for
    machine-managed state, and inside ``<project>/config/`` for
    hand-edited config."""

    def test_ships_state_dir_under_project(self, tmp_path: Path) -> None:
        assert ships_state_dir(str(tmp_path)) == os.path.join(
            str(tmp_path), SHIPS_STATE_DIRNAME
        )

    def test_build_counter_lives_under_ships(self, tmp_path: Path) -> None:
        assert build_counter_path(str(tmp_path)) == os.path.join(
            str(tmp_path), SHIPS_STATE_DIRNAME, BUILD_COUNTER_FILENAME
        )

    def test_decisions_lives_under_ships(self, tmp_path: Path) -> None:
        assert decisions_json_path(str(tmp_path)) == os.path.join(
            str(tmp_path), SHIPS_STATE_DIRNAME, DECISIONS_FILENAME
        )

    def test_waves_lives_under_ships(self, tmp_path: Path) -> None:
        assert waves_txt_path(str(tmp_path)) == os.path.join(
            str(tmp_path), SHIPS_STATE_DIRNAME, WAVES_FILENAME
        )

    def test_object_placement_stays_under_config(self, tmp_path: Path) -> None:
        """object_placement.yaml is hand-edited config — NOT machine state."""
        assert object_placement_yaml_path(str(tmp_path)) == os.path.join(
            str(tmp_path), "config", OBJECT_PLACEMENT_YAML_FILENAME
        )


class TestEnsureShipsStateDir:
    """``ensure_ships_state_dir`` returns the path and creates the
    directory only when absent — never erases existing contents."""

    def test_creates_when_missing(self, tmp_path: Path) -> None:
        assert not (tmp_path / SHIPS_STATE_DIRNAME).exists()
        path = ensure_ships_state_dir(str(tmp_path))
        assert os.path.isdir(path)

    def test_idempotent_when_present(self, tmp_path: Path) -> None:
        first = ensure_ships_state_dir(str(tmp_path))
        # Seed a sentinel file; a second call must not erase it.
        sentinel = Path(first) / "sentinel.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        again = ensure_ships_state_dir(str(tmp_path))
        assert again == first
        assert sentinel.read_text(encoding="utf-8") == "keep me"


class TestBuildCounterEndToEnd:
    """The build counter now round-trips through ``.ships/``."""

    def test_scaffolder_writes_under_ships(self, tmp_path: Path) -> None:
        """A fresh scaffold places ``.build_counter`` under ``.ships/`` and
        nothing at the project root."""
        from td_release_packager.scaffolder import _generate_build_counter

        _generate_build_counter(str(tmp_path))

        assert (tmp_path / ".ships" / ".build_counter").exists()
        # And NOT at the project root.
        assert not (tmp_path / ".build_counter").exists()

    def test_counter_round_trip(self, tmp_path: Path) -> None:
        from td_release_packager.build_counter import (
            next_build_number,
            read_build_number,
        )
        from td_release_packager.scaffolder import _generate_build_counter

        _generate_build_counter(str(tmp_path))
        assert read_build_number(str(tmp_path)) == 0
        assert next_build_number(str(tmp_path)) == 1
        assert read_build_number(str(tmp_path)) == 1


class TestGitignoreTemplate:
    """The scaffolded ``.gitignore`` ignores the machine-managed
    ``.ships/`` directory."""

    def test_ships_dir_ignored(self, tmp_path: Path) -> None:
        from td_release_packager.scaffolder import _generate_gitignore

        _generate_gitignore(str(tmp_path))
        body = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert ".ships/" in body


class TestDecisionsFilenameSingleSource:
    """``DECISIONS_FILENAME`` is defined once in ``project_paths`` and
    re-used everywhere (issue #283). Consumers that still expose the
    name must resolve to the same value — guards against a local
    redefinition silently drifting from the canonical filename."""

    def test_orchestrator_reexports_canonical_name(self) -> None:
        from td_release_packager import project_paths
        from td_release_packager.orchestrator import (
            DECISIONS_FILENAME as orch_name,
        )

        assert orch_name == project_paths.DECISIONS_FILENAME

    def test_context_artifacts_uses_canonical_name(self) -> None:
        from td_release_packager import context_artifacts, project_paths

        assert context_artifacts.DECISIONS_FILENAME == project_paths.DECISIONS_FILENAME

    def test_pipeline_report_has_no_local_redefinition(self) -> None:
        from td_release_packager.reporting import pipeline_report

        assert not hasattr(pipeline_report, "DECISIONS_FILENAME")

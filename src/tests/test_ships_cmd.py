"""Tests for the Navigator's friendliest-verb detector (#403)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from td_release_packager import ships_cmd as ships_cmd_module
from td_release_packager.ships_cmd import (
    _in_ships_project,
    install_hint,
    ships_cmd,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the lru_cache between tests so each test picks up its own
    patched environment instead of the first test's result."""
    ships_cmd_module.reset_cache()
    yield
    ships_cmd_module.reset_cache()


def test_picks_ships_when_on_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    assert ships_cmd() == "ships"


def test_falls_back_to_uv_run_when_ships_absent_but_uv_present_in_project(
    monkeypatch, tmp_path
):
    # ships not on PATH, uv is on PATH, cwd is inside a SHIPS project.
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name == "ships" else f"/usr/bin/{name}"
    )
    project = tmp_path / "fake-ships-checkout"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "teradata-ships"\n', encoding="utf-8"
    )
    monkeypatch.chdir(project)

    assert ships_cmd() == "uv run ships"


def test_falls_back_to_uv_run_from_subdir_of_project(monkeypatch, tmp_path):
    """``uv run`` walks parents to find pyproject.toml, so a subdir is
    still in-project for our purposes."""
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name == "ships" else f"/usr/bin/{name}"
    )
    project = tmp_path / "fake-ships-checkout"
    (project / "src" / "nested").mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "td-release-packager"\n', encoding="utf-8"
    )
    monkeypatch.chdir(project / "src" / "nested")

    assert ships_cmd() == "uv run ships"


def test_falls_back_to_python_m_outside_uv_project(monkeypatch, tmp_path):
    """uv is on PATH but cwd is not a SHIPS project — ``uv run ships``
    would resolve the wrong environment, so don't suggest it."""
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name == "ships" else f"/usr/bin/{name}"
    )
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "pyproject.toml").write_text(
        '[project]\nname = "some-other-app"\n', encoding="utf-8"
    )
    monkeypatch.chdir(unrelated)

    assert ships_cmd() == "python -m td_release_packager"


def test_falls_back_to_python_m_when_nothing_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.chdir(tmp_path)
    assert ships_cmd() == "python -m td_release_packager"


def test_in_ships_project_walks_parents(tmp_path):
    project = tmp_path / "proj"
    deep = project / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "teradata-ships"\n', encoding="utf-8"
    )
    assert _in_ships_project(deep) is True


def test_in_ships_project_rejects_unrelated_pyproject(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "django"\n', encoding="utf-8"
    )
    assert _in_ships_project(project) is False


def test_in_ships_project_returns_false_without_any_pyproject(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert _in_ships_project(bare) is False


def test_install_hint_none_when_ships_on_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    assert install_hint() is None


def test_install_hint_present_for_fallback_verbs(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.chdir(tmp_path)
    hint = install_hint()
    assert hint is not None
    assert "uv tool install" in hint


def test_cache_returns_stable_result(monkeypatch):
    """Two calls in the same process return the same answer — caching
    means we don't re-probe PATH on every wizard line."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    first = ships_cmd()
    # Even if shutil.which would now answer differently, the cache wins.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    second = ships_cmd()
    assert first == second == "ships"


def test_real_pyproject_in_this_repo_is_recognised():
    """Smoke check against the actual checkout: this test file lives
    inside the SHIPS project, so the detector must recognise it."""
    here = Path(__file__).resolve().parent
    assert _in_ships_project(here) is True

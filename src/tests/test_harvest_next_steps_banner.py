"""
test_harvest_next_steps_banner.py — Tests for the context-aware
harvest "Next Steps" banner.

Three flows produce three distinct banners. These tests pin which
flow gets which next-step list so regressions surface immediately
rather than via a confused user finding stale guidance in the
wrong scenario.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from td_release_packager.cli import (
    _print_harvest_next_steps,
    _project_has_env_properties,
)


# ---------------------------------------------------------------
# Project-property detection
# ---------------------------------------------------------------


class TestProjectHasEnvProperties:
    def test_no_config_dir(self, tmp_path):
        assert _project_has_env_properties(str(tmp_path)) is False

    def test_empty_properties_dir(self, tmp_path):
        (tmp_path / "config" / "properties").mkdir(parents=True)
        assert _project_has_env_properties(str(tmp_path)) is False

    def test_one_properties_file(self, tmp_path):
        d = tmp_path / "config" / "properties"
        d.mkdir(parents=True)
        (d / "DEV.conf").write_text("X=1\n", encoding="utf-8")
        assert _project_has_env_properties(str(tmp_path)) is True

    def test_other_files_dont_count(self, tmp_path):
        d = tmp_path / "config" / "properties"
        d.mkdir(parents=True)
        (d / "README.md").write_text("hi\n", encoding="utf-8")
        assert _project_has_env_properties(str(tmp_path)) is False


# ---------------------------------------------------------------
# Flow A — --generate-token-map was used
# ---------------------------------------------------------------


class TestFlowA_GenerateTokenMap:
    """Token map was just written; substitutions NOT applied yet.

    Banner must guide the user through reviewing the map,
    bootstrapping properties (if none exist), re-harvesting with
    the map applied, verifying properties, and packaging.
    """

    def _run(self, tmp_path, *, has_props: bool):
        if has_props:
            d = tmp_path / "config" / "properties"
            d.mkdir(parents=True)
            (d / "DEV.conf").write_text("X=1\n", encoding="utf-8")

        args = Namespace(project=str(tmp_path))
        return args

    def test_flow_a_with_no_properties_yet(self, tmp_path, capsys):
        """6 steps: review map, bootstrap properties, re-harvest,
        validate (inspect/analyze/scan), verify properties, package."""
        args = self._run(tmp_path, has_props=False)
        token_map = str(tmp_path / "config" / "token_map.conf")

        _print_harvest_next_steps(
            args,
            generated_token_map_path=token_map,
            substitutions_applied=False,
        )

        out = capsys.readouterr().out
        # Step 1: review the map (with the actual path)
        assert "Review the generated token map" in out
        assert token_map in out
        # Step 2: bootstrap properties via decompose-names
        assert "decompose-names" in out
        assert "Bootstrap a .conf file" in out
        # Step 3: re-harvest with --token-map
        assert "Re-harvest with the token map applied" in out
        assert "--token-map" in out
        # Step 4: quality gates — all three SHIPS validators
        assert "inspect" in out
        assert "analyze" in out
        assert "scan" in out
        assert "Validate the harvested DDL" in out
        # Step 5: verify properties
        assert "Verify environment properties match your topology" in out
        assert "SHIPS_ENV" in out
        # Step 6: package
        assert "package" in out
        assert "--output releases/" in out

    def test_flow_a_with_existing_properties_skips_bootstrap(self, tmp_path, capsys):
        """5 steps when properties already exist: review map,
        re-harvest, validate, verify, package. No bootstrap step."""
        args = self._run(tmp_path, has_props=True)
        token_map = str(tmp_path / "config" / "token_map.conf")

        _print_harvest_next_steps(
            args,
            generated_token_map_path=token_map,
            substitutions_applied=False,
        )

        out = capsys.readouterr().out
        # The bootstrap step is skipped
        assert "Bootstrap a .conf file" not in out
        # But the rest — including the quality gates — should appear
        assert "Review the generated token map" in out
        assert "Re-harvest with the token map applied" in out
        assert "Validate the harvested DDL" in out
        assert "inspect" in out
        assert "analyze" in out
        assert "scan" in out
        assert "Verify environment properties" in out


# ---------------------------------------------------------------
# Flow B — --token-map was applied
# ---------------------------------------------------------------


class TestFlowB_SubstitutionsApplied:
    """Substitutions are now baked into the staged DDL.
    Banner: 3 steps — validate, verify properties, package."""

    def test_no_token_map_guidance_when_applied(self, tmp_path, capsys):
        args = Namespace(project=str(tmp_path))

        _print_harvest_next_steps(
            args,
            generated_token_map_path=None,
            substitutions_applied=True,
        )

        out = capsys.readouterr().out
        # Banner must NOT mention the token map review or
        # decompose-names — those steps are done.
        assert "Review the generated token map" not in out
        assert "decompose-names" not in out
        assert "Re-harvest with the token map applied" not in out
        # But quality gates + verify + package must be present
        assert "Validate the harvested DDL" in out
        assert "inspect" in out
        assert "analyze" in out
        assert "scan" in out
        assert "Verify environment properties" in out
        assert "package" in out


# ---------------------------------------------------------------
# Flow C — plain harvest, no token activity
# ---------------------------------------------------------------


class TestFlowC_PlainHarvest:
    """No --generate-token-map and no --token-map. Banner is the
    same as flow B — validate, verify properties, package."""

    def test_plain_harvest_same_as_applied(self, tmp_path, capsys):
        args = Namespace(project=str(tmp_path))

        _print_harvest_next_steps(
            args,
            generated_token_map_path=None,
            substitutions_applied=False,
        )

        out = capsys.readouterr().out
        assert "decompose-names" not in out
        assert "Validate the harvested DDL" in out
        assert "inspect" in out
        assert "Verify environment properties" in out
        assert "package" in out


# ---------------------------------------------------------------
# Flow D — already-tokenised source
# ---------------------------------------------------------------


class TestFlowD_AlreadyTokenised:
    """--generate-token-map was used, but no literals were found
    because the source already uses {{TOKEN}} references. Banner
    must skip the token-map dance and route straight to
    bootstrap-properties."""

    def test_already_tokenised_no_props_yet(self, tmp_path, capsys):
        """Flow D, no .properties file → 4 steps:
        bootstrap-properties, validate, verify, package.
        Token map / decompose-names / re-harvest must NOT appear."""
        args = Namespace(project=str(tmp_path))

        _print_harvest_next_steps(
            args,
            generated_token_map_path=None,
            substitutions_applied=False,
            already_tokenised=True,
        )

        out = capsys.readouterr().out

        # Stage line + state line
        assert "[H] Harvest complete" in out
        assert "already tokenised" in out

        # Flow-D-specific guidance
        assert "bootstrap-properties" in out
        assert "Bootstrap a .conf file from the tokens" in out

        # Must NOT mention token-map activities — we're past that
        assert "Review the generated token map" not in out
        assert "decompose-names" not in out
        assert "Re-harvest with the token map applied" not in out

        # Quality gates + verify + package still required
        assert "Validate the harvested DDL" in out
        assert "Verify environment properties" in out
        assert "package" in out

    def test_already_tokenised_with_existing_props(self, tmp_path, capsys):
        """When a .properties file already exists, bootstrap step
        becomes optional with --force. Flow still ends in
        validate / verify / package."""
        d = tmp_path / "config" / "properties"
        d.mkdir(parents=True)
        (d / "DEV.conf").write_text("X=1\n", encoding="utf-8")

        args = Namespace(project=str(tmp_path))

        _print_harvest_next_steps(
            args,
            generated_token_map_path=None,
            substitutions_applied=False,
            already_tokenised=True,
        )

        out = capsys.readouterr().out
        # Optional step references --force
        assert "(Optional) Refresh" in out
        assert "--force" in out
        # Quality gates still appear
        assert "Validate the harvested DDL" in out

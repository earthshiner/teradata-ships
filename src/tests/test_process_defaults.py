"""
test_process_defaults.py — `ships process` single-front-door defaults
(issue #384).

Covers ``_apply_process_defaults_from_ships_yaml``: derivation precedence
(CLI arg > packaging: block > convention), the env-config-must-exist
guard, the no-source-convention rule, and the no-ships.yaml no-op.
"""

from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path

import yaml

from td_release_packager.cli import _apply_process_defaults_from_ships_yaml


def _args(**overrides) -> Namespace:
    base = dict(source=None, env=None, env_config=None, name=None, root_parent=None)
    base.update(overrides)
    return Namespace(**base)


def _write_ships_yaml(project: Path, data: dict) -> None:
    (project / "ships.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _write_env_config(project: Path, env: str) -> None:
    env_dir = project / "config" / "env"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / f"{env}.conf").write_text("X=Y\n", encoding="utf-8")


class TestConventionFallback:
    """An empty ``packaging: {}`` block opts in; sub-keys then fall back to
    conventions (project name, first environment, config/env/<ENV>.conf)."""

    def test_derives_name_env_and_existing_env_config(self, tmp_path):
        _write_ships_yaml(
            tmp_path,
            {"project": "OMR", "environments": ["DEV", "PRD"], "packaging": {}},
        )
        _write_env_config(tmp_path, "DEV")

        args = _args()
        _apply_process_defaults_from_ships_yaml(args, str(tmp_path))

        assert args.name == "OMR"
        assert args.env == "DEV"  # first environment
        # Convention path uses the OS separator.
        assert args.env_config == os.path.join("config", "env", "DEV.conf")

    def test_env_config_skipped_when_file_absent(self, tmp_path):
        _write_ships_yaml(
            tmp_path, {"project": "OMR", "environments": ["DEV"], "packaging": {}}
        )
        # No config/env/DEV.conf on disk.

        args = _args()
        _apply_process_defaults_from_ships_yaml(args, str(tmp_path))

        assert args.env == "DEV"
        assert args.env_config is None  # not invented when missing

    def test_source_has_no_convention_fallback(self, tmp_path):
        _write_ships_yaml(
            tmp_path, {"project": "OMR", "environments": ["DEV"], "packaging": {}}
        )

        args = _args()
        _apply_process_defaults_from_ships_yaml(args, str(tmp_path))

        assert args.source is None  # omitting source means "use payload"


class TestPackagingBlock:
    def test_packaging_block_supplies_values(self, tmp_path):
        _write_ships_yaml(
            tmp_path,
            {
                "project": "OMR",
                "environments": ["DEV", "PRD"],
                "packaging": {
                    "source": "src/ddl",
                    "name": "OMR_changeset",
                    "default_env": "PRD",
                    "env_config": "config/env/PRD.conf",
                },
            },
        )

        args = _args()
        _apply_process_defaults_from_ships_yaml(args, str(tmp_path))

        assert args.source == "src/ddl"
        assert args.name == "OMR_changeset"
        assert args.env == "PRD"  # default_env wins over environments[0]
        # packaging.env_config is taken verbatim (no existence requirement).
        assert args.env_config == "config/env/PRD.conf"

    def test_root_parent_supplied_from_packaging(self, tmp_path):
        """#501 — packaging.root_parent feeds args.root_parent so the
        argless flow injects FROM <parent> into top-level CREATE
        DATABASE/USER statements without a manual --root-parent flag."""
        _write_ships_yaml(
            tmp_path,
            {
                "project": "OMR",
                "environments": ["DEV"],
                "packaging": {"root_parent": "DataProducts"},
            },
        )

        args = _args()
        _apply_process_defaults_from_ships_yaml(args, str(tmp_path))

        assert args.root_parent == "DataProducts"

    def test_root_parent_cli_value_wins(self, tmp_path):
        """An explicit --root-parent on the CLI must NOT be overwritten
        by packaging.root_parent — same precedence as name/env."""
        _write_ships_yaml(
            tmp_path,
            {
                "project": "OMR",
                "environments": ["DEV"],
                "packaging": {"root_parent": "FromYaml"},
            },
        )

        args = _args(root_parent="FromCli")
        _apply_process_defaults_from_ships_yaml(args, str(tmp_path))

        assert args.root_parent == "FromCli"


class TestPrecedenceAndNoOp:
    def test_cli_args_are_never_overridden(self, tmp_path):
        _write_ships_yaml(
            tmp_path,
            {
                "project": "OMR",
                "environments": ["DEV"],
                "packaging": {"name": "from_yaml", "default_env": "DEV"},
            },
        )

        args = _args(name="explicit", env="TST")
        _apply_process_defaults_from_ships_yaml(args, str(tmp_path))

        assert args.name == "explicit"
        assert args.env == "TST"

    def test_no_ships_yaml_is_a_noop(self, tmp_path):
        args = _args()
        _apply_process_defaults_from_ships_yaml(args, str(tmp_path))

        assert args.name is None
        assert args.env is None
        assert args.env_config is None
        assert args.source is None

    def test_ships_yaml_without_packaging_block_is_a_noop(self, tmp_path):
        """A project that has NOT opted into the packaging profile keeps the
        old behaviour — nothing is derived, so process won't auto-package."""
        _write_ships_yaml(tmp_path, {"project": "OMR", "environments": ["DEV"]})
        _write_env_config(tmp_path, "DEV")

        args = _args()
        _apply_process_defaults_from_ships_yaml(args, str(tmp_path))

        assert args.name is None
        assert args.env is None
        assert args.env_config is None
        assert args.source is None

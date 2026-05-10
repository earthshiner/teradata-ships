"""
test_scan_enhanced.py — Tests for the enhanced ships scan command.

Covers:
    - --show-map: token → file reverse index printed per token
    - --all-envs: discovers all *.conf in config/env/, validates each
    - --format json: machine-readable output with token_map and validation keys
    - --fail-on-orphan: exit 1 when orphan tokens exist
    - --env-config + --all-envs mutual exclusion: exits with error
    - cross-env sweep identifies which envs have undefined tokens
    - orphan tokens (defined in conf but never referenced) flagged per env
    - clean run (no undefined, no orphans) exits 0 with success message
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


_SRC_DIR = str(Path(__file__).parent.parent)  # .../src


def _ships(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run ships scan via subprocess with src/ on PYTHONPATH."""
    env = os.environ.copy()
    env["PYTHONPATH"] = _SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "td_release_packager"] + args,
        capture_output=True,
        text=True,
        cwd=cwd or str(Path(__file__).parent.parent.parent),
        env=env,
    )


def _make_project(
    tmp_path: Path, tokens: list[str], env_configs: dict[str, dict]
) -> Path:
    """
    Create a minimal SHIPS project for scan tests.

    tokens:       list of token names to embed in a single .tbl file
    env_configs:  {env_name: {token_name: value, ...}}
    """
    project = tmp_path / "project"
    payload = project / "payload" / "database" / "DDL" / "tables"
    payload.mkdir(parents=True)

    # Write one DDL file using all requested tokens as column defaults.
    # Use a plain (non-tokenised) table qualifier so {{DB}} is never
    # accidentally introduced as an undefined token in tests.
    ddl_lines = ["CREATE MULTISET TABLE TESTDB.T ("]
    for i, tok in enumerate(tokens):
        ddl_lines.append(
            f"    col_{i} VARCHAR(100) DEFAULT '{{{{{tok}}}}}'{',' if i < len(tokens) - 1 else ''}"
        )
    ddl_lines.append(") PRIMARY INDEX (col_0);")
    (payload / "DB.T.tbl").write_text("\n".join(ddl_lines), encoding="utf-8")

    # Write env configs
    env_dir = project / "config" / "env"
    env_dir.mkdir(parents=True)
    for env_name, values in env_configs.items():
        lines = [f"SHIPS_ENV={env_name.upper()}"]
        for k, v in values.items():
            lines.append(f"{k}={v}")
        (env_dir / f"{env_name}.conf").write_text("\n".join(lines), encoding="utf-8")

    # Build counter
    (project / ".build_counter").write_text("0\n", encoding="utf-8")

    return project


# ---------------------------------------------------------------
# --show-map
# ---------------------------------------------------------------


class TestShowMap:
    def test_prints_file_list_per_token(self, tmp_path):
        project = _make_project(
            tmp_path,
            tokens=["MY_DB", "OTHER_DB"],
            env_configs={},
        )
        result = _ships(["scan", "--source", str(project), "--show-map"])
        assert "MY_DB" in result.stdout
        assert "OTHER_DB" in result.stdout
        # With --show-map, files should be listed (not just count)
        assert "DB.T.tbl" in result.stdout

    def test_without_show_map_only_shows_count(self, tmp_path):
        project = _make_project(tmp_path, tokens=["MY_DB"], env_configs={})
        result = _ships(["scan", "--source", str(project)])
        assert "MY_DB" in result.stdout
        # Without --show-map, files should NOT be listed individually
        assert "file(s)" in result.stdout


# ---------------------------------------------------------------
# --all-envs
# ---------------------------------------------------------------


class TestAllEnvs:
    def test_discovers_all_conf_files(self, tmp_path):
        project = _make_project(
            tmp_path,
            tokens=["MY_DB"],
            env_configs={
                "DEV": {"MY_DB": "A_DEV_DB"},
                "PRD": {"MY_DB": "P_PRD_DB"},
            },
        )
        result = _ships(["scan", "--source", str(project), "--all-envs"])
        assert "DEV" in result.stdout
        assert "PRD" in result.stdout

    def test_clean_sweep_exits_0(self, tmp_path):
        project = _make_project(
            tmp_path,
            tokens=["MY_DB"],
            env_configs={
                "DEV": {"MY_DB": "A_DEV_DB"},
                "TST": {"MY_DB": "T_TST_DB"},
            },
        )
        result = _ships(["scan", "--source", str(project), "--all-envs"])
        assert result.returncode == 0

    def test_undefined_in_one_env_exits_1(self, tmp_path):
        project = _make_project(
            tmp_path,
            tokens=["MY_DB"],
            env_configs={
                "DEV": {"MY_DB": "A_DEV_DB"},
                "PRD": {},  # MY_DB not defined in PRD
            },
        )
        result = _ships(["scan", "--source", str(project), "--all-envs"])
        assert result.returncode == 1
        assert "MY_DB" in result.stdout

    def test_shows_per_env_status(self, tmp_path):
        project = _make_project(
            tmp_path,
            tokens=["MY_DB"],
            env_configs={
                "DEV": {"MY_DB": "A_DEV_DB"},
                "PRD": {},
            },
        )
        result = _ships(["scan", "--source", str(project), "--all-envs"])
        # One env passes, one fails — both names appear in output
        assert "DEV" in result.stdout
        assert "PRD" in result.stdout

    def test_mutually_exclusive_with_env_config(self, tmp_path):
        project = _make_project(tmp_path, tokens=["MY_DB"], env_configs={})
        result = _ships(
            [
                "scan",
                "--source",
                str(project),
                "--all-envs",
                "--env-config",
                "config/env/DEV.conf",
            ]
        )
        assert result.returncode == 1
        assert (
            "mutually exclusive" in result.stderr.lower()
            or "mutually exclusive" in result.stdout.lower()
        )


# ---------------------------------------------------------------
# --format json
# ---------------------------------------------------------------


class TestFormatJson:
    def test_output_is_valid_json(self, tmp_path):
        project = _make_project(tmp_path, tokens=["MY_DB"], env_configs={})
        result = _ships(["scan", "--source", str(project), "--format", "json"])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert isinstance(data, dict)

    def test_json_contains_required_keys(self, tmp_path):
        project = _make_project(tmp_path, tokens=["MY_DB"], env_configs={})
        result = _ships(["scan", "--source", str(project), "--format", "json"])
        data = json.loads(result.stdout)
        assert "unique_tokens" in data
        assert "token_map" in data
        assert "files_with_tokens" in data
        assert "validation" in data

    def test_json_token_map_has_count_and_files(self, tmp_path):
        project = _make_project(tmp_path, tokens=["MY_DB"], env_configs={})
        result = _ships(["scan", "--source", str(project), "--format", "json"])
        data = json.loads(result.stdout)
        assert "MY_DB" in data["token_map"]
        entry = data["token_map"]["MY_DB"]
        assert "count" in entry
        assert "files" in entry
        assert entry["count"] >= 1

    def test_json_validation_included_when_env_config_given(self, tmp_path):
        project = _make_project(
            tmp_path,
            tokens=["MY_DB"],
            env_configs={"DEV": {"MY_DB": "A_DEV_DB"}},
        )
        env_conf = str(project / "config" / "env" / "DEV.conf")
        result = _ships(
            [
                "scan",
                "--source",
                str(project),
                "--env-config",
                env_conf,
                "--format",
                "json",
            ]
        )
        data = json.loads(result.stdout)
        assert "DEV" in data["validation"] or any(data["validation"])

    def test_json_undefined_token_listed(self, tmp_path):
        project = _make_project(
            tmp_path,
            tokens=["MY_DB"],
            env_configs={"DEV": {}},  # MY_DB not defined
        )
        env_conf = str(project / "config" / "env" / "DEV.conf")
        result = _ships(
            [
                "scan",
                "--source",
                str(project),
                "--env-config",
                env_conf,
                "--format",
                "json",
            ]
        )
        data = json.loads(result.stdout)
        # Find the validation entry (key may be env name or path stem)
        val_entries = list(data["validation"].values())
        assert any("MY_DB" in str(entry.get("undefined", "")) for entry in val_entries)


# ---------------------------------------------------------------
# --fail-on-orphan
# ---------------------------------------------------------------


class TestFailOnOrphan:
    def test_exits_0_when_no_orphans(self, tmp_path):
        project = _make_project(
            tmp_path,
            tokens=["MY_DB"],
            env_configs={"DEV": {"MY_DB": "A_DEV_DB"}},
        )
        env_conf = str(project / "config" / "env" / "DEV.conf")
        result = _ships(
            [
                "scan",
                "--source",
                str(project),
                "--env-config",
                env_conf,
                "--fail-on-orphan",
            ]
        )
        assert result.returncode == 0

    def test_exits_1_when_orphan_exists(self, tmp_path):
        project = _make_project(
            tmp_path,
            tokens=["MY_DB"],
            env_configs={"DEV": {"MY_DB": "A_DEV_DB", "UNUSED": "value"}},
        )
        env_conf = str(project / "config" / "env" / "DEV.conf")
        result = _ships(
            [
                "scan",
                "--source",
                str(project),
                "--env-config",
                env_conf,
                "--fail-on-orphan",
            ]
        )
        assert result.returncode == 1

    def test_orphan_without_flag_exits_0(self, tmp_path):
        """Without --fail-on-orphan, orphans are warnings not errors."""
        project = _make_project(
            tmp_path,
            tokens=["MY_DB"],
            env_configs={"DEV": {"MY_DB": "A_DEV_DB", "UNUSED": "value"}},
        )
        env_conf = str(project / "config" / "env" / "DEV.conf")
        result = _ships(
            [
                "scan",
                "--source",
                str(project),
                "--env-config",
                env_conf,
            ]
        )
        assert result.returncode == 0

    def test_orphan_flagged_in_text_output(self, tmp_path):
        project = _make_project(
            tmp_path,
            tokens=["MY_DB"],
            env_configs={"DEV": {"MY_DB": "A_DEV_DB", "UNUSED_TOKEN": "value"}},
        )
        env_conf = str(project / "config" / "env" / "DEV.conf")
        result = _ships(
            [
                "scan",
                "--source",
                str(project),
                "--env-config",
                env_conf,
            ]
        )
        assert "UNUSED_TOKEN" in result.stdout or "orphan" in result.stdout.lower()


# ---------------------------------------------------------------
# End-to-end clean run
# ---------------------------------------------------------------


class TestCleanRun:
    def test_clean_project_exits_0(self, tmp_path):
        project = _make_project(
            tmp_path,
            tokens=["MY_DB"],
            env_configs={"DEV": {"MY_DB": "A_DEV_DB"}},
        )
        env_conf = str(project / "config" / "env" / "DEV.conf")
        result = _ships(["scan", "--source", str(project), "--env-config", env_conf])
        assert result.returncode == 0

    def test_no_tokens_exits_0(self, tmp_path):
        project = tmp_path / "project"
        (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
        (project / "payload" / "database" / "DDL" / "tables" / "DB.T.tbl").write_text(
            "CREATE MULTISET TABLE DB.T (Id INTEGER) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )
        (project / ".build_counter").write_text("0\n", encoding="utf-8")
        result = _ships(["scan", "--source", str(project)])
        assert result.returncode == 0

"""
test_env_config_bootstrapper.py — Tests for the third bootstrap
path (already-tokenised source → .conf scaffold).

Covers:
    1. Token discovery from a project's payload tree
    2. Existing-value preservation and unused-token detection
    3. Section-8 emission with sorted ordering
    4. CLI: --force gating, --output-dir handling, error paths
    5. Integration: scaffold output round-trips through the
       SHIPS token engine read_env_config()
"""

from __future__ import annotations

from pathlib import Path

import pytest

from td_release_packager import env_config_bootstrapper as boot
from td_release_packager.token_engine import read_env_config


def _make_tokenised_project(tmp_path: Path, ddl_files: dict) -> Path:
    """Create a minimal SHIPS-shape project with the given DDL files
    placed under payload/database/DDL/tables/.

    ``ddl_files`` is ``{filename: contents}``.
    """
    payload = tmp_path / "payload" / "database" / "DDL" / "tables"
    payload.mkdir(parents=True)
    for fname, content in ddl_files.items():
        (payload / fname).write_text(content, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------


class TestDiscoverReferencedTokens:
    def test_finds_tokens_in_payload(self, tmp_path):
        project = _make_tokenised_project(
            tmp_path,
            {
                "a.tbl": "CREATE TABLE {{BASE_T}}.a (id INT);\n",
                "b.tbl": "CREATE TABLE {{GCFR_T}}.b (id INT);\n",
            },
        )
        tokens = boot.discover_referenced_tokens(str(project))
        assert tokens == {"BASE_T", "GCFR_T"}

    def test_dedupes_across_files(self, tmp_path):
        project = _make_tokenised_project(
            tmp_path,
            {
                "a.tbl": "CREATE TABLE {{BASE_T}}.a (id INT);\n",
                "b.tbl": "CREATE TABLE {{BASE_T}}.b (id INT);\n",
            },
        )
        assert boot.discover_referenced_tokens(str(project)) == {"BASE_T"}

    def test_empty_project_returns_empty_set(self, tmp_path):
        project = _make_tokenised_project(tmp_path, {})
        assert boot.discover_referenced_tokens(str(project)) == set()


# ---------------------------------------------------------------
# Existing-value preservation
# ---------------------------------------------------------------


class TestReadExistingValues:
    def test_missing_file_returns_empty(self, tmp_path):
        assert boot.read_existing_values(str(tmp_path / "no.conf")) == {}

    def test_reads_simple_kv_pairs(self, tmp_path):
        f = tmp_path / "p.conf"
        f.write_text("FOO=bar\nBAZ=qux\n", encoding="utf-8")
        result = boot.read_existing_values(str(f))
        assert result == {"FOO": "bar", "BAZ": "qux"}


# ---------------------------------------------------------------
# Section-8 rendering
# ---------------------------------------------------------------


class TestFormatSection8Body:
    def test_referenced_tokens_emitted_alphabetically(self):
        body = boot.format_section_8_body(
            referenced={"GAMMA", "ALPHA", "BETA"},
            existing={},
        )
        # Lines should be in alphabetical order
        lines = [ln for ln in body.split("\n") if "=" in ln and not ln.startswith("#")]
        assert lines == ["ALPHA=", "BETA=", "GAMMA="]

    def test_existing_values_preserved(self):
        body = boot.format_section_8_body(
            referenced={"FOO", "BAR"},
            existing={"FOO": "preserved", "OTHER": "irrelevant"},
        )
        assert "FOO=preserved" in body
        assert "BAR=" in body  # new, empty value

    def test_unused_tokens_flagged(self):
        body = boot.format_section_8_body(
            referenced={"FOO"},
            existing={"FOO": "v1", "STALE": "v2"},
        )
        assert "FOO=v1" in body
        assert "# WARN unused: STALE=v2" in body
        assert "UNUSED tokens" in body


# ---------------------------------------------------------------
# CLI / driver
# ---------------------------------------------------------------


class TestBootstrapPropertiesFile:
    def test_writes_fresh_file_when_none_exists(self, tmp_path):
        project = _make_tokenised_project(
            tmp_path,
            {"a.tbl": "CREATE TABLE {{BASE_T}}.a (id INT);\n"},
        )

        result = boot.bootstrap_env_config_file(
            project_dir=str(project),
            env="DEV",
            output_dir=str(project / "config"),
        )

        props_path = Path(result["env_config_path"])
        assert props_path.exists()
        assert "BASE_T=" in props_path.read_text(encoding="utf-8")
        assert result["overwrote"] is False
        assert result["new"] == ["BASE_T"]

    def test_refuses_to_overwrite_without_force(self, tmp_path):
        project = _make_tokenised_project(
            tmp_path,
            {"a.tbl": "CREATE TABLE {{BASE_T}}.a (id INT);\n"},
        )
        # First run creates it
        boot.bootstrap_env_config_file(
            project_dir=str(project),
            env="DEV",
            output_dir=str(project / "config"),
        )
        # Second run without --force should raise
        with pytest.raises(FileExistsError):
            boot.bootstrap_env_config_file(
                project_dir=str(project),
                env="DEV",
                output_dir=str(project / "config"),
            )

    def test_force_preserves_existing_values(self, tmp_path):
        project = _make_tokenised_project(
            tmp_path,
            {"a.tbl": "CREATE TABLE {{BASE_T}}.a (id INT);\n"},
        )
        # Manually pre-populate a value
        config_dir = project / "config"
        props_dir = config_dir / "env"
        props_dir.mkdir(parents=True)
        props_path = props_dir / "DEV.conf"
        props_path.write_text("BASE_T=existing_value\n", encoding="utf-8")

        result = boot.bootstrap_env_config_file(
            project_dir=str(project),
            env="DEV",
            output_dir=str(config_dir),
            force=True,
        )

        assert result["overwrote"] is True
        assert result["preserved"] == ["BASE_T"]
        # The value survived the rewrite
        assert "BASE_T=existing_value" in props_path.read_text(encoding="utf-8")

    def test_unused_tokens_flagged_in_output(self, tmp_path):
        project = _make_tokenised_project(
            tmp_path,
            {"a.tbl": "CREATE TABLE {{NEW_TOKEN}}.a (id INT);\n"},
        )
        config_dir = project / "config"
        props_dir = config_dir / "env"
        props_dir.mkdir(parents=True)
        (props_dir / "DEV.conf").write_text(
            "OLD_TOKEN=v1\nNEW_TOKEN=v2\n", encoding="utf-8"
        )

        result = boot.bootstrap_env_config_file(
            project_dir=str(project),
            env="DEV",
            output_dir=str(config_dir),
            force=True,
        )

        assert result["unused"] == ["OLD_TOKEN"]
        assert result["preserved"] == ["NEW_TOKEN"]

    def test_missing_project_raises(self, tmp_path):
        with pytest.raises(NotADirectoryError):
            boot.bootstrap_env_config_file(
                project_dir=str(tmp_path / "nope"),
                env="DEV",
            )


# ---------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------


class TestMain:
    def test_main_succeeds_for_fresh_project(self, tmp_path, capsys):
        project = _make_tokenised_project(
            tmp_path,
            {"a.tbl": "CREATE TABLE {{X}}.a (id INT);\n"},
        )

        rc = boot.main(
            [
                "--source",
                str(project),
                "--env",
                "DEV",
                "--output-dir",
                str(project / "config"),
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "bootstrap-env-config" in captured.out
        assert "Next Steps" in captured.out

    def test_main_returns_1_when_clobber_blocked(self, tmp_path, capsys):
        project = _make_tokenised_project(
            tmp_path,
            {"a.tbl": "CREATE TABLE {{X}}.a (id INT);\n"},
        )
        # Run once
        boot.main(
            [
                "--source",
                str(project),
                "--env",
                "DEV",
                "--output-dir",
                str(project / "config"),
            ]
        )
        capsys.readouterr()  # drain
        # Run again without --force → rc=1
        rc = boot.main(
            [
                "--source",
                str(project),
                "--env",
                "DEV",
                "--output-dir",
                str(project / "config"),
            ]
        )
        assert rc == 1
        assert "already exists" in capsys.readouterr().err


# ---------------------------------------------------------------
# Integration — output round-trips through the engine
# ---------------------------------------------------------------


class TestRoundTrip:
    def test_scaffold_output_loads_through_token_engine(self, tmp_path):
        project = _make_tokenised_project(
            tmp_path,
            {
                "a.tbl": "CREATE TABLE {{BASE_T}}.a (id INT);\n",
                "b.tbl": "CREATE TABLE {{GCFR_T}}.b (id INT);\n",
            },
        )

        result = boot.bootstrap_env_config_file(
            project_dir=str(project),
            env="DEV",
            output_dir=str(project / "config"),
        )

        # The file is parseable. Tokens have empty values.
        tokens = read_env_config(result["env_config_path"])
        assert tokens.get("BASE_T") == ""
        assert tokens.get("GCFR_T") == ""

    def test_scaffold_output_includes_seven_sections(self, tmp_path):
        project = _make_tokenised_project(
            tmp_path,
            {"a.tbl": "CREATE TABLE {{X}}.a (id INT);\n"},
        )
        result = boot.bootstrap_env_config_file(
            project_dir=str(project),
            env="DEV",
            output_dir=str(project / "config"),
        )
        text = Path(result["env_config_path"]).read_text(encoding="utf-8")
        for n in range(1, 8):
            assert f"# {n}." in text, f"section {n} missing"
        # Section 8 (Imported) is the destination for the scanned tokens
        assert "# 8. Imported (UNCATEGORISED)" in text

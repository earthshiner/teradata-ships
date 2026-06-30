"""
test_orchestrator_ships_yaml.py — Tests for ships.yaml schema,
parser, validator, defaults, and generate-on-first-run helper.

Covers:
    - load() — parse / missing-file / invalid-YAML / non-mapping top level
    - validate() — required fields, type errors, unknown stages,
      unknown on_error values, complete error collection
    - apply_defaults() — gap-filling, no mutation of input
    - generate_default() — shape, required arg validation
    - write_if_missing() — happy path, never overwrites, atomic
"""

from __future__ import annotations


import pytest

from td_release_packager.orchestrator.ships_yaml import (
    LAYER_1_DEFAULTS,
    STAGES,
    TOKEN_PATTERN,
    VALID_ON_ERROR,
    ShipsConfigError,
    apply_defaults,
    generate_default,
    load,
    validate,
    write_if_missing,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _write_yaml(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _valid_minimal() -> dict:
    return {"project": "X", "environments": ["DEV"]}


# ---------------------------------------------------------------
# load()
# ---------------------------------------------------------------


class TestLoad:
    def test_loads_valid_yaml(self, tmp_path):
        p = tmp_path / "ships.yaml"
        _write_yaml(
            str(p),
            "project: MyProject\nenvironments:\n  - DEV\n  - PRD\n",
        )
        data = load(str(p))
        assert data["project"] == "MyProject"
        assert data["environments"] == ["DEV", "PRD"]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ShipsConfigError, match="not found"):
            load(str(tmp_path / "nope.yaml"))

    def test_invalid_yaml_raises(self, tmp_path):
        p = tmp_path / "ships.yaml"
        _write_yaml(str(p), "project: [unclosed\n")
        with pytest.raises(ShipsConfigError, match="not valid YAML"):
            load(str(p))

    def test_non_mapping_top_level_raises(self, tmp_path):
        p = tmp_path / "ships.yaml"
        _write_yaml(str(p), "- just\n- a\n- list\n")
        with pytest.raises(ShipsConfigError, match="must be a mapping"):
            load(str(p))

    def test_empty_file_returns_empty_dict(self, tmp_path):
        p = tmp_path / "ships.yaml"
        _write_yaml(str(p), "")
        assert load(str(p)) == {}


# ---------------------------------------------------------------
# validate()
# ---------------------------------------------------------------


class TestValidate:
    def test_minimal_valid_returns_empty(self):
        assert validate(_valid_minimal()) == []

    def test_missing_project_flagged(self):
        errs = validate({"environments": ["DEV"]})
        assert any(e.path == "project" for e in errs)

    def test_empty_project_flagged(self):
        errs = validate({"project": "  ", "environments": ["DEV"]})
        assert any(e.path == "project" for e in errs)

    def test_missing_environments_flagged(self):
        errs = validate({"project": "X"})
        assert any(e.path == "environments" for e in errs)

    def test_empty_environments_flagged(self):
        errs = validate({"project": "X", "environments": []})
        assert any(e.path == "environments" for e in errs)

    def test_environments_must_be_list(self):
        errs = validate({"project": "X", "environments": "DEV"})
        assert any(e.path == "environments" for e in errs)

    def test_environment_entry_must_be_non_empty_string(self):
        errs = validate({"project": "X", "environments": ["DEV", ""]})
        assert any(e.path == "environments[1]" for e in errs)

    def test_version_string_or_number_ok(self):
        for v in ("1.0", 1, 1.0):
            data = _valid_minimal() | {"version": v}
            assert validate(data) == []

    def test_version_other_types_rejected(self):
        data = _valid_minimal() | {"version": ["1", "0"]}
        errs = validate(data)
        assert any(e.path == "version" for e in errs)

    def test_unknown_stage_flagged(self):
        data = _valid_minimal() | {"stages": {"frobinate": {"strict": True}}}
        errs = validate(data)
        assert any(e.path == "stages.frobinate" for e in errs)

    def test_known_stages_accepted(self):
        data = _valid_minimal() | {
            "stages": {stage: {"strict": True} for stage in STAGES}
        }
        assert validate(data) == []

    def test_strict_must_be_bool(self):
        data = _valid_minimal() | {"stages": {"generate": {"strict": "yes"}}}
        errs = validate(data)
        assert any(e.path == "stages.generate.strict" for e in errs)

    def test_on_error_vocabulary_enforced(self):
        data = _valid_minimal() | {"stages": {"generate": {"on_error": "maybe"}}}
        errs = validate(data)
        assert any(e.path == "stages.generate.on_error" for e in errs)

    def test_on_error_valid_values_accepted(self):
        for value in VALID_ON_ERROR:
            data = _valid_minimal() | {"stages": {"generate": {"on_error": value}}}
            assert validate(data) == []

    def test_config_block_must_be_mapping(self):
        data = _valid_minimal() | {"config": "config/inspect.conf"}
        errs = validate(data)
        assert any(e.path == "config" for e in errs)

    def test_config_path_must_be_non_empty_string(self):
        data = _valid_minimal() | {"config": {"inspect": ""}}
        errs = validate(data)
        assert any(e.path == "config.inspect" for e in errs)

    def test_collects_all_errors_not_just_first(self):
        data = {
            "environments": [],
            "stages": {"generate": {"strict": "no", "on_error": "maybe"}},
        }
        errs = validate(data)
        paths = {e.path for e in errs}
        # All four issues surface together
        assert "project" in paths
        assert "environments" in paths
        assert "stages.generate.strict" in paths
        assert "stages.generate.on_error" in paths

    def test_validation_error_str_round_trip(self):
        errs = validate({"environments": ["DEV"]})
        # Has a __str__ that combines path + message
        rendered = str(errs[0])
        assert "project" in rendered


# ---------------------------------------------------------------
# packaging block (#384 single front door)
# ---------------------------------------------------------------


class TestPackagingBlock:
    def test_valid_packaging_block(self):
        data = _valid_minimal()
        data["environments"] = ["DEV", "PRD"]
        data["packaging"] = {
            "source": "src/ddl",
            "name": "OMR",
            "default_env": "DEV",
            "env_config": "config/env/DEV.conf",
        }
        assert validate(data) == []

    def test_packaging_must_be_mapping(self):
        data = _valid_minimal()
        data["packaging"] = "nope"
        errs = validate(data)
        assert any(e.path == "packaging" for e in errs)

    def test_packaging_values_must_be_non_empty_strings(self):
        data = _valid_minimal()
        data["packaging"] = {"name": "  "}
        errs = validate(data)
        assert any(e.path == "packaging.name" for e in errs)

    def test_default_env_must_be_a_known_environment(self):
        data = _valid_minimal()
        data["environments"] = ["DEV"]
        data["packaging"] = {"default_env": "PRD"}
        errs = validate(data)
        assert any(e.path == "packaging.default_env" for e in errs)

    def test_root_parent_accepted(self):
        """#501 — packaging.root_parent feeds args.root_parent so the
        argless flow injects FROM <parent> into top-level DB/USER DDL."""
        data = _valid_minimal()
        data["packaging"] = {"root_parent": "DataProducts"}
        assert validate(data) == []

    def test_root_parent_must_be_non_empty(self):
        """Blank / whitespace fails validation the same way name does."""
        data = _valid_minimal()
        data["packaging"] = {"root_parent": "  "}
        errs = validate(data)
        assert any(e.path == "packaging.root_parent" for e in errs)

    def test_root_parent_must_be_string(self):
        data = _valid_minimal()
        data["packaging"] = {"root_parent": 42}
        errs = validate(data)
        assert any(e.path == "packaging.root_parent" for e in errs)


# ---------------------------------------------------------------
# apply_defaults()
# ---------------------------------------------------------------


class TestApplyDefaults:
    def test_fills_config_block(self):
        out = apply_defaults(_valid_minimal())
        assert out["config"]["inspect"] == LAYER_1_DEFAULTS["config"]["inspect"]
        assert out["config"]["placement"] == LAYER_1_DEFAULTS["config"]["placement"]

    def test_preserves_existing_config_values(self):
        data = _valid_minimal() | {"config": {"inspect": "custom/inspect"}}
        out = apply_defaults(data)
        assert out["config"]["inspect"] == "custom/inspect"
        assert out["config"]["placement"] == LAYER_1_DEFAULTS["config"]["placement"]

    def test_fills_per_stage_defaults(self):
        out = apply_defaults(_valid_minimal())
        for stage in STAGES:
            assert out["stages"][stage]["strict"] is False
            assert out["stages"][stage]["on_error"] == "continue"

    def test_preserves_explicit_stage_settings(self):
        data = _valid_minimal() | {
            "stages": {"generate": {"strict": True, "on_error": "halt"}}
        }
        out = apply_defaults(data)
        assert out["stages"]["generate"]["strict"] is True
        assert out["stages"]["generate"]["on_error"] == "halt"
        # other stages still get defaults
        assert out["stages"]["scaffold"]["strict"] is False

    def test_does_not_mutate_input(self):
        data = _valid_minimal()
        snapshot = {k: v for k, v in data.items()}
        apply_defaults(data)
        # input unchanged at top level and no new keys leaked in
        assert data == snapshot
        assert "stages" not in data
        assert "config" not in data


# ---------------------------------------------------------------
# generate_default()
# ---------------------------------------------------------------


class TestGenerateDefault:
    def test_minimum_required_fields(self):
        out = generate_default("MyProject", ["DEV"])
        assert out["project"] == "MyProject"
        assert out["environments"] == ["DEV"]
        assert "config" in out
        assert validate(out) == []

    def test_includes_version_by_default(self):
        out = generate_default("X", ["DEV"])
        assert out["version"] == "1.0"

    def test_version_can_be_omitted(self):
        out = generate_default("X", ["DEV"], version=None)
        assert "version" not in out

    def test_config_pointers_match_layer1(self):
        out = generate_default("X", ["DEV"])
        assert out["config"] == LAYER_1_DEFAULTS["config"]

    def test_does_not_pin_per_stage_settings(self):
        """Per-stage settings fall through to Layer 1 — keep file readable."""
        out = generate_default("X", ["DEV"])
        assert "stages" not in out

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValueError):
            generate_default("", ["DEV"])

    def test_non_string_project_rejected(self):
        with pytest.raises(ValueError):
            generate_default(123, ["DEV"])

    def test_empty_environments_rejected(self):
        with pytest.raises(ValueError):
            generate_default("X", [])

    def test_environments_not_list_rejected(self):
        with pytest.raises(ValueError):
            generate_default("X", "DEV")

    def test_environment_must_be_non_empty(self):
        with pytest.raises(ValueError):
            generate_default("X", ["DEV", ""])


# ---------------------------------------------------------------
# write_if_missing()
# ---------------------------------------------------------------


class TestWriteIfMissing:
    def test_writes_when_absent(self, tmp_path):
        p = tmp_path / "ships.yaml"
        wrote = write_if_missing(str(p), generate_default("X", ["DEV"]))
        assert wrote is True
        assert p.exists()

    def test_round_trip(self, tmp_path):
        p = tmp_path / "ships.yaml"
        original = generate_default("X", ["DEV", "PRD"])
        write_if_missing(str(p), original)
        loaded = load(str(p))
        assert loaded == original
        assert validate(loaded) == []

    def test_does_not_overwrite_existing(self, tmp_path):
        p = tmp_path / "ships.yaml"
        _write_yaml(str(p), "project: ExistingProject\nenvironments: [PRD]\n")
        wrote = write_if_missing(str(p), generate_default("X", ["DEV"]))
        assert wrote is False
        # Existing content untouched
        loaded = load(str(p))
        assert loaded["project"] == "ExistingProject"
        assert loaded["environments"] == ["PRD"]

    def test_does_not_overwrite_empty_existing_file(self, tmp_path):
        p = tmp_path / "ships.yaml"
        p.write_text("", encoding="utf-8")
        wrote = write_if_missing(str(p), generate_default("X", ["DEV"]))
        assert wrote is False
        assert p.read_text(encoding="utf-8") == ""

    def test_creates_parent_dir(self, tmp_path):
        p = tmp_path / "nested" / "deeper" / "ships.yaml"
        wrote = write_if_missing(str(p), generate_default("X", ["DEV"]))
        assert wrote is True
        assert p.exists()


# ---------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------


class TestMcpBlock:
    """Schema validation for the optional `mcp:` block."""

    BASE = {"project": "p", "environments": ["DEV"]}

    def _err(self, mcp_block, path_prefix="mcp"):
        from td_release_packager.orchestrator.ships_yaml import validate

        errors = validate({**self.BASE, "mcp": mcp_block})
        return [e for e in errors if e.path.startswith(path_prefix)]

    def test_empty_block_is_valid(self):
        assert self._err({}) == []

    def test_full_valid_block(self):
        assert (
            self._err(
                {
                    "transport": "streamable-http",
                    "host": "0.0.0.0",
                    "port": 8000,
                    "path": "/mcp",
                    "stateless": True,
                    "log_level": "INFO",
                }
            )
            == []
        )

    def test_must_be_mapping(self):
        errs = self._err("not-a-mapping")
        assert any(e.path == "mcp" for e in errs)

    def test_unknown_transport_rejected(self):
        errs = self._err({"transport": "websocket"})
        assert any("transport" in e.path for e in errs)

    def test_port_out_of_range_rejected(self):
        assert self._err({"port": 0})
        assert self._err({"port": 70000})

    def test_port_must_be_int_not_bool(self):
        assert self._err({"port": True})

    def test_path_must_start_with_slash(self):
        assert self._err({"path": "mcp"})
        assert self._err({"path": ""})

    def test_log_level_vocabulary(self):
        assert self._err({"log_level": "VERBOSE"})
        assert not self._err({"log_level": "DEBUG"})


class TestModuleInvariants:
    def test_token_pattern_is_built_in(self):
        assert TOKEN_PATTERN == "{{TOKEN}}"

    def test_layer1_covers_every_canonical_stage(self):
        for stage in STAGES:
            assert stage in LAYER_1_DEFAULTS["stages"]

    def test_layer1_stage_defaults_are_lenient(self):
        for stage in STAGES:
            assert LAYER_1_DEFAULTS["stages"][stage]["strict"] is False
            assert LAYER_1_DEFAULTS["stages"][stage]["on_error"] == "continue"


# ---------------------------------------------------------------
# Tests for inspect.warn_external_grants (renamed from warn_orphan_grants)
# ---------------------------------------------------------------

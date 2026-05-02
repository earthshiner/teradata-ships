"""
test_orchestrator_cascade.py — Tests for the five-layer Cascade
configuration resolver.

Covers:
    - Resolution precedence (5 → 4 → 3 → 2 → 1)
    - Missing layers handled (None layers ignored)
    - Dotted-path walking through nested dicts
    - get() fallback, has() probe, layer_for() introspection
    - SettingNotFound on miss
    - Layer constraints — values at the wrong layer raise
    - Source-path provenance recorded on every resolution
"""

from __future__ import annotations

import pytest

from td_release_packager.orchestrator import LAYER_1_DEFAULTS
from td_release_packager.orchestrator.cascade import (
    Cascade,
    CascadeConfigError,
    LayerSource,
    ResolvedSetting,
    SettingNotFound,
)


# ---------------------------------------------------------------
# Resolution precedence
# ---------------------------------------------------------------


class TestPrecedence:
    def test_layer5_cli_wins_over_everything(self):
        c = Cascade(
            defaults={"stages": {"generate": {"strict": False}}},
            template={"stages": {"generate": {"strict": False}}},
            project={"stages": {"generate": {"strict": False}}},
            env_properties={"stages": {"generate": {"strict": False}}},
            cli={"stages": {"generate": {"strict": True}}},
        )
        r = c.resolve("stages.generate.strict")
        assert r.value is True
        assert r.source == LayerSource.LAYER_5_CLI

    def test_layer4_env_wins_over_3_2_1(self):
        c = Cascade(
            defaults={"x": 1},
            template={"x": 2},
            project={"x": 3},
            env_properties={"x": 4},
        )
        r = c.resolve("x")
        assert r.value == 4
        assert r.source == LayerSource.LAYER_4_ENV

    def test_layer3_project_wins_over_2_1(self):
        c = Cascade(
            defaults={"x": 1},
            template={"x": 2},
            project={"x": 3},
        )
        r = c.resolve("x")
        assert r.value == 3
        assert r.source == LayerSource.LAYER_3_PROJECT

    def test_layer2_template_wins_over_1(self):
        c = Cascade(defaults={"x": 1}, template={"x": 2})
        r = c.resolve("x")
        assert r.value == 2
        assert r.source == LayerSource.LAYER_2_TEMPLATE

    def test_layer1_default_falls_through(self):
        c = Cascade(defaults={"x": 1})
        r = c.resolve("x")
        assert r.value == 1
        assert r.source == LayerSource.LAYER_1_DEFAULTS

    def test_layer_for_introspection(self):
        c = Cascade(
            defaults={"x": 1},
            project={"x": 99},
        )
        assert c.layer_for("x") == LayerSource.LAYER_3_PROJECT
        assert c.layer_for("missing") is None


# ---------------------------------------------------------------
# Missing layers / partial coverage
# ---------------------------------------------------------------


class TestPartialLayers:
    def test_none_layers_skipped(self):
        c = Cascade(
            defaults={"x": 1},
            template=None,
            project=None,
            env_properties=None,
            cli=None,
        )
        assert c.resolve("x").value == 1

    def test_higher_layer_without_setting_falls_through(self):
        # CLI defines an unrelated key; project still wins for x
        c = Cascade(
            defaults={"x": 1},
            project={"x": 2},
            cli={"y": 99},
        )
        r = c.resolve("x")
        assert r.value == 2
        assert r.source == LayerSource.LAYER_3_PROJECT

    def test_none_value_treated_as_unset(self):
        c = Cascade(
            defaults={"x": 1},
            project={"x": None},
        )
        # Project's explicit None doesn't shadow the default
        r = c.resolve("x")
        assert r.value == 1
        assert r.source == LayerSource.LAYER_1_DEFAULTS


# ---------------------------------------------------------------
# Dotted-path walking
# ---------------------------------------------------------------


class TestDottedPaths:
    def test_deep_path(self):
        c = Cascade(project={"a": {"b": {"c": "deep"}}})
        assert c.resolve("a.b.c").value == "deep"

    def test_partial_match_still_a_miss(self):
        c = Cascade(project={"a": {"b": {}}})
        with pytest.raises(SettingNotFound):
            c.resolve("a.b.c")

    def test_segment_into_non_dict(self):
        c = Cascade(project={"a": {"b": "leaf"}})
        with pytest.raises(SettingNotFound):
            c.resolve("a.b.c")

    def test_falsy_values_resolve(self):
        # 0, False, "" must all be findable — only None == unset
        c = Cascade(project={"zero": 0, "no": False, "empty": ""})
        assert c.resolve("zero").value == 0
        assert c.resolve("no").value is False
        assert c.resolve("empty").value == ""


# ---------------------------------------------------------------
# get() / has() / SettingNotFound
# ---------------------------------------------------------------


class TestGetHasMissing:
    def test_get_returns_default_on_miss(self):
        c = Cascade(defaults={})
        assert c.get("missing", default="fallback") == "fallback"

    def test_get_returns_value_on_hit(self):
        c = Cascade(project={"x": 42})
        assert c.get("x") == 42

    def test_has_true_on_hit(self):
        c = Cascade(project={"x": 1})
        assert c.has("x") is True

    def test_has_false_on_miss(self):
        c = Cascade(defaults={"x": 1})
        assert c.has("y") is False

    def test_resolve_raises_on_miss(self):
        c = Cascade(defaults={})
        with pytest.raises(SettingNotFound, match="no value for setting"):
            c.resolve("missing")


# ---------------------------------------------------------------
# Layer constraints
# ---------------------------------------------------------------


class TestLayerConstraints:
    def test_violation_raises_at_construction(self):
        # token VALUES must be at Layer 4 (env-properties) only
        with pytest.raises(CascadeConfigError, match="not permitted"):
            Cascade(
                project={"tokens": {"DOM_DB": "DEV01_DOM"}},
                layer_constraints={
                    "tokens.DOM_DB": [LayerSource.LAYER_4_ENV],
                },
            )

    def test_constraint_satisfied_when_value_at_allowed_layer(self):
        c = Cascade(
            env_properties={"tokens": {"DOM_DB": "DEV01_DOM"}},
            layer_constraints={
                "tokens.DOM_DB": [LayerSource.LAYER_4_ENV],
            },
        )
        assert c.resolve("tokens.DOM_DB").value == "DEV01_DOM"

    def test_constraint_satisfied_when_value_absent(self):
        # No layer defines the path → constraint trivially holds
        Cascade(
            project={"unrelated": True},
            layer_constraints={
                "tokens.DOM_DB": [LayerSource.LAYER_4_ENV],
            },
        )

    def test_constraint_with_multiple_allowed_layers(self):
        # Allow at Layer 3 OR Layer 4
        c = Cascade(
            project={"x": 1},
            layer_constraints={
                "x": [LayerSource.LAYER_3_PROJECT, LayerSource.LAYER_4_ENV],
            },
        )
        assert c.resolve("x").value == 1

    def test_constraint_error_message_lists_allowed_layers(self):
        with pytest.raises(CascadeConfigError) as exc_info:
            Cascade(
                cli={"tokens": "TOKEN_VALUE"},
                layer_constraints={
                    "tokens": [LayerSource.LAYER_4_ENV],
                },
            )
        msg = str(exc_info.value)
        assert "layer-4" in msg
        assert "layer-5" in msg


# ---------------------------------------------------------------
# Source paths / provenance
# ---------------------------------------------------------------


class TestSourcePaths:
    def test_default_source_paths_are_recorded(self):
        c = Cascade(defaults={"x": 1}, project={"y": 2})
        assert c.resolve("x").source_path == "default"
        assert c.resolve("y").source_path == "ships.yaml"

    def test_custom_source_paths_override_defaults(self):
        c = Cascade(
            project={"x": 1},
            source_paths={LayerSource.LAYER_3_PROJECT: "custom-ships.yaml"},
        )
        assert c.resolve("x").source_path == "custom-ships.yaml"

    def test_resolved_setting_immutable(self):
        c = Cascade(defaults={"x": 1})
        r = c.resolve("x")
        assert isinstance(r, ResolvedSetting)
        with pytest.raises(Exception):
            # frozen=True on the dataclass
            r.value = 99  # type: ignore[misc]


# ---------------------------------------------------------------
# Integration with LAYER_1_DEFAULTS
# ---------------------------------------------------------------


class TestLayer1DefaultsIntegration:
    def test_resolves_canonical_stages_from_defaults(self):
        c = Cascade(defaults=LAYER_1_DEFAULTS)
        for stage in (
            "scaffold",
            "harvest",
            "generate",
            "inspect",
            "analyse",
            "package",
            "ship",
        ):
            r = c.resolve(f"stages.{stage}.strict")
            assert r.value is False
            assert r.source == LayerSource.LAYER_1_DEFAULTS

    def test_project_overrides_default(self):
        c = Cascade(
            defaults=LAYER_1_DEFAULTS,
            project={"stages": {"generate": {"strict": True}}},
        )
        r = c.resolve("stages.generate.strict")
        assert r.value is True
        assert r.source == LayerSource.LAYER_3_PROJECT
        # Other stages still come from default
        r2 = c.resolve("stages.scaffold.strict")
        assert r2.source == LayerSource.LAYER_1_DEFAULTS

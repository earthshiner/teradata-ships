"""
test_decompose_database_names.py — Tests for the cascade-aware
literal-database-name decomposer (tools/decompose_database_names.py).

Covers:
    1. Input reader  — auto-detect names file vs token_map.conf
    2. Inference     — composition roots from various inputs
    3. Decomposition — each grammar variant (parent_node, leaf, node,
                       cross-instance, outlier)
    4. Collisions    — same token name from different literals
    5. Emitters      — properties + report
    6. CLI           — argparse, output paths, error returns
    7. Integration   — realistic GCFR-style input round-trips through
                       the SHIPS token engine
"""

from __future__ import annotations


import pytest

from td_release_packager import decomposer as decomp
from td_release_packager.token_engine import read_env_config


# ---------------------------------------------------------------
# Input reader
# ---------------------------------------------------------------


class TestReadInputFile:
    """Tests for read_input_file() — auto-detect format."""

    def test_reads_plain_names_file(self, tmp_path):
        f = tmp_path / "names.txt"
        f.write_text("PDE_DEV_00\nPDE_DEV_00_MDL_0_T\n", encoding="utf-8")

        names = decomp.read_input_file(str(f))

        assert names == ["PDE_DEV_00", "PDE_DEV_00_MDL_0_T"]

    def test_reads_token_map_conf_format(self, tmp_path):
        """token_map.conf format: extracts LHS only."""
        f = tmp_path / "token_map.conf"
        f.write_text(
            "PDE_DEV_00={{PARENT_NODE}}\nPDE_DEV_00_MDL_0_T={{MDL_T}}\n",
            encoding="utf-8",
        )

        names = decomp.read_input_file(str(f))

        assert names == ["PDE_DEV_00", "PDE_DEV_00_MDL_0_T"]

    def test_skips_blank_and_comment_lines(self, tmp_path):
        f = tmp_path / "names.txt"
        f.write_text(
            "# header comment\n\nPDE_DEV_00\n# trailing comment\nPDE_DEV_00_MDL_0_T\n",
            encoding="utf-8",
        )
        assert decomp.read_input_file(str(f)) == ["PDE_DEV_00", "PDE_DEV_00_MDL_0_T"]

    def test_dedupes_preserving_first_order(self, tmp_path):
        f = tmp_path / "names.txt"
        f.write_text(
            "PDE_DEV_00_MDL_0_T\nPDE_DEV_00\nPDE_DEV_00_MDL_0_T\n",
            encoding="utf-8",
        )
        assert decomp.read_input_file(str(f)) == [
            "PDE_DEV_00_MDL_0_T",
            "PDE_DEV_00",
        ]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            decomp.read_input_file(str(tmp_path / "nope.txt"))


# ---------------------------------------------------------------
# Composition-root inference
# ---------------------------------------------------------------


class TestInferCompositionRoots:
    """Tests for infer_composition_roots()."""

    def test_simple_three_segment_grammar(self):
        names = ["PDE_DEV_00_MDL_0_T", "PDE_DEV_00_GCFR_API"]
        roots, conf = decomp.infer_composition_roots(names)

        assert roots.env_prefix == "PDE"
        assert roots.ships_env == "DEV"
        assert roots.instance == "00"
        assert conf["env_prefix"] == "HIGH"
        assert conf["instance"] == "HIGH"

    def test_security_tier_inferred_from_leaf_position(self):
        """`_0_T` and `_0_V` patterns → SECURITY_TIER=0."""
        names = [
            "PDE_DEV_00_MDL_0_T",
            "PDE_DEV_00_MDL_0_V",
            "PDE_DEV_00_GCFR_STD_0_T",
        ]
        roots, _ = decomp.infer_composition_roots(names)

        assert roots.security_tier == "0"

    def test_no_instance_segment(self):
        """Names like A_D01_OMR_STD lack an instance — INSTANCE inferred empty."""
        names = ["A_D01_OMR_STD", "A_D01_OMR_SEM"]
        roots, conf = decomp.infer_composition_roots(names)

        # Common prefix is "A_D01" — but heuristic puts ENV_PREFIX=A,
        # SHIPS_ENV=D01. That's not perfect but it's deterministic;
        # the user can correct it in the generated file.
        assert roots.env_prefix == "A"
        assert roots.ships_env == "D01"
        assert roots.instance == ""
        assert conf["instance"] == "LOW"

    def test_outlier_does_not_block_instance_inference(self):
        """Mostly-conformant names with 1 outlier still infer roots correctly."""
        names = [
            "PDE_DEV_00_MDL_0_T",
            "PDE_DEV_00_MDL_0_V",
            "PDE_DEV_00_GCFR_API",
            "PDE_DEV_MDL",  # outlier — no instance
        ]
        roots, conf = decomp.infer_composition_roots(names)

        assert roots.instance == "00"
        # Common prefix ends after PDE_DEV; INSTANCE inference is
        # 3-of-4 (75%) → MEDIUM
        assert conf["instance"] in ("HIGH", "MEDIUM")

    def test_empty_input_returns_empty_roots(self):
        roots, conf = decomp.infer_composition_roots([])
        assert roots.env_prefix == ""
        assert all(v == "LOW" for v in conf.values())


# ---------------------------------------------------------------
# Per-name decomposition
# ---------------------------------------------------------------


class TestDecomposeName:
    """Tests for decompose_name()."""

    @pytest.fixture
    def roots(self):
        return decomp.CompositionRoots(
            env_prefix="PDE", ships_env="DEV", instance="00", security_tier="0"
        )

    def test_parent_node(self, roots):
        d = decomp.decompose_name("PDE_DEV_00", roots)
        assert d is not None
        assert d.is_parent_node
        assert d.token_name == "PARENT_NODE"
        assert d.cascade_form() == "{{ENV_PREFIX}}_{{SHIPS_ENV}}_{{INSTANCE}}"

    def test_full_leaf(self, roots):
        d = decomp.decompose_name("PDE_DEV_00_MDL_0_T", roots)
        assert d is not None
        assert d.layer == "MDL"
        assert d.security_tier == "0"
        assert d.kind == "T"
        assert d.has_instance is True
        assert d.token_name == "MDL_T"
        assert d.cascade_form() == "{{PARENT_NODE}}_MDL_{{SECURITY_TIER}}_T"

    def test_compound_layer(self, roots):
        """`GCFR_STD` is a single compound layer, not two segments."""
        d = decomp.decompose_name("PDE_DEV_00_GCFR_STD_0_T", roots)
        assert d.layer == "GCFR_STD"
        assert d.kind == "T"
        assert d.token_name == "GCFR_STD_T"

    def test_node_form_no_tier_no_kind(self, roots):
        d = decomp.decompose_name("PDE_DEV_00_GCFR_API", roots)
        assert d.layer == "GCFR_API"
        assert d.security_tier is None
        assert d.kind is None
        assert d.token_name == "GCFR_API_NODE"
        assert d.cascade_form() == "{{PARENT_NODE}}_GCFR_API"

    def test_cross_instance_form(self, roots):
        """`PDE_DEV_MDL` has no INSTANCE segment."""
        d = decomp.decompose_name("PDE_DEV_MDL", roots)
        assert d is not None
        assert d.has_instance is False
        assert d.layer == "MDL"
        assert d.cascade_form() == "{{ENV_PREFIX}}_{{SHIPS_ENV}}_MDL"

    def test_outlier_returns_none(self, roots):
        """Names not starting with the ENV_PREFIX_SHIPS_ENV prefix are outliers."""
        assert decomp.decompose_name("GCFR_APPL_ADMIN_USER", roots) is None
        assert decomp.decompose_name("OTHER_PROJECT_TBL", roots) is None


# ---------------------------------------------------------------
# Aggregate / collisions
# ---------------------------------------------------------------


class TestDecomposeAll:
    """Tests for decompose_all() — the orchestrator."""

    def test_collision_detected(self):
        """Two literals decomposing to the same token name are flagged."""
        names = [
            "PDE_DEV_00_MDL_0_T",
            "PDE_DEV_01_MDL_0_T",  # different instance, same layer/tier/kind
        ]
        result = decomp.decompose_all(names)
        # Both should decompose, both produce token_name 'MDL_T'
        # but only one matches the dominant INSTANCE; the other
        # is treated as has_instance=False (cross-instance form),
        # which still produces a different cascade form.
        # Either way collision detection is robust to either path.
        # Assert: collisions dict contains MDL_T iff two literals
        # share that name.
        names_per_token = {}
        for d in result.decomposed:
            names_per_token.setdefault(d.token_name, []).append(d.literal)
        if "MDL_T" in names_per_token and len(names_per_token["MDL_T"]) > 1:
            assert "MDL_T" in result.collisions

    def test_empty_input(self):
        result = decomp.decompose_all([])
        assert result.decomposed == []
        assert result.outliers == []
        assert result.collisions == {}


# ---------------------------------------------------------------
# Properties emitter
# ---------------------------------------------------------------


class TestFormatPropertiesFile:
    """Tests for format_properties_file()."""

    def test_emits_composition_roots(self):
        names = ["PDE_DEV_00", "PDE_DEV_00_MDL_0_T"]
        result = decomp.decompose_all(names)
        out = decomp.format_properties_file("DEV", result)

        assert "SHIPS_ENV=DEV" in out
        assert "ENV_PREFIX=PDE" in out
        assert "INSTANCE=00" in out
        assert "SECURITY_TIER=0" in out
        assert "PARENT_NODE={{ENV_PREFIX}}_{{SHIPS_ENV}}_{{INSTANCE}}" in out

    def test_emits_derived_names_in_cascade_form(self):
        names = ["PDE_DEV_00", "PDE_DEV_00_MDL_0_T"]
        result = decomp.decompose_all(names)
        out = decomp.format_properties_file("DEV", result)

        assert "MDL_T={{PARENT_NODE}}_MDL_{{SECURITY_TIER}}_T" in out

    def test_emits_outliers_with_literal_suffix(self):
        names = ["PDE_DEV_00_MDL_0_T", "GCFR_APPL_ADMIN_USER"]
        result = decomp.decompose_all(names)
        out = decomp.format_properties_file("DEV", result)

        assert "GCFR_APPL_ADMIN_USER_LITERAL=GCFR_APPL_ADMIN_USER" in out

    def test_renders_full_seven_section_scaffold(self):
        """Output includes all 7 canonical sections — sections 1
        and 2 populated, the rest empty placeholders."""
        names = ["PDE_DEV_00", "PDE_DEV_00_MDL_0_T"]
        result = decomp.decompose_all(names)
        out = decomp.format_properties_file("DEV", result)

        for n in range(1, 8):
            assert f"# {n}." in out, f"section {n} header missing"

    def test_outliers_land_in_section_8(self):
        """Outliers must appear below section 8 header, not
        interleaved with sections 1-2."""
        names = ["PDE_DEV_00_MDL_0_T", "EXTERNAL_USER"]
        result = decomp.decompose_all(names)
        out = decomp.format_properties_file("DEV", result)

        sec8_pos = out.find("# 8. Outliers")
        outlier_pos = out.find("EXTERNAL_USER_LITERAL=EXTERNAL_USER")
        assert sec8_pos > 0
        assert outlier_pos > sec8_pos

    def test_no_section_8_when_no_outliers(self):
        """If every name decomposes cleanly, section 8 is omitted."""
        names = ["PDE_DEV_00", "PDE_DEV_00_MDL_0_T", "PDE_DEV_00_MDL_0_V"]
        result = decomp.decompose_all(names)
        out = decomp.format_properties_file("DEV", result)

        assert "# 8." not in out

    def test_sections_3_through_7_remain_empty_placeholders(self):
        """Decomposer only fills sections 1-2; sections 3-7 stay as
        placeholders for the user to populate from other sources."""
        names = ["PDE_DEV_00", "PDE_DEV_00_MDL_0_T"]
        result = decomp.decompose_all(names)
        out = decomp.format_properties_file("DEV", result)

        # 5 empty sections (3, 4, 5, 6, 7) → 5 'no entries' hints
        assert out.count("no entries") == 5

    def test_collision_emits_warn_comment(self):
        # Two literals that decompose to the same token name
        names = [
            "PDE_DEV_00_MDL_0_T",
            "PDE_DEV_00_MDL_0_T",  # exact duplicate gets dedup'd by reader,
        ]
        # Use a different path to construct collision: same layer/kind/tier
        # but the reader dedupes exact duplicates, so we hand-craft:
        result = decomp.decompose_all(["PDE_DEV_00_MDL_0_T"])
        # Manually inject a duplicate decomposed entry
        result.decomposed.append(result.decomposed[0])
        result.collisions = {
            "MDL_T": [
                "PDE_DEV_00_MDL_0_T",
                "PDE_DEV_00_MDL_0_T",
            ]
        }
        out = decomp.format_properties_file("DEV", result)
        assert "# WARN collision" in out


# ---------------------------------------------------------------
# Report emitter
# ---------------------------------------------------------------


class TestFormatDecompositionReport:
    """Tests for format_decomposition_report()."""

    def test_report_contains_roots_table(self):
        names = ["PDE_DEV_00", "PDE_DEV_00_MDL_0_T"]
        result = decomp.decompose_all(names)
        report = decomp.format_decomposition_report("DEV", names, result)

        assert "# Decomposition Report — DEV" in report
        assert "ENV_PREFIX" in report
        assert "PDE" in report

    def test_report_lists_outliers(self):
        names = ["PDE_DEV_00", "GCFR_APPL_ADMIN_USER"]
        result = decomp.decompose_all(names)
        report = decomp.format_decomposition_report("DEV", names, result)

        assert "## Outliers" in report
        assert "GCFR_APPL_ADMIN_USER" in report


# ---------------------------------------------------------------
# CLI
# ---------------------------------------------------------------


class TestCLI:
    def test_writes_both_artefacts(self, tmp_path):
        names_file = tmp_path / "names.txt"
        names_file.write_text("PDE_DEV_00\nPDE_DEV_00_MDL_0_T\n", encoding="utf-8")

        rc = decomp.main(
            [str(names_file), "--env", "DEV", "--output-dir", str(tmp_path)]
        )
        assert rc == 0
        assert (tmp_path / "env" / "DEV.conf").exists()
        assert (tmp_path / "decomposition_report.md").exists()

    def test_missing_input_returns_nonzero(self, tmp_path, capsys):
        rc = decomp.main(
            [str(tmp_path / "nope.txt"), "--env", "DEV", "--output-dir", str(tmp_path)]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "Input file not found" in captured.err

    def test_empty_input_returns_nonzero(self, tmp_path, capsys):
        f = tmp_path / "empty.txt"
        f.write_text("# only comments\n", encoding="utf-8")
        rc = decomp.main([str(f), "--env", "DEV", "--output-dir", str(tmp_path)])
        assert rc == 1
        captured = capsys.readouterr()
        assert "no literal database names" in captured.err


# ---------------------------------------------------------------
# Integration — realistic GCFR-style input
# ---------------------------------------------------------------


# 39 literal database names extracted from a typical GCFR project
# (all the database-name values from Paul's original sed list).
_REALISTIC_GCFR_NAMES = [
    "PDE_DEV_00",
    "PDE_DEV_MDL",
    "PDE_DEV_00_MDL_0_T",
    "PDE_DEV_00_MDL_0_V",
    "PDE_DEV_00_GCFR",
    "PDE_DEV_00_GCFR_STD_0_T",
    "PDE_DEV_00_GCFR_STD_0_V",
    "PDE_DEV_00_GCFR_STD_0_M",
    "PDE_DEV_00_GCFR_API",
    "PDE_DEV_00_GCFR_BBP_0_P",
    "PDE_DEV_00_GCFR_FFP_0_P",
    "PDE_DEV_00_GCFR_UTP_0_P",
    "PDE_DEV_00_GCFR_CPP_0_P",
    "PDE_DEV_00_GCFR_PPP_0_P",
    "PDE_DEV_00_TMP",
    "PDE_DEV_00_TMP_0_T",
    "PDE_DEV_00_WRK_0_T",
    "PDE_DEV_00_STG",
    "PDE_DEV_00_STG_0_T",
    "PDE_DEV_00_STG_0_V",
    "PDE_DEV_00_SRC",
    "PDE_DEV_00_SRC_0_T",
    "PDE_DEV_00_SRC_0_V",
    "PDE_DEV_00_UTL",
    "PDE_DEV_00_UTL_0_T",
    "PDE_DEV_00_UTL_0_V",
    "PDE_DEV_00_TFM",
    "PDE_DEV_00_INP_0_V",
    "PDE_DEV_00_OUT_0_V",
    "PDE_DEV_00_SEM",
    "PDE_DEV_00_SEM_0_T",
    "PDE_DEV_00_SEM_0_V",
    "PDE_DEV_00_OPR",
    "PDE_DEV_00_GCFR_OPR_0_T",
    "PDE_DEV_00_GCFR_OPR_0_V",
    "PDE_DEV_00_GCFR_OPR_0_M",
    "PDE_DEV_00_GCFR_ETL_USR",
    "PDE_DEV_00_GCFR_ETL_USR_ROLE",
    "GCFR_APPL_ADMIN_USER",
]


class TestRealisticGCFRRoundTrip:
    """End-to-end: realistic input round-trips through the SHIPS engine."""

    def test_inference_matches_known_grammar(self):
        result = decomp.decompose_all(_REALISTIC_GCFR_NAMES)

        assert result.roots.env_prefix == "PDE"
        assert result.roots.ships_env == "DEV"
        assert result.roots.instance == "00"
        assert result.roots.security_tier == "0"
        assert result.confidence["env_prefix"] == "HIGH"
        # 1 outlier (GCFR_APPL_ADMIN_USER) and 1 cross-instance
        # name (PDE_DEV_MDL) but the dominant pattern still wins
        assert result.confidence["instance"] in ("HIGH", "MEDIUM")

    def test_outliers_correctly_identified(self):
        result = decomp.decompose_all(_REALISTIC_GCFR_NAMES)
        assert "GCFR_APPL_ADMIN_USER" in result.outliers

    def test_parent_node_decomposed_correctly(self):
        result = decomp.decompose_all(_REALISTIC_GCFR_NAMES)
        parent = next(d for d in result.decomposed if d.is_parent_node)
        assert parent.literal == "PDE_DEV_00"
        assert parent.token_name == "PARENT_NODE"

    def test_emitted_properties_loads_through_token_engine(self, tmp_path):
        """The generated .conf file resolves cleanly end-to-end,
        and the resolved values match the original literals."""
        names_file = tmp_path / "names.txt"
        names_file.write_text("\n".join(_REALISTIC_GCFR_NAMES) + "\n", encoding="utf-8")

        rc = decomp.main(
            [str(names_file), "--env", "DEV", "--output-dir", str(tmp_path)]
        )
        assert rc == 0

        props_path = tmp_path / "env" / "DEV.conf"
        tokens = read_env_config(str(props_path))

        # Composition roots survived
        assert tokens["ENV_PREFIX"] == "PDE"
        assert tokens["SHIPS_ENV"] == "DEV"
        assert tokens["INSTANCE"] == "00"

        # PARENT_NODE resolved through cascade
        assert tokens["PARENT_NODE"] == "PDE_DEV_00"

        # A leaf token resolved through cascade
        assert tokens["MDL_T"] == "PDE_DEV_00_MDL_0_T"
        assert tokens["GCFR_STD_T"] == "PDE_DEV_00_GCFR_STD_0_T"

        # Compound-layer node (no tier, no kind)
        assert tokens["GCFR_API_NODE"] == "PDE_DEV_00_GCFR_API"

        # Cross-instance form
        assert tokens["MDL_NODE"] == "PDE_DEV_MDL"

        # Outlier kept as literal fallback
        assert tokens["GCFR_APPL_ADMIN_USER_LITERAL"] == "GCFR_APPL_ADMIN_USER"

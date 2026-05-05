"""
test_properties_scaffold.py — Tests for the shared ``.properties``
scaffold renderer (``td_release_packager.properties_scaffold``).

The scaffold is consumed by ``legacy_importer`` and ``decomposer``;
both rely on the same canonical 7-section layout. These tests pin
the structure so a rename, reordering, or section addition is a
deliberate change with one place to update.
"""

from __future__ import annotations


from td_release_packager import properties_scaffold as scaffold
from td_release_packager.token_engine import read_env_config


# ---------------------------------------------------------------
# Canonical structure
# ---------------------------------------------------------------


class TestSectionsConstant:
    def test_seven_sections_in_order(self):
        """The canonical layout is exactly seven sections, numbered 1-7."""
        assert len(scaffold.SECTIONS) == 7
        for i, section in enumerate(scaffold.SECTIONS, start=1):
            assert section.number == i

    def test_each_section_has_a_title(self):
        for section in scaffold.SECTIONS:
            assert section.title
            assert isinstance(section.title, str)

    def test_titles_are_unique(self):
        titles = [s.title for s in scaffold.SECTIONS]
        assert len(set(titles)) == len(titles)

    def test_canonical_titles_present(self):
        """Renaming a canonical section is a deliberate change."""
        titles = [s.title for s in scaffold.SECTIONS]
        assert titles == [
            "Composition roots",
            "Derived database names",
            "Users & roles",
            "SQL constants",
            "Engine / runtime flags",
            "Field-length policy",
            "Diagnostic / DI-tool stanzas",
        ]


# ---------------------------------------------------------------
# Renderer behaviour
# ---------------------------------------------------------------


class TestRenderScaffold:
    def test_emits_all_seven_section_headers(self):
        out = scaffold.render_scaffold(
            env="DEV",
            generator_label="test",
            source_label="test input",
            next_steps=["1. Do thing"],
            sections_content={},
        )
        for section in scaffold.SECTIONS:
            expected = f"# {section.number}. {section.title}"
            assert expected in out, f"section header missing: {expected}"

    def test_empty_sections_carry_hint_comment(self):
        out = scaffold.render_scaffold(
            env="DEV",
            generator_label="test",
            source_label="test input",
            next_steps=[],
            sections_content={},
        )
        # The hint comment guides the user — must appear at least once
        assert "no entries" in out
        assert "Imported section" in out

    def test_populated_section_replaces_hint(self):
        out = scaffold.render_scaffold(
            env="DEV",
            generator_label="test",
            source_label="test input",
            next_steps=[],
            sections_content={1: "FOO=bar\nBAZ=qux"},
        )
        assert "FOO=bar" in out
        assert "BAZ=qux" in out
        # The empty hint should no longer appear for section 1, but
        # SHOULD still appear for the other 6 unfilled sections
        assert out.count("no entries") == 6

    def test_grammar_header_rendered(self):
        out = scaffold.render_scaffold(
            env="DEV",
            generator_label="test",
            source_label="test input",
            next_steps=[],
            sections_content={},
        )
        assert "Naming grammar" in out
        assert "{{ENV_PREFIX}}_{{SHIPS_ENV}}_{{INSTANCE}}" in out

    def test_env_appears_in_header(self):
        out = scaffold.render_scaffold(
            env="PRD",
            generator_label="test",
            source_label="x",
            next_steps=[],
            sections_content={},
        )
        assert "PRD.conf" in out

    def test_source_label_appears_in_header(self):
        out = scaffold.render_scaffold(
            env="DEV",
            generator_label="test",
            source_label="my legacy script",
            next_steps=[],
            sections_content={},
        )
        assert "Source: my legacy script" in out

    def test_next_steps_rendered_as_comments(self):
        out = scaffold.render_scaffold(
            env="DEV",
            generator_label="test",
            source_label="x",
            next_steps=["Step one", "Step two"],
            sections_content={},
        )
        assert "# Step one" in out
        assert "# Step two" in out

    def test_final_section_appended_when_provided(self):
        out = scaffold.render_scaffold(
            env="DEV",
            generator_label="test",
            source_label="x",
            next_steps=[],
            sections_content={},
            final_section_title="Imported (UNCATEGORISED)",
            final_section_purpose=["dump section"],
            final_section_content="A=1\nB=2",
        )
        assert "# 8. Imported (UNCATEGORISED)" in out
        assert "A=1" in out
        assert "B=2" in out
        # Position: section 8 must come AFTER section 7
        sec7 = out.find("# 7.")
        sec8 = out.find("# 8.")
        assert 0 < sec7 < sec8

    def test_no_final_section_when_omitted(self):
        out = scaffold.render_scaffold(
            env="DEV",
            generator_label="test",
            source_label="x",
            next_steps=[],
            sections_content={},
        )
        assert "# 8." not in out

    def test_output_loads_through_token_engine(self, tmp_path):
        """End-to-end: scaffold output (with content in section 1)
        round-trips through read_env_config() without errors —
        proving the section comments don't accidentally parse as
        properties."""
        out = scaffold.render_scaffold(
            env="DEV",
            generator_label="test",
            source_label="x",
            next_steps=["Step"],
            sections_content={
                1: "SHIPS_ENV=DEV\nENV_PREFIX=PDE\nINSTANCE=00\nSECURITY_TIER=0",
                2: "PARENT_NODE={{ENV_PREFIX}}_{{SHIPS_ENV}}_{{INSTANCE}}",
            },
            final_section_title="Imported (UNCATEGORISED)",
            final_section_purpose=["dump"],
            final_section_content="ADMIN=alice\nTS_TYPE=TIMESTAMP(6)",
        )

        f = tmp_path / "DEV.conf"
        f.write_text(out, encoding="utf-8")

        tokens = read_env_config(str(f))
        assert tokens["SHIPS_ENV"] == "DEV"
        assert tokens["ENV_PREFIX"] == "PDE"
        assert tokens["PARENT_NODE"] == "PDE_DEV_00"
        assert tokens["ADMIN"] == "alice"
        assert tokens["TS_TYPE"] == "TIMESTAMP(6)"

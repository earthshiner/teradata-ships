"""
Unit tests for td_release_packager.provenance_renderer.

Exercises the HTML rendering path. Because the report is a static
build artefact, the contract here is mostly structural — the right
elements with the right CSS classes appear in the output, user
content is HTML-escaped, and the renderer fails loudly on
malformed input.
"""

import pytest

from database_package_deployer.provenance import (
    ProvenanceChain,
    Stage,
    Status,
)
from database_package_deployer.provenance_renderer import (
    PROVENANCE_CSS,
    render_chain,
)


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------


def _happy_chain() -> ProvenanceChain:
    """A typical eponymous-rename chain — exercises 3/4 statuses."""
    c = ProvenanceChain()
    c.add(
        Stage(
            "source",
            "domain/tables/MortgagePlatform_Domain_Mortgage.tbl",
            Status.APPLIED,
        )
    )
    c.add(
        Stage(
            "eponymous",
            "domain/tables/P_MP_DOM_T.Mortgage.tbl",
            Status.APPLIED,
            "Renamed from DDL",
        )
    )
    c.add(
        Stage(
            "token_resolved",
            "domain/tables/P_MP_DOM_T.Mortgage.tbl",
            Status.NO_OP,
            "no tokens",
        )
    )
    c.add(
        Stage(
            "package",
            "03_tables/P_MP_DOM_T.Mortgage.tbl",
            Status.APPLIED,
        )
    )
    return c


def _failed_chain() -> ProvenanceChain:
    """A chain where eponymous rename failed — used to verify the
    failed-row highlight applies."""
    c = ProvenanceChain()
    c.add(Stage("source", "x.viw", Status.APPLIED))
    c.add(
        Stage(
            "eponymous",
            "x.viw",
            Status.FAILED,
            "Could not parse CREATE VIEW header",
        )
    )
    c.add(
        Stage(
            "token_resolved",
            "x.viw",
            Status.SKIPPED,
            "Upstream stage failed",
        )
    )
    c.add(Stage("package", "04_views/x.viw", Status.APPLIED))
    return c


# -------------------------------------------------------------------
# Structure — output is well-formed and contains expected elements
# -------------------------------------------------------------------


class TestRenderStructure:
    """The output has the right HTML shape."""

    def test_returns_details_element(self):
        """Top-level element is <details> for native collapse."""
        out = render_chain(_happy_chain())
        assert out.startswith("<details")
        assert out.endswith("</details>")

    def test_contains_summary(self):
        """Drill-down has a <summary> child for the click target."""
        out = render_chain(_happy_chain())
        assert "<summary" in out
        assert "</summary>" in out

    def test_contains_table_with_four_rows(self):
        """Body table has one row per stage — always four."""
        out = render_chain(_happy_chain())
        # Count <tr> in tbody by counting tr opening tags after <tbody>
        body = out.split("<tbody>")[1].split("</tbody>")[0]
        assert body.count("<tr") == 4

    def test_contains_all_status_pills(self):
        """All four status classes are referenced in the renderer."""
        # Render two chains and check the union covers all four
        out = render_chain(_happy_chain()) + render_chain(_failed_chain())
        for status in ("applied", "no_op", "skipped", "failed"):
            assert f"ships-prov-status {status}" in out, (
                f"Status pill class for '{status}' missing"
            )

    def test_contains_all_stage_labels(self):
        """All four canonical stages appear in the rendered output."""
        out = render_chain(_happy_chain())
        for label in (
            "Source",
            "Eponymous rename",
            "Token substitution",
            "Package path",
        ):
            assert label in out, f"Stage label '{label}' missing"

    def test_failed_row_has_highlight_class(self):
        """Failed stages get the stage-failed row class for CSS hook."""
        out = render_chain(_failed_chain())
        assert "stage-failed" in out


# -------------------------------------------------------------------
# Escaping — user content cannot inject HTML
# -------------------------------------------------------------------


class TestEscaping:
    """Paths and notes are HTML-escaped — no XSS via filenames."""

    def test_path_with_angle_brackets_escaped(self):
        c = ProvenanceChain()
        c.add(Stage("source", "<script>alert(1)</script>.tbl", Status.APPLIED))
        c.add(Stage("eponymous", "x.tbl", Status.APPLIED, "renamed"))
        c.add(Stage("token_resolved", "x.tbl", Status.NO_OP, "n/a"))
        c.add(Stage("package", "03_tables/x.tbl", Status.APPLIED))

        out = render_chain(c)
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_note_with_quotes_escaped(self):
        c = ProvenanceChain()
        c.add(Stage("source", "x.tbl", Status.APPLIED))
        c.add(
            Stage(
                "eponymous",
                "x.tbl",
                Status.NO_OP,
                'note with "quotes" and <tags>',
            )
        )
        c.add(Stage("token_resolved", "x.tbl", Status.NO_OP, "n/a"))
        c.add(Stage("package", "03_tables/x.tbl", Status.APPLIED))

        out = render_chain(c)
        assert "<tags>" not in out
        assert "&lt;tags&gt;" in out


# -------------------------------------------------------------------
# Visual compactness — unchanged paths fold to "↑ unchanged"
# -------------------------------------------------------------------


class TestPathFolding:
    """When a stage doesn't change the path, the renderer shows
    '↑ unchanged' rather than repeating the same string. Verifies
    the visual-compactness contract."""

    def test_repeated_path_folded(self):
        out = render_chain(_happy_chain())
        # The token_resolved stage repeats the eponymous path
        # (P_MP_DOM_T.Mortgage.tbl) so should fold.
        assert "↑ unchanged" in out

    def test_first_stage_never_folded(self):
        """The source stage has no previous path, so it's shown in
        full even on a chain where downstream stages fold."""
        out = render_chain(_happy_chain())
        # Source path appears in full (not folded)
        assert "MortgagePlatform_Domain_Mortgage.tbl" in out


# -------------------------------------------------------------------
# Failure paths
# -------------------------------------------------------------------


class TestRendererFailures:
    """The renderer fails loudly on bad input rather than producing
    half-finished HTML."""

    def test_incomplete_chain_rejected(self):
        c = ProvenanceChain()
        c.add(Stage("source", "x.tbl", Status.APPLIED))
        with pytest.raises(ValueError, match="incomplete"):
            render_chain(c)

    def test_empty_chain_rejected(self):
        c = ProvenanceChain()
        with pytest.raises(ValueError, match="incomplete"):
            render_chain(c)


# -------------------------------------------------------------------
# CSS — included once, scoped to avoid host-page collision
# -------------------------------------------------------------------


class TestCSS:
    """The CSS block is well-formed and uses brand colours."""

    def test_css_uses_brand_colours(self):
        """Teradata Orange #FF5F02 and Navy #00233C must be present."""
        assert "#FF5F02" in PROVENANCE_CSS
        assert "#00233C" in PROVENANCE_CSS

    def test_css_uses_inter_font(self):
        """Inter is the brand font."""
        assert "Inter" in PROVENANCE_CSS

    def test_css_scoped_under_ships_prov(self):
        """All selectors are scoped under .ships-prov to avoid
        collision with the host page's existing styles."""
        # Check that there are no top-level selectors like 'table'
        # or 'details' that could affect the rest of the page.
        # Every selector should be prefixed with .ships-prov.
        # Strip comments first
        css_no_comments = PROVENANCE_CSS
        while "/*" in css_no_comments:
            start = css_no_comments.index("/*")
            end = css_no_comments.index("*/", start) + 2
            css_no_comments = css_no_comments[:start] + css_no_comments[end:]

        # Find each rule (selector { ... })
        for line in css_no_comments.split("{"):
            line = line.strip()
            if not line or "}" not in line:
                # Selector lines (before the brace)
                # The actual selector is the last segment before {
                continue
            # Extract just the selector portion before any rules
            selector_part = line.split("}")[-1].strip()
            if selector_part and not selector_part.startswith("/*"):
                assert "ships-prov" in selector_part, (
                    f"Unscoped selector found: '{selector_part}'"
                )

"""
test_wave_svg_layout.py — Layout invariants for the pipeline-report wave SVG.

Covers three improvements:
  1. Legend renders BEFORE the SVG, not after.
  2. Each column sizes to its own content (shorter waves no longer leave
     a tall whitespace tail under the items).
  3. Object names display in a cell wide enough to keep tokenised
     qualified names intact (was truncating ``{{DB_PREFIX}}_DOM_STD_T.x``
     to ``{{DB_PREFIX}}_DOM_S…``).
"""

from __future__ import annotations

import re

from td_release_packager.reporting.waves import render_wave_svg


def _records(*specs):
    """Build payload-record dicts: ``("name", "type", wave)`` per spec."""
    return [{"name": n, "type": t, "wave": w} for n, t, w in specs]


class TestLayout:
    def test_legend_renders_before_svg(self):
        html = render_wave_svg(
            _records(
                ("MyDB.Foo", "TABLE", 1),
                ("MyDB.Bar", "VIEW", 2),
            )
        )
        legend_pos = html.find("border-radius:50%")
        svg_pos = html.find("<svg ")
        assert legend_pos != -1, "legend missing"
        assert svg_pos != -1, "svg missing"
        assert legend_pos < svg_pos, "legend should render before the SVG, not after it"

    def test_columns_size_independently_no_trailing_whitespace(self):
        """A 3-item Serial column should be far shorter than a 41-item Wave.

        Before #466's layout pass every column was sized to ``max_items``
        and the short columns showed a tall whitespace tail.
        """
        records = _records(*[(f"a.r{i}", "ROLE", None) for i in range(3)])
        for i in range(41):
            records.append({"name": f"db.tbl{i}", "type": "TABLE", "wave": 1})

        html = render_wave_svg(records)
        # Each column emits one wrapper rect with width="260" — capture
        # height of every one.
        wrapper_rects = re.findall(r'<rect[^/]*width="260"[^/]*height="(\d+)"', html)
        # We're after the OUTER backgrounds (not the header strip or
        # 6-pixel underline). The outer ones are the two largest values.
        outer_heights = sorted(set(int(h) for h in wrapper_rects), reverse=True)
        # There must be at least two distinct column heights — the
        # earlier "max_items everywhere" layout would collapse them.
        assert len(outer_heights) >= 2, (
            f"all columns share the same height ({outer_heights!r}) — "
            "the short Serial column should be smaller than the wave column"
        )

    def test_long_tokenised_name_renders_with_full_or_near_full_text(self):
        """Token+suffix qualified names should fit the wider cell.

        Before the cell-width bump, ``{{DB_PREFIX}}_DOM_STD_T.customer_keymap``
        truncated to ``{{DB_PREFIX}}_DOM_S…`` (20 chars). With the cell
        now 260px wide and the truncation lifted to 36 chars, more of the
        name shows up directly in the visible text; the SVG <title> still
        carries the full name for hover.
        """
        long_name = "{{DB_PREFIX}}_DOM_STD_T.customer_keymap"  # 41 chars
        html = render_wave_svg(
            _records(
                (long_name, "TABLE", 1),
            )
        )
        # Hover tooltip always carries the full name.
        assert f"<title>{long_name}</title>" in html
        # The visible body shows at least the database-token portion past
        # the closing ``}}``, not just ``{{DB_PREFIX}}_DOM_S…``.
        assert "{{DB_PREFIX}}_DOM_STD_T" in html

    def test_no_records_returns_friendly_note(self):
        # Defensive — no records means analyse hasn't run yet, render a
        # readable note rather than an empty page.
        html = render_wave_svg([])
        assert "No wave data" in html
        assert "<svg" not in html

    def test_more_indicator_appears_when_over_cap(self):
        """Columns are capped at 40 items with a "... N more" footer."""
        records = [{"name": f"db.t{i}", "type": "TABLE", "wave": 1} for i in range(45)]
        html = render_wave_svg(records)
        assert "… 5 more" in html

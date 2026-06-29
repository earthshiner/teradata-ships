"""
test_issue_code_glossary.py — Tooltip + glossary rendering for HTML reports.

Locks in two surfaces that consume the central ``issue_codes.ISSUE_CODES``
registry:

  1. ``render_issue_list()`` decorates every issue code in the issues
     detail block with a ``title="…"`` attribute carrying the registry's
     description, so a reader can hover any code in any report tab to see
     what it means.
  2. ``render_issue_code_glossary()`` emits a per-domain glossary block
     ready to drop into the Guide tab. Every registered code appears
     exactly once, grouped by stage prefix (Harvest / Inspect / Analyse /
     Generate / Package / Token / Properties).

Both reports — the pre-package pipeline report and the sealed package
report — pull from the same renderer, so a new code only has to be
registered once.
"""

from __future__ import annotations

from td_release_packager.orchestrator.issue_codes import ISSUE_CODES
from td_release_packager.reporting.common import (
    render_issue_code_glossary,
    render_issue_list,
)


# ---------------------------------------------------------------------------
# render_issue_list — per-issue tooltips
# ---------------------------------------------------------------------------


class TestIssueListTooltips:
    """Each registered code gets a ``title=…`` attribute on its code span."""

    def test_known_code_carries_title_attribute(self):
        html = render_issue_list(
            [{"severity": "info", "code": "ANALYSE_EXTERNAL_REF", "message": "x"}]
        )
        assert "ANALYSE_EXTERNAL_REF" in html
        assert 'title="' in html
        # Description should be visible (first few chars survive escaping).
        assert "A DDL object references another object" in html

    def test_unregistered_code_has_no_title_attribute(self):
        """Codes not in the registry don't get an empty/misleading tooltip."""
        html = render_issue_list(
            [{"severity": "info", "code": "AD_HOC_TEST_CODE", "message": "x"}]
        )
        assert "AD_HOC_TEST_CODE" in html
        assert 'title="(unregistered code)"' not in html

    def test_dotted_underline_signals_hoverable(self):
        """Visually flag the code so readers know it's hoverable."""
        html = render_issue_list(
            [{"severity": "warning", "code": "HARVEST_TOKEN_CANDIDATE", "message": "y"}]
        )
        assert "border-bottom:1px dotted" in html
        assert "cursor:help" in html

    def test_empty_list_no_issues_message(self):
        html = render_issue_list([])
        assert "No issues recorded." in html


# ---------------------------------------------------------------------------
# render_issue_code_glossary — Guide tab catalogue
# ---------------------------------------------------------------------------


class TestIssueCodeGlossary:
    """The Guide tab's catalogue covers every registered code, once."""

    def test_every_registered_code_appears(self):
        html = render_issue_code_glossary()
        for code in ISSUE_CODES:
            assert code in html, f"{code} missing from glossary"

    def test_each_description_appears(self):
        html = render_issue_code_glossary()
        for description in ISSUE_CODES.values():
            # Match the leading prose — descriptions are too long to
            # check verbatim end-to-end after HTML escaping.
            opener = description[:40]
            # Some descriptions contain literal backticks which survive
            # escaping but `<`/`>` would not — pick safe openers.
            assert opener in html, f"description prefix not found: {opener!r}"

    def test_codes_grouped_by_domain_heading(self):
        html = render_issue_code_glossary()
        # Domains that have at least one code today.
        for label in ("Harvest", "Inspect", "Analyse", "Generate", "Package", "Token"):
            assert f"{label} codes" in html, f"missing domain heading: {label}"

    def test_per_condition_grant_codes_present(self):
        """The post-#451 per-condition codes appear, not just the deprecated alias."""
        html = render_issue_code_glossary()
        for code in (
            "INSPECT_GRANT_AUTO_GENERATED",
            "INSPECT_GRANT_EXTERNAL",
            "INSPECT_GRANT_MISSING",
            "INSPECT_GRANT_DRIFT",
        ):
            assert code in html

    def test_package_integrity_code_present(self):
        """The post-#452 package-integrity code is in the catalogue."""
        html = render_issue_code_glossary()
        assert "INSPECT_PACKAGE_INTEGRITY" in html

    def test_each_code_appears_exactly_once(self):
        """No duplicates across domains."""
        html = render_issue_code_glossary()
        for code in ISSUE_CODES:
            # The code text itself is what we count — descriptions may
            # reference other code names by prose, so we use the
            # ``<code …>CODE</code>`` shape that wraps each glossary entry.
            wrapped = f">{code}</code>"
            assert html.count(wrapped) == 1, (
                f"{code} appears {html.count(wrapped)} times in glossary"
            )

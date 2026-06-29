"""
test_perm_footprint.py — Allocated PERM-space scan (#473).

The pre-package pipeline report surfaces the floor the parent
database must have free before deploy. This module's tests pin down
the parsing, aggregation, and unresolved-token handling that drive
that card.
"""

from __future__ import annotations

from td_release_packager.perm_footprint import compute_perm_footprint


def _make_prereqs(project, *, databases=None, users=None):
    """Write a prereq tree under ``<project>/payload/database/pre-requisites/``.

    ``databases`` / ``users`` are dicts of ``filename → DDL body``.
    """
    db_dir = project / "payload" / "database" / "pre-requisites" / "databases"
    user_dir = project / "payload" / "database" / "pre-requisites" / "users"
    db_dir.mkdir(parents=True, exist_ok=True)
    user_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (databases or {}).items():
        (db_dir / name).write_text(body, encoding="utf-8")
    for name, body in (users or {}).items():
        (user_dir / name).write_text(body, encoding="utf-8")


class TestComputePermFootprint:
    def test_literal_perm_with_suffixes_aggregates_correctly(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        _make_prereqs(
            project,
            databases={
                "Foo.db": "CREATE DATABASE Foo FROM Root AS PERM = 1G;\n",
                "Bar.db": "CREATE DATABASE Bar FROM Root AS PERM = 512M;\n",
                "Baz.db": "CREATE DATABASE Baz FROM Other AS PERM = 100K;\n",
            },
        )
        fp = compute_perm_footprint(str(project))
        assert fp.db_count == 3
        assert fp.user_count == 0
        # 1 GB + 512 MB + 100 KB
        assert fp.total_bytes == (1024**3 + 512 * 1024**2 + 100 * 1024)
        # Two parents: Root with 2 children, Other with 1 child.
        parents = {p.parent_name: p for p in fp.by_parent}
        assert parents["Root"].child_count == 2
        assert parents["Root"].total_bytes == (1024**3 + 512 * 1024**2)
        assert parents["Other"].child_count == 1

    def test_tokenised_perm_excluded_from_total_and_flagged(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        _make_prereqs(
            project,
            databases={
                "Concrete.db": "CREATE DATABASE Concrete FROM Root AS PERM = 1G;\n",
                "Tokenised.db": "CREATE DATABASE Tokenised FROM Root AS PERM = {{PERM_DEV}};\n",
            },
        )
        fp = compute_perm_footprint(str(project))
        assert fp.total_bytes == 1024**3  # Concrete only
        assert len(fp.unresolved) == 1
        assert fp.unresolved[0].child_name == "Tokenised"
        assert fp.unresolved[0].tokenised_perm is True
        # The unresolved entry doesn't contribute to its parent's total
        parents = {p.parent_name: p for p in fp.by_parent}
        assert parents["Root"].total_bytes == 1024**3
        assert parents["Root"].child_count == 1

    def test_missing_perm_clause_treated_as_zero(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        _make_prereqs(
            project,
            databases={
                # No PERM clause — Teradata default is 0.
                "BareDb.db": "CREATE DATABASE BareDb FROM Root;\n",
            },
        )
        fp = compute_perm_footprint(str(project))
        assert fp.db_count == 1
        assert fp.total_bytes == 0
        assert fp.unresolved == []

    def test_user_files_counted_separately(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        _make_prereqs(
            project,
            databases={
                "Foo.db": "CREATE DATABASE Foo FROM Root AS PERM = 100M;\n",
            },
            users={
                "Bob.usr": "CREATE USER Bob FROM Root AS PERM = 50M, PASSWORD=...;\n",
            },
        )
        fp = compute_perm_footprint(str(project))
        assert fp.db_count == 1
        assert fp.user_count == 1
        # Both counted in total.
        assert fp.total_bytes == 150 * 1024**2

    def test_no_prereq_tree_returns_empty_footprint(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        # Payload but no pre-requisites/ dir.
        (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
        fp = compute_perm_footprint(str(project))
        assert fp.db_count == 0
        assert fp.user_count == 0
        assert fp.total_bytes == 0
        assert fp.per_database == []
        assert fp.by_parent == []

    def test_database_without_from_clause_still_captured(self, tmp_path):
        """``CREATE DATABASE X AS PERM = N;`` (no FROM) is legal Teradata —
        the database lands under whatever logged-in user creates it. We
        still record the child + perm, just with no parent rollup."""
        project = tmp_path / "proj"
        project.mkdir()
        _make_prereqs(
            project,
            databases={
                "Standalone.db": "CREATE DATABASE Standalone AS PERM = 256M;\n",
            },
        )
        fp = compute_perm_footprint(str(project))
        assert fp.db_count == 1
        assert fp.total_bytes == 256 * 1024**2
        assert fp.per_database[0].parent_name is None
        # No parent → nothing in by_parent.
        assert fp.by_parent == []


class TestPipelineReportRenderedCard:
    """End-to-end: the Payload tab's rendered HTML carries the card."""

    def test_card_renders_with_total_and_by_parent(self, tmp_path):
        from td_release_packager.reporting.pipeline_report import (
            _render_perm_footprint_card,
        )

        project = tmp_path / "proj"
        project.mkdir()
        _make_prereqs(
            project,
            databases={
                "Foo.db": "CREATE DATABASE Foo FROM Root AS PERM = 1G;\n",
                "Bar.db": "CREATE DATABASE Bar FROM Other AS PERM = 100M;\n",
            },
        )
        html = _render_perm_footprint_card(str(project))
        assert "Allocated perm space" in html
        # Both parent names appear with their byte totals.
        assert "Root" in html
        assert "Other" in html
        # Total appears (1 GB + 100 MB rounds to "1.1 GB").
        assert "GB" in html

    def test_card_renders_empty_when_no_prereqs(self, tmp_path):
        from td_release_packager.reporting.pipeline_report import (
            _render_perm_footprint_card,
        )

        project = tmp_path / "proj"
        (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
        html = _render_perm_footprint_card(str(project))
        # No card emitted at all — the tab just shows the wave SVG.
        assert html == ""

    def test_card_flags_unresolved_tokenised_perm(self, tmp_path):
        from td_release_packager.reporting.pipeline_report import (
            _render_perm_footprint_card,
        )

        project = tmp_path / "proj"
        project.mkdir()
        _make_prereqs(
            project,
            databases={
                "Tokenised.db": "CREATE DATABASE Tokenised FROM Root AS PERM = {{PERM_DEV}};\n",
            },
        )
        html = _render_perm_footprint_card(str(project))
        assert "tokenised" in html.lower()
        assert "env-config" in html

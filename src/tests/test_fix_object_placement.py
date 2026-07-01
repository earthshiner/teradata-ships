"""Object-placement fixer (#538).

Covers the content-only rewrite of ``.viw`` files that qualify an
object with a tables-database identifier (bypassing the intermediate
locking view layer).

The rule is default-ERROR, the fix is mechanical
(``placement.resolve_views_database`` computes the target), and
locking views + comment/string masks are respected — the fixer is
registered ``default_on=True``.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.fixers import FIX_REGISTRY, FixResult
from td_release_packager.fixers.object_placement import fix_object_placement


# ---------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------

_SEPARATED_PLACEMENT_YAML = """\
strategy: separated
locking_views: true
database_pattern_tables: "{BASE}_STD_T"
database_pattern_views: "{BASE}_STD_V"
"""


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _setup_project(
    tmp_path: Path, placement_yaml: str | None = _SEPARATED_PLACEMENT_YAML
) -> Path:
    """Create a project with a valid placement config.

    Passing ``placement_yaml=None`` omits the file entirely — for tests
    that exercise the "no placement config → fixer no-op" guard.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / "ships.yaml").write_text("name: testpkg\n", encoding="utf-8", newline="")
    (project / "payload" / "database" / "DDL" / "views").mkdir(parents=True)
    (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
    if placement_yaml is not None:
        (project / "config").mkdir(exist_ok=True)
        _write(project / "config/object_placement.yaml", placement_yaml)
    return project


# ---------------------------------------------------------------
# Registry entry
# ---------------------------------------------------------------


class TestRegistryEntry:
    def test_object_placement_is_registered(self):
        assert "object_placement" in FIX_REGISTRY

    def test_object_placement_is_default_on(self):
        """Default-on: the fix is mechanical and the rule is default-ERROR."""
        assert FIX_REGISTRY["object_placement"].default_on is True

    def test_object_placement_writes_to_payload(self):
        assert FIX_REGISTRY["object_placement"].write_scope == "payload"


# ---------------------------------------------------------------
# Rewrite behaviour
# ---------------------------------------------------------------


class TestRewrite:
    def test_qualifier_rewritten(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/Prod.CustomerV.viw",
            "REPLACE VIEW Prod_STD_V.CustomerV AS\n"
            "SELECT c.Id FROM Prod_STD_T.Customer c;\n",
        )

        result = fix_object_placement(str(project))

        assert isinstance(result, FixResult)
        assert result.rule_id == "object_placement"
        text = f.read_text(encoding="utf-8")
        assert "Prod_STD_T.Customer" not in text
        assert "Prod_STD_V.Customer" in text
        assert result.totals["files_rewritten"] == 1
        assert result.totals["refs_rewritten"] == 1

    def test_multiple_refs_in_same_file(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/Prod.Wide.viw",
            "REPLACE VIEW Prod_STD_V.Wide AS\n"
            "SELECT c.Id, o.Total\n"
            "FROM Prod_STD_T.Customer c\n"
            "JOIN Prod_STD_T.Orders o ON c.Id = o.CustomerId;\n",
        )

        result = fix_object_placement(str(project))

        text = f.read_text(encoding="utf-8")
        assert "Prod_STD_T" not in text
        assert result.totals["refs_rewritten"] == 2

    def test_quoted_identifier_preserved(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/Prod.Q.viw",
            'REPLACE VIEW Prod_STD_V.Q AS\nSELECT c.Id FROM "Prod_STD_T".Customer c;\n',
        )

        result = fix_object_placement(str(project))

        text = f.read_text(encoding="utf-8")
        assert '"Prod_STD_T".Customer' not in text
        assert '"Prod_STD_V".Customer' in text
        assert result.totals["refs_rewritten"] == 1

    def test_ref_inside_comment_is_untouched(self, tmp_path):
        """Comment content is masked out by ``_build_exclusion_mask`` —
        the rewriter must skip it or noise-comment refs would be
        rewritten and drift the source."""
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/Prod.Doc.viw",
            "-- was: FROM Prod_STD_T.Customer\n"
            "REPLACE VIEW Prod_STD_V.Doc AS\n"
            "SELECT c.Id FROM Prod_STD_V.Customer c;\n",
        )
        original = f.read_text(encoding="utf-8")
        result = fix_object_placement(str(project))
        assert result.totals["refs_rewritten"] == 0
        assert f.read_text(encoding="utf-8") == original

    def test_ref_inside_string_literal_is_untouched(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/Prod.Str.viw",
            "REPLACE VIEW Prod_STD_V.Str AS\n"
            "SELECT 'read from Prod_STD_T.Customer' AS msg FROM Prod_STD_V.Customer;\n",
        )
        original = f.read_text(encoding="utf-8")
        result = fix_object_placement(str(project))
        assert result.totals["refs_rewritten"] == 0
        assert f.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------


class TestIdempotency:
    def test_second_run_is_a_noop(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/Prod.Idem.viw",
            "REPLACE VIEW Prod_STD_V.Idem AS SELECT 1 FROM Prod_STD_T.Foo;\n",
        )
        first = fix_object_placement(str(project))
        second = fix_object_placement(str(project))
        assert first.totals["refs_rewritten"] == 1
        assert second.totals["refs_rewritten"] == 0


# ---------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------


class TestDryRun:
    def test_dry_run_makes_no_writes(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/Prod.Dry.viw",
            "REPLACE VIEW Prod_STD_V.Dry AS SELECT 1 FROM Prod_STD_T.Foo;\n",
        )
        original = f.read_text(encoding="utf-8")
        result = fix_object_placement(str(project), dry_run=True)
        assert f.read_text(encoding="utf-8") == original
        assert result.dry_run is True
        # Still reports what would have happened.
        assert result.totals["files_rewritten"] == 1
        assert result.totals["refs_rewritten"] == 1
        # files_written is 0 under dry_run regardless of matches.
        assert result.files_written == 0


# ---------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------


class TestSkips:
    def test_no_placement_yaml_is_noop(self, tmp_path):
        """Missing ``config/object_placement.yaml`` → the rule is
        inactive; the fixer returns cleanly with zero counts."""
        project = _setup_project(tmp_path, placement_yaml=None)
        f = _write(
            project / "payload/database/DDL/views/Prod.NP.viw",
            "REPLACE VIEW Prod_STD_V.NP AS SELECT 1 FROM Prod_STD_T.Foo;\n",
        )
        original = f.read_text(encoding="utf-8")
        result = fix_object_placement(str(project))
        assert f.read_text(encoding="utf-8") == original
        assert result.totals["files_rewritten"] == 0

    def test_colocated_strategy_is_noop(self, tmp_path):
        """A colocated strategy has no separation → the rule doesn't
        fire, so the fixer must not rewrite anything either."""
        project = _setup_project(
            tmp_path,
            placement_yaml=("strategy: colocated\nlocking_views: true\n"),
        )
        # Even a match against the pattern (there is none) shouldn't
        # rewrite because the strategy short-circuits.
        f = _write(
            project / "payload/database/DDL/views/Prod.Col.viw",
            "REPLACE VIEW Prod_STD_V.Col AS SELECT 1 FROM Prod_STD_T.Foo;\n",
        )
        original = f.read_text(encoding="utf-8")
        result = fix_object_placement(str(project))
        assert f.read_text(encoding="utf-8") == original
        assert result.totals["files_rewritten"] == 0

    def test_locking_views_off_is_noop(self, tmp_path):
        project = _setup_project(
            tmp_path,
            placement_yaml=(
                "strategy: separated\n"
                "locking_views: false\n"
                'database_pattern_tables: "{BASE}_STD_T"\n'
                'database_pattern_views: "{BASE}_STD_V"\n'
            ),
        )
        f = _write(
            project / "payload/database/DDL/views/Prod.LV.viw",
            "REPLACE VIEW Prod_STD_V.LV AS SELECT 1 FROM Prod_STD_T.Foo;\n",
        )
        original = f.read_text(encoding="utf-8")
        result = fix_object_placement(str(project))
        assert f.read_text(encoding="utf-8") == original
        assert result.totals["files_rewritten"] == 0

    def test_locking_view_file_is_untouched(self, tmp_path):
        """A 1:1 locking view legitimately references the ``_T``
        companion. ``_is_locking_view`` short-circuits the walk."""
        project = _setup_project(tmp_path)
        # Canonical locking-view shape: matching _V/_T companion +
        # LOCKING ROW FOR ACCESS in the body.
        f = _write(
            project / "payload/database/DDL/views/Prod.LockCustomer.viw",
            "REPLACE VIEW Prod_STD_V.Customer AS\n"
            "LOCKING ROW FOR ACCESS\n"
            "SELECT * FROM Prod_STD_T.Customer;\n",
        )
        original = f.read_text(encoding="utf-8")
        result = fix_object_placement(str(project))
        assert f.read_text(encoding="utf-8") == original
        assert result.totals["files_rewritten"] == 0

    def test_non_viw_file_is_untouched(self, tmp_path):
        """The rule only applies to ``.viw`` — the fixer must too. A
        table file (``.tbl``) with a tables-db reference is legitimate."""
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/tables/Prod.Foo.tbl",
            "CREATE MULTISET TABLE Prod_STD_T.Foo (Id INTEGER);\n",
        )
        original = f.read_text(encoding="utf-8")
        result = fix_object_placement(str(project))
        assert f.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------
# Per-file details in the FixResult
# ---------------------------------------------------------------


class TestFileDetails:
    def test_per_file_details_record_each_substitution(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/Prod.Detail.viw",
            "REPLACE VIEW Prod_STD_V.Detail AS\n"
            "SELECT c.Id FROM Prod_STD_T.Customer c\n"
            "JOIN Prod_STD_T.Orders o ON c.Id = o.CustomerId;\n",
        )
        result = fix_object_placement(str(project))
        assert len(result.files_changed) == 1
        details = result.files_changed[0].details
        assert details["refs_rewritten"] == 2
        refs = details["refs"]
        assert {r["from_db"] for r in refs} == {"Prod_STD_T"}
        assert {r["to_db"] for r in refs} == {"Prod_STD_V"}
        assert all(isinstance(r["line"], int) for r in refs)

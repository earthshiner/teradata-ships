"""
test_changeset.py — Tests for git-native / baseline change detection (#114).

Covers:
    - reverse-dependants graph inversion and forward BFS expansion
    - content-hash baseline capture + diff (git-less fallback)
    - detect_changeset end-to-end via the baseline path
    - mapping changed files → qualified objects + dependants pull-in
"""

from pathlib import Path

from td_release_packager.changeset import (
    _expand_dependants,
    _reverse_dependants,
    detect_changeset,
    write_changeset_baseline,
)
from td_release_packager.project_paths import changeset_baseline_path


def _mk_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for sub in (
        "payload/database/DDL/tables",
        "payload/database/DDL/views",
        "config/env",
    ):
        (project / sub).mkdir(parents=True, exist_ok=True)
    (project / ".ships").mkdir(parents=True, exist_ok=True)
    (project / ".ships" / ".build_counter").write_text("0\n", encoding="utf-8")
    return project


def _seed_table_and_view(project: Path) -> None:
    (project / "payload/database/DDL/tables/DB.Customer.tbl").write_text(
        "CREATE MULTISET TABLE DB.Customer (Id INTEGER) PRIMARY INDEX (Id);\n",
        encoding="utf-8",
    )
    (project / "payload/database/DDL/views/DB.ActiveCust.viw").write_text(
        "REPLACE VIEW DB.ActiveCust AS SELECT Id FROM DB.Customer;\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------


class TestReverseDependants:
    def test_inverts_edges(self):
        deps = {"V": {"T"}, "W": {"V"}}
        rev = _reverse_dependants(deps)
        assert rev["T"] == {"V"}
        assert rev["V"] == {"W"}

    def test_forward_bfs_transitive(self):
        # T <- V <- W : changing T pulls in V and W
        rev = _reverse_dependants({"V": {"T"}, "W": {"V"}})
        assert _expand_dependants({"T"}, rev) == {"V", "W"}

    def test_bfs_excludes_seed(self):
        rev = _reverse_dependants({"V": {"T"}})
        assert "T" not in _expand_dependants({"T"}, rev)

    def test_bfs_handles_cycle(self):
        rev = _reverse_dependants({"A": {"B"}, "B": {"A"}})
        # Must terminate and not include the seed itself.
        assert _expand_dependants({"A"}, rev) == {"B"}


# ---------------------------------------------------------------
# Baseline capture + diff
# ---------------------------------------------------------------


class TestBaseline:
    def test_capture_writes_baseline(self, tmp_path):
        project = _mk_project(tmp_path)
        _seed_table_and_view(project)
        path = write_changeset_baseline(
            str(project), str(project / "payload" / "database")
        )
        assert Path(path) == Path(changeset_baseline_path(str(project)))
        assert Path(path).is_file()

    def test_no_baseline_yields_none_mode(self, tmp_path):
        project = _mk_project(tmp_path)
        _seed_table_and_view(project)
        result = detect_changeset(str(project))
        assert result.mode == "none"
        assert "baseline" in result.note.lower()

    def test_unchanged_after_capture(self, tmp_path):
        project = _mk_project(tmp_path)
        _seed_table_and_view(project)
        write_changeset_baseline(str(project), str(project / "payload" / "database"))
        result = detect_changeset(str(project))
        assert result.mode == "baseline"
        assert result.changed == set()
        assert result.selected == set()

    def test_edit_table_pulls_in_view(self, tmp_path):
        project = _mk_project(tmp_path)
        _seed_table_and_view(project)
        write_changeset_baseline(str(project), str(project / "payload" / "database"))

        # Edit the table → its dependent view must be pulled in.
        (project / "payload/database/DDL/tables/DB.Customer.tbl").write_text(
            "CREATE MULTISET TABLE DB.Customer (Id INTEGER, Name VARCHAR(50)) "
            "PRIMARY INDEX (Id);\n",
            encoding="utf-8",
        )
        result = detect_changeset(str(project))
        assert result.mode == "baseline"
        assert "DB.Customer" in result.changed
        assert "DB.ActiveCust" in result.dependants
        assert result.selected == {"DB.Customer", "DB.ActiveCust"}

    def test_edit_view_only_no_dependants(self, tmp_path):
        project = _mk_project(tmp_path)
        _seed_table_and_view(project)
        write_changeset_baseline(str(project), str(project / "payload" / "database"))

        (project / "payload/database/DDL/views/DB.ActiveCust.viw").write_text(
            "REPLACE VIEW DB.ActiveCust AS SELECT Id FROM DB.Customer WHERE Id > 0;\n",
            encoding="utf-8",
        )
        result = detect_changeset(str(project))
        assert result.changed == {"DB.ActiveCust"}
        assert result.dependants == set()

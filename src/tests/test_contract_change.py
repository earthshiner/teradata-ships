"""
test_contract_change.py — backward-incompatible contract changes (#171).

Covers contract extraction (view/procedure/table), the backward-incompatible
diff, baseline write/load, the no-baseline no-op, and end-to-end
check_contract_changes against a captured baseline.
"""

from __future__ import annotations

from td_release_packager.contract import (
    build_contracts,
    check_contract_changes,
    diff_contracts,
    extract_contract,
    load_baseline,
    write_baseline,
)
from td_release_packager.project_paths import contracts_baseline_path


def _details(base_sql: str, cur_sql: str):
    b = extract_contract(base_sql)
    c = extract_contract(cur_sql)
    return [
        d["detail"] for d in diff_contracts({b["qualified"]: b}, {c["qualified"]: c})
    ]


class TestExtraction:
    def test_view_columns(self):
        c = extract_contract("CREATE VIEW db.v (a, b, c) AS SELECT 1, 2, 3;")
        assert c == {"kind": "VIEW", "qualified": "db.v", "items": ["a", "b", "c"]}

    def test_view_without_column_list_returns_none(self):
        # No explicit column list → no comparable contract.
        assert extract_contract("CREATE VIEW db.v AS SELECT 1 AS a;") is None

    def test_procedure_params(self):
        c = extract_contract(
            "CREATE PROCEDURE db.p (IN a INTEGER, OUT b VARCHAR(10)) BEGIN END;"
        )
        assert c["kind"] == "PROCEDURE"
        assert c["items"] == [
            {"name": "a", "direction": "IN", "type": "INTEGER"},
            {"name": "b", "direction": "OUT", "type": "VARCHAR(10)"},
        ]

    def test_table_columns_skip_constraints(self):
        c = extract_contract(
            "CREATE MULTISET TABLE db.t (id INTEGER, name VARCHAR(10), "
            "amt DECIMAL(5,2)) PRIMARY INDEX (id);"
        )
        assert c["kind"] == "TABLE"
        assert [col["name"] for col in c["items"]] == ["id", "name", "amt"]
        assert c["items"][2]["type"] == "DECIMAL(5,2)"


class TestDiff:
    def test_removed_view_column(self):
        d = _details(
            "CREATE VIEW db.v (a, b, c) AS SELECT 1,2,3;",
            "CREATE VIEW db.v (a, c) AS SELECT 1,3;",
        )
        assert any("'b' was removed" in x for x in d)

    def test_reordered_view_columns(self):
        d = _details(
            "CREATE VIEW db.v (a, b) AS SELECT 1,2;",
            "CREATE VIEW db.v (b, a) AS SELECT 2,1;",
        )
        assert any("reordered" in x for x in d)

    def test_removed_proc_param_and_direction_change(self):
        d = _details(
            "CREATE PROCEDURE db.p (IN a INTEGER, OUT b VARCHAR(10)) BEGIN END;",
            "CREATE PROCEDURE db.p (INOUT a INTEGER) BEGIN END;",
        )
        assert any("'b' was removed" in x for x in d)
        assert any("direction changed" in x for x in d)

    def test_dropped_and_retyped_table_column(self):
        d = _details(
            "CREATE TABLE db.t (id INTEGER, name VARCHAR(10), amt DECIMAL(5,2)) "
            "PRIMARY INDEX (id);",
            "CREATE TABLE db.t (id INTEGER, name VARCHAR(20)) PRIMARY INDEX (id);",
        )
        assert any("'amt' was dropped" in x for x in d)
        assert any("'name' datatype changed" in x for x in d)

    def test_added_column_is_backward_compatible(self):
        # Adding a column is compatible → no finding.
        d = _details(
            "CREATE VIEW db.v (a) AS SELECT 1;",
            "CREATE VIEW db.v (a, b) AS SELECT 1, 2;",
        )
        assert d == []

    def test_object_removed(self):
        b = extract_contract("CREATE VIEW db.v (a) AS SELECT 1;")
        changes = diff_contracts({b["qualified"]: b}, {})
        assert changes and "no longer defined" in changes[0]["detail"]


class TestBaselineRoundTrip:
    def test_write_and_load(self, tmp_path):
        contracts = {"db.v": {"kind": "VIEW", "qualified": "db.v", "items": ["a"]}}
        path = str(tmp_path / "baseline.json")
        write_baseline(path, contracts)
        assert load_baseline(path) == contracts

    def test_load_missing_returns_none(self, tmp_path):
        assert load_baseline(str(tmp_path / "nope.json")) is None


class TestEndToEnd:
    def _payload(self, tmp_path, view_sql):
        d = tmp_path / "payload" / "database" / "DDL" / "views"
        d.mkdir(parents=True, exist_ok=True)
        (d / "db.v.viw").write_text(view_sql, encoding="utf-8")
        return tmp_path

    def test_no_baseline_is_noop(self, tmp_path):
        proj = self._payload(tmp_path, "CREATE VIEW db.v (a, b) AS SELECT 1, 2;")
        assert (
            check_contract_changes(str(proj), str(proj / "payload" / "database")) == []
        )

    def test_flags_change_against_baseline(self, tmp_path):
        proj = self._payload(tmp_path, "CREATE VIEW db.v (a, b) AS SELECT 1, 2;")
        payload = str(proj / "payload" / "database")
        # Capture baseline, then narrow the view (drop column b).
        write_baseline(contracts_baseline_path(str(proj)), build_contracts(payload))
        (proj / "payload" / "database" / "DDL" / "views" / "db.v.viw").write_text(
            "CREATE VIEW db.v (a) AS SELECT 1;", encoding="utf-8"
        )
        issues = check_contract_changes(str(proj), payload, severity="ERROR")
        assert len(issues) == 1
        assert issues[0].rule == "contract_change"
        assert issues[0].severity == "ERROR"
        assert "db.v" in issues[0].file
        assert issues[0].remediation["requires_human_review"] is True

    def test_no_change_clean(self, tmp_path):
        proj = self._payload(tmp_path, "CREATE VIEW db.v (a, b) AS SELECT 1, 2;")
        payload = str(proj / "payload" / "database")
        write_baseline(contracts_baseline_path(str(proj)), build_contracts(payload))
        assert check_contract_changes(str(proj), payload) == []

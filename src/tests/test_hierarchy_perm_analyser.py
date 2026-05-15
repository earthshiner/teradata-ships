"""Tests for static database hierarchy PERM capacity analysis."""

from td_release_packager.hierarchy_perm_analyser import (
    analyse_hierarchy_perm_capacity,
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestHierarchyPermAnalyser:
    """Integration-style tests against temporary SHIPS payload trees."""

    def test_parent_capacity_uses_immediate_children_not_grandchildren(self, tmp_path):
        payload = tmp_path / "payload"
        _write(
            payload / "01_pre_requisites" / "databases" / "PARENT.db",
            "create database PARENT from DBC as perm = 1000000000;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "BASE_NODE.db",
            "create database BASE_NODE from PARENT as perm = 100e6;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "BASE_T.db",
            "create database BASE_T from BASE_NODE as perm = 100e6;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "GCFR_NODE.db",
            "create database GCFR_NODE from PARENT as perm = 180e6;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "TMP_NODE.db",
            "create database TMP_NODE from PARENT as perm = 100e6;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "STG_NODE.db",
            "create database STG_NODE from PARENT as perm = 50e6;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "OI_NODE.db",
            "create database OI_NODE from PARENT as perm = 25e6;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "UTL_NODE.db",
            "create database UTL_NODE from PARENT as perm = 100e6;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "TXFM_NODE.db",
            "create database TXFM_NODE from PARENT as perm = 0;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "SEM_NODE.db",
            "create database SEM_NODE from PARENT as perm = 25e6;\n",
        )
        _write(
            payload / "01_pre_requisites" / "users" / "ETL_USER.usr",
            "create user ETL_USER from PARENT as password=ETL_USER perm=0;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "OPR_NODE.db",
            "create database OPR_NODE from PARENT as perm = 50e6;\n",
        )

        result = analyse_hierarchy_perm_capacity(str(tmp_path))
        by_parent = {finding.parent_name: finding for finding in result.findings}

        assert result.passed
        assert by_parent["PARENT"].direct_child_perm_bytes == 630_000_000
        assert by_parent["PARENT"].headroom_bytes == 370_000_000
        assert by_parent["BASE_NODE"].direct_child_perm_bytes == 100_000_000
        assert by_parent["BASE_NODE"].headroom_bytes == 0

    def test_insufficient_parent_capacity_fails(self, tmp_path):
        payload = tmp_path / "payload"
        _write(
            payload / "01_pre_requisites" / "databases" / "PARENT.db",
            "create database PARENT from DBC as perm = 100e6;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "CHILD1.db",
            "create database CHILD1 from PARENT as perm = 75e6;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "CHILD2.db",
            "create database CHILD2 from PARENT as perm = 50e6;\n",
        )

        result = analyse_hierarchy_perm_capacity(str(tmp_path))
        finding = {item.parent_name: item for item in result.findings}["PARENT"]

        assert not result.passed
        assert result.errors == 1
        assert finding.status == "INSUFFICIENT"
        assert finding.direct_child_perm_bytes == 125_000_000
        assert finding.headroom_bytes == -25_000_000

    def test_comments_do_not_affect_perm_values(self, tmp_path):
        payload = tmp_path / "payload"
        _write(
            payload / "01_pre_requisites" / "databases" / "PARENT.db",
            "create database PARENT from DBC as perm = 200e6;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "CHILD.db",
            """
            create database CHILD from PARENT
            as perm = 100e6
            /* for another environment: as perm = 400e6 */
            ;
            """,
        )

        result = analyse_hierarchy_perm_capacity(str(tmp_path))
        finding = {item.parent_name: item for item in result.findings}["PARENT"]

        assert result.passed
        assert finding.direct_child_perm_bytes == 100_000_000

    def test_external_parent_reported_separately(self, tmp_path):
        payload = tmp_path / "payload"
        _write(
            payload / "01_pre_requisites" / "databases" / "CHILD.db",
            "create database CHILD from EXTERNAL_PARENT as perm = 10m;\n",
        )

        result = analyse_hierarchy_perm_capacity(str(tmp_path))

        assert result.passed
        assert len(result.external_parents) == 1
        assert result.external_parents[0].parent_name == "EXTERNAL_PARENT"
        assert result.external_parents[0].direct_child_perm_bytes == 10 * 1024 * 1024

    def test_tokenised_names_are_compared_consistently(self, tmp_path):
        payload = tmp_path / "payload"
        _write(
            payload / "01_pre_requisites" / "databases" / "{{PARENT_NODE}}.db",
            "create database {{PARENT_NODE}} from DBC as perm = 100e6;\n",
        )
        _write(
            payload / "01_pre_requisites" / "databases" / "{{CHILD_NODE}}.db",
            "create database {{CHILD_NODE}} from {{PARENT_NODE}} as perm = 50e6;\n",
        )

        result = analyse_hierarchy_perm_capacity(str(tmp_path))
        finding = {item.parent_name: item for item in result.findings}[
            "{{PARENT_NODE}}"
        ]

        assert result.passed
        assert finding.direct_child_perm_bytes == 50_000_000

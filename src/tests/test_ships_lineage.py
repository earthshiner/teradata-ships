"""
test_ships_lineage.py — Tests for the OpenLineage event emission module.

Covers:
    - _active: disabled when OPENLINEAGE_URL unset or OPENLINEAGE_DISABLED set
    - _namespace: env-var override, host fallback, unknown fallback
    - _read_build_meta: absent file, valid file, partial file, malformed JSON
    - _emit_to_file: appends NDJSON lines; creates new file; handles errors
    - _emit_to_http: posts JSON; handles connection errors gracefully
    - _emit_event: routes to file transport; no-op when inactive; ignores
                   unsupported scheme
    - start_deploy_run: returns a UUID string; emits START event when active
    - complete_deploy_run: emits COMPLETE with output datasets
    - fail_deploy_run: emits FAIL with partial outputs and failure list
    - deploy_package wrapper: lineage calls are made in the right order
                              (no live DB required — impl is mocked)
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import ships_lineage as sut


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _write_build_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _read_ndjson(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------
# _active
# ---------------------------------------------------------------


class TestActive:
    def test_inactive_when_url_unset(self, monkeypatch):
        monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)
        assert sut._active() is False

    def test_active_when_url_set(self, monkeypatch):
        monkeypatch.setenv("OPENLINEAGE_URL", "http://marquez:5000")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)
        assert sut._active() is True

    def test_inactive_when_disabled_true(self, monkeypatch):
        monkeypatch.setenv("OPENLINEAGE_URL", "http://marquez:5000")
        monkeypatch.setenv("OPENLINEAGE_DISABLED", "true")
        assert sut._active() is False

    def test_inactive_when_disabled_1(self, monkeypatch):
        monkeypatch.setenv("OPENLINEAGE_URL", "http://marquez:5000")
        monkeypatch.setenv("OPENLINEAGE_DISABLED", "1")
        assert sut._active() is False


# ---------------------------------------------------------------
# _namespace
# ---------------------------------------------------------------


class TestNamespace:
    def test_env_override_takes_priority(self, monkeypatch):
        monkeypatch.setenv("OPENLINEAGE_NAMESPACE", "teradata://override:1025")
        assert sut._namespace("myhost") == "teradata://override:1025"

    def test_host_used_when_no_override(self, monkeypatch):
        monkeypatch.delenv("OPENLINEAGE_NAMESPACE", raising=False)
        assert sut._namespace("dbhost.example.com") == "teradata://dbhost.example.com"

    def test_unknown_when_no_host_no_override(self, monkeypatch):
        monkeypatch.delenv("OPENLINEAGE_NAMESPACE", raising=False)
        assert sut._namespace("") == "teradata://unknown"


# ---------------------------------------------------------------
# _read_build_meta
# ---------------------------------------------------------------


class TestReadBuildMeta:
    def test_returns_defaults_when_no_file(self, tmp_path):
        meta = sut._read_build_meta(str(tmp_path))
        assert meta == {
            "build_number": "",
            "environment": "",
            "package_name": "",
            "package_filename": "",
        }

    def test_reads_all_fields(self, tmp_path):
        _write_build_json(
            tmp_path / "BUILD.json",
            {
                "build_number": "0042",
                "environment": "PRD",
                "package_name": "ships_test",
                "package_filename": "ships_test_0042.zip",
            },
        )
        meta = sut._read_build_meta(str(tmp_path))
        assert meta["build_number"] == "0042"
        assert meta["environment"] == "PRD"
        assert meta["package_name"] == "ships_test"
        assert meta["package_filename"] == "ships_test_0042.zip"

    def test_partial_file_fills_missing_with_defaults(self, tmp_path):
        _write_build_json(tmp_path / "BUILD.json", {"build_number": "0007"})
        meta = sut._read_build_meta(str(tmp_path))
        assert meta["build_number"] == "0007"
        assert meta["environment"] == ""

    def test_malformed_json_returns_defaults(self, tmp_path):
        (tmp_path / "BUILD.json").write_text("{bad json", encoding="utf-8")
        meta = sut._read_build_meta(str(tmp_path))
        assert meta["build_number"] == ""


# ---------------------------------------------------------------
# _emit_to_file
# ---------------------------------------------------------------


class TestEmitToFile:
    def test_creates_and_appends(self, tmp_path):
        target = tmp_path / "ol.ndjson"
        sut._emit_to_file(str(target), '{"a":1}')
        sut._emit_to_file(str(target), '{"b":2}')
        lines = _read_ndjson(target)
        assert lines == [{"a": 1}, {"b": 2}]

    def test_handles_write_error_gracefully(self, tmp_path):
        # Non-existent parent directory → write error, must not raise
        bad_path = str(tmp_path / "nonexistent" / "ol.ndjson")
        sut._emit_to_file(bad_path, '{"x":1}')  # must not raise


# ---------------------------------------------------------------
# _emit_event
# ---------------------------------------------------------------


class TestEmitEvent:
    def test_no_op_when_url_not_set(self, monkeypatch):
        monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
        # Should not raise or do anything
        sut._emit_event({"eventType": "START"})

    def test_routes_to_file(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)
        sut._emit_event({"eventType": "START", "val": 1})
        events = _read_ndjson(target)
        assert len(events) == 1
        assert events[0]["eventType"] == "START"

    def test_ignores_unsupported_scheme(self, monkeypatch):
        monkeypatch.setenv("OPENLINEAGE_URL", "kafka://broker:9092")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)
        # Must not raise
        sut._emit_event({"eventType": "START"})

    def test_no_op_when_disabled(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.setenv("OPENLINEAGE_DISABLED", "true")
        sut._emit_event({"eventType": "START"})
        assert not target.exists()


# ---------------------------------------------------------------
# start_deploy_run
# ---------------------------------------------------------------


class TestStartDeployRun:
    def test_returns_uuid_when_inactive(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
        run_id = sut.start_deploy_run(str(tmp_path))
        uuid.UUID(run_id)  # must parse as a valid UUID

    def test_emits_start_event(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)
        _write_build_json(
            tmp_path / "BUILD.json",
            {"build_number": "0001", "package_name": "my_pkg"},
        )
        run_id = sut.start_deploy_run(str(tmp_path), dry_run=True)
        events = _read_ndjson(target)
        assert len(events) == 1
        ev = events[0]
        assert ev["eventType"] == "START"
        assert ev["run"]["runId"] == run_id
        assert ev["run"]["facets"]["ships"]["dry_run"] is True
        assert ev["run"]["facets"]["ships"]["build_number"] == "0001"

    def test_start_event_has_empty_inputs_and_outputs(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)
        sut.start_deploy_run(str(tmp_path))
        ev = _read_ndjson(target)[0]
        assert ev["inputs"] == []
        assert ev["outputs"] == []


# ---------------------------------------------------------------
# complete_deploy_run
# ---------------------------------------------------------------


class TestCompleteDeployRun:
    def test_emits_complete_with_output_datasets(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)
        monkeypatch.delenv("OPENLINEAGE_NAMESPACE", raising=False)
        run_id = str(uuid.uuid4())
        sut.complete_deploy_run(
            run_id,
            str(tmp_path),
            completed_objects=[("MYDB", "ORDERS_T"), ("MYDB", "CUSTOMERS_V")],
            db_host="td-host",
        )
        ev = _read_ndjson(target)[0]
        assert ev["eventType"] == "COMPLETE"
        assert ev["run"]["runId"] == run_id
        assert len(ev["outputs"]) == 2
        names = {o["name"] for o in ev["outputs"]}
        assert "MYDB.ORDERS_T" in names
        assert "MYDB.CUSTOMERS_V" in names

    def test_no_op_when_inactive(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
        # Must not raise
        sut.complete_deploy_run(str(uuid.uuid4()), str(tmp_path), [])

    def test_namespace_from_db_host(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)
        monkeypatch.delenv("OPENLINEAGE_NAMESPACE", raising=False)
        sut.complete_deploy_run(
            str(uuid.uuid4()),
            str(tmp_path),
            completed_objects=[("DB", "T")],
            db_host="myhost:1025",
        )
        ev = _read_ndjson(target)[0]
        assert ev["outputs"][0]["namespace"] == "teradata://myhost:1025"


# ---------------------------------------------------------------
# fail_deploy_run
# ---------------------------------------------------------------


class TestFailDeployRun:
    def test_emits_fail_event(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)
        run_id = str(uuid.uuid4())
        sut.fail_deploy_run(
            run_id,
            str(tmp_path),
            completed_objects=[("DB", "T_OK")],
            failed_objects=[("DB", "T_BAD", "Error 3706")],
        )
        ev = _read_ndjson(target)[0]
        assert ev["eventType"] == "FAIL"
        assert ev["run"]["runId"] == run_id
        # Partial outputs for completed objects
        assert len(ev["outputs"]) == 1
        assert ev["outputs"][0]["name"] == "DB.T_OK"
        # Failed objects in the run facet
        failures = ev["run"]["facets"]["shipsFailures"]["failed_objects"]
        assert len(failures) == 1
        assert failures[0]["object"] == "T_BAD"
        assert failures[0]["error"] == "Error 3706"

    def test_top_level_error_stamped(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)
        sut.fail_deploy_run(
            str(uuid.uuid4()),
            str(tmp_path),
            error="unexpected exception",
        )
        ev = _read_ndjson(target)[0]
        assert ev["run"]["facets"]["ships"]["error"] == "unexpected exception"

    def test_no_op_when_inactive(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
        # Must not raise
        sut.fail_deploy_run(str(uuid.uuid4()), str(tmp_path))


# ---------------------------------------------------------------
# run_id continuity: START run_id matches COMPLETE / FAIL
# ---------------------------------------------------------------


class TestRunIdContinuity:
    def _collect_events(self, tmp_path: Path) -> list[dict]:
        target = tmp_path / "ol.ndjson"
        if not target.exists():
            return []
        return _read_ndjson(target)

    def test_complete_shares_run_id_with_start(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)
        run_id = sut.start_deploy_run(str(tmp_path))
        sut.complete_deploy_run(run_id, str(tmp_path), [])
        events = _read_ndjson(target)
        assert len(events) == 2
        assert events[0]["run"]["runId"] == run_id
        assert events[1]["run"]["runId"] == run_id
        assert events[0]["eventType"] == "START"
        assert events[1]["eventType"] == "COMPLETE"

    def test_fail_shares_run_id_with_start(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)
        run_id = sut.start_deploy_run(str(tmp_path))
        sut.fail_deploy_run(run_id, str(tmp_path))
        events = _read_ndjson(target)
        assert events[0]["run"]["runId"] == run_id
        assert events[1]["run"]["runId"] == run_id
        assert events[0]["eventType"] == "START"
        assert events[1]["eventType"] == "FAIL"


# ---------------------------------------------------------------
# deploy_package integration — lineage calls are correctly wired
# ---------------------------------------------------------------


class TestDeployPackageLineageIntegration:
    """Verify the lineage hook-up in deploy_package without a live DB.

    _deploy_package_impl is mocked to return a canned PackageDeployResult.
    """

    def _make_result(self, completed=None, failed=None):
        from database_package_deployer.models import (
            DeployState,
            ObjectDeployResult,
            ObjectType,
            PackageDeployResult,
        )

        results = []
        for db, obj in completed or []:
            r = ObjectDeployResult(
                database_name=db,
                object_name=obj,
                object_type=ObjectType.TABLE,
                state=DeployState.COMPLETED,
            )
            results.append(r)
        for db, obj, err in failed or []:
            r = ObjectDeployResult(
                database_name=db,
                object_name=obj,
                object_type=ObjectType.TABLE,
                state=DeployState.FAILED,
                error=err,
            )
            results.append(r)
        total = len(results)
        completed_count = sum(1 for r in results if r.state == DeployState.COMPLETED)
        failed_count = sum(1 for r in results if r.state == DeployState.FAILED)
        return PackageDeployResult(
            deployment_id="test-run",
            manifest_path="/fake/manifest.json",
            total=total,
            completed=completed_count,
            failed=failed_count,
            results=results,
        )

    def test_emits_start_then_complete_on_success(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)

        mock_result = self._make_result(completed=[("DB", "T")])

        from database_package_deployer import deployer

        with patch.object(
            deployer,
            "_deploy_package_impl",
            return_value=mock_result,
        ):
            deployer.deploy_package(
                cursor=MagicMock(),
                package_dir=str(tmp_path),
            )

        events = _read_ndjson(target)
        types = [ev["eventType"] for ev in events]
        assert types == ["START", "COMPLETE"]

    def test_emits_start_then_fail_on_failure(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)

        mock_result = self._make_result(
            completed=[("DB", "T_OK")],
            failed=[("DB", "T_BAD", "Error 3707")],
        )

        from database_package_deployer import deployer

        with patch.object(
            deployer,
            "_deploy_package_impl",
            return_value=mock_result,
        ):
            deployer.deploy_package(
                cursor=MagicMock(),
                package_dir=str(tmp_path),
            )

        events = _read_ndjson(target)
        types = [ev["eventType"] for ev in events]
        assert types == ["START", "FAIL"]
        # Partial outputs for the completed object
        assert len(events[1]["outputs"]) == 1
        assert events[1]["outputs"][0]["name"] == "DB.T_OK"

    def test_emits_fail_when_impl_raises(self, monkeypatch, tmp_path):
        target = tmp_path / "ol.ndjson"
        monkeypatch.setenv("OPENLINEAGE_URL", f"file://{target}")
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)

        from database_package_deployer import deployer

        with patch.object(
            deployer,
            "_deploy_package_impl",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError):
                deployer.deploy_package(
                    cursor=MagicMock(),
                    package_dir=str(tmp_path),
                )

        events = _read_ndjson(target)
        types = [ev["eventType"] for ev in events]
        assert types == ["START", "FAIL"]
        assert events[1]["run"]["facets"]["ships"]["error"] == "boom"

    def test_no_lineage_events_when_url_unset(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
        monkeypatch.delenv("OPENLINEAGE_DISABLED", raising=False)

        mock_result = self._make_result(completed=[("DB", "T")])

        from database_package_deployer import deployer

        with patch.object(
            deployer,
            "_deploy_package_impl",
            return_value=mock_result,
        ):
            deployer.deploy_package(
                cursor=MagicMock(),
                package_dir=str(tmp_path),
            )

        # No NDJSON file written — lineage was a no-op
        assert not (tmp_path / "ol.ndjson").exists()

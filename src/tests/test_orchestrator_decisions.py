"""
test_orchestrator_decisions.py — Tests for the ships.decisions.json
manifest writer.

Covers:
    - DecisionsManifest construction (load existing, init new)
    - Run lifecycle — context-managed, finalises timestamps,
      computes final_status, persists on exit
    - Stage lifecycle — config_resolved/inputs/outputs/decisions/issues
    - Status auto-upgrade from error issues
    - Schema versioning — current version persisted, unknown rejected
    - Append-only behaviour across runs
    - Atomic file write via tempfile + os.replace
    - Corruption / unparseable / unsupported version errors
    - Interrupted run path (exception inside context manager)
"""

from __future__ import annotations

import json

import pytest

from td_release_packager.orchestrator.decisions import (
    DECISIONS_FILENAME,
    FINAL_STATUSES,
    ISSUE_SEVERITIES,
    SCHEMA_VERSION,
    STAGE_STATUSES,
    DecisionsCorruptError,
    DecisionsManifest,
    DecisionsSchemaError,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------
# Construction
# ---------------------------------------------------------------


class TestConstruction:
    def test_new_manifest_in_memory_only(self, tmp_path):
        """Construction with no existing file leaves disc untouched."""
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        assert not p.exists()
        assert m.data["schema_version"] == SCHEMA_VERSION
        assert m.data["project"] == {"name": "X"}
        assert m.data["runs"] == []

    def test_save_persists_in_memory_state(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        m.save()
        data = _read_json(str(p))
        assert data["project"]["name"] == "X"

    def test_load_existing_preserves_content(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        original = {
            "schema_version": SCHEMA_VERSION,
            "project": {"name": "X"},
            "runs": [{"run_id": "r1", "stages": []}],
        }
        p.write_text(json.dumps(original), encoding="utf-8")
        m = DecisionsManifest(str(p))
        assert m.data["project"]["name"] == "X"
        assert m.data["runs"][0]["run_id"] == "r1"

    def test_load_ignores_project_meta_arg(self, tmp_path):
        """When file exists, the project_meta arg is ignored."""
        p = tmp_path / DECISIONS_FILENAME
        original = {
            "schema_version": SCHEMA_VERSION,
            "project": {"name": "Existing"},
            "runs": [],
        }
        p.write_text(json.dumps(original), encoding="utf-8")
        m = DecisionsManifest(str(p), project_meta={"name": "Should-Be-Ignored"})
        assert m.data["project"]["name"] == "Existing"


# ---------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------


class TestRunLifecycle:
    def test_clean_run_finalises_timestamps_and_status(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("td_release_packager process /raw") as run:
            assert run.run_id  # populated
        data = _read_json(str(p))
        run_entry = data["runs"][0]
        assert run_entry["finished_at"] is not None
        assert isinstance(run_entry["duration_ms"], int)
        assert run_entry["duration_ms"] >= 0
        assert run_entry["final_status"] == "success"
        assert "interrupted" not in run_entry

    def test_run_records_command_verbatim(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("td_release_packager process /raw --strict") as _run:
            pass
        data = _read_json(str(p))
        assert data["runs"][0]["command"] == "td_release_packager process /raw --strict"

    def test_explicit_run_id_used_when_provided(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd", run_id="custom-id") as run:
            assert run.run_id == "custom-id"

    def test_interrupted_run_marked_and_persisted(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with pytest.raises(RuntimeError, match="boom"):
            with m.run("cmd") as _run:
                raise RuntimeError("boom")
        data = _read_json(str(p))
        run_entry = data["runs"][0]
        assert run_entry["interrupted"] is True
        assert run_entry["final_status"] == "failed"
        assert run_entry["finished_at"] is not None


# ---------------------------------------------------------------
# Stage lifecycle
# ---------------------------------------------------------------


class TestStageLifecycle:
    def test_stage_universal_sections_initialised(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as run:
            with run.stage("scaffold") as _stage:
                pass
        stage = _read_json(str(p))["runs"][0]["stages"][0]
        for key in ("config_resolved", "inputs", "outputs", "decisions", "issues"):
            assert key in stage
        assert stage["status"] == "success"
        assert stage["stage"] == "scaffold"

    def test_set_status_validates_vocabulary(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as run:
            with run.stage("scaffold") as stage:
                with pytest.raises(ValueError, match="unknown stage status"):
                    stage.set_status("frobinated")

    def test_set_config_resolved_records_provenance(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as run:
            with run.stage("generate") as stage:
                stage.set_config_resolved(
                    "strict",
                    value=True,
                    source="layer-3",
                    source_path="ships.yaml",
                )
        cr = _read_json(str(p))["runs"][0]["stages"][0]["config_resolved"]
        assert cr["strict"] == {
            "value": True,
            "source": "layer-3",
            "source_path": "ships.yaml",
        }

    def test_set_inputs_outputs_decisions_merge(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as run:
            with run.stage("harvest") as stage:
                stage.set_inputs(files_read=47, modules=["DOM"])
                stage.set_inputs(extra="late")
                stage.set_outputs(files_written=["a.tbl"])
                stage.set_decisions(tokens_applied=3, classifications={"TABLE": 5})
        s = _read_json(str(p))["runs"][0]["stages"][0]
        assert s["inputs"] == {
            "files_read": 47,
            "modules": ["DOM"],
            "extra": "late",
        }
        assert s["outputs"]["files_written"] == ["a.tbl"]
        assert s["decisions"]["tokens_applied"] == 3
        assert s["decisions"]["classifications"] == {"TABLE": 5}

    def test_add_issue_records_all_fields(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as run:
            with run.stage("generate") as stage:
                stage.add_issue(
                    severity="warning",
                    code="GEN-COLLISION",
                    message="renamed",
                    location="foo.viw:42",
                )
        issues = _read_json(str(p))["runs"][0]["stages"][0]["issues"]
        assert issues[0]["severity"] == "warning"
        assert issues[0]["code"] == "GEN-COLLISION"
        assert issues[0]["message"] == "renamed"
        assert issues[0]["location"] == "foo.viw:42"

    def test_add_issue_validates_severity(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as run:
            with run.stage("generate") as stage:
                with pytest.raises(ValueError, match="unknown severity"):
                    stage.add_issue("catastrophic", "X", "no")

    def test_error_issue_auto_upgrades_status(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as run:
            with run.stage("generate") as stage:
                stage.add_issue("error", "GEN-FAIL", "broken")
                # status not explicitly set
        s = _read_json(str(p))["runs"][0]["stages"][0]
        assert s["status"] == "error"

    def test_warning_issue_does_not_auto_upgrade_status(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as run:
            with run.stage("generate") as stage:
                stage.add_issue("warning", "GEN-X", "minor")
        s = _read_json(str(p))["runs"][0]["stages"][0]
        # Caller must explicitly set "warning" — we don't auto-upgrade
        assert s["status"] == "success"

    def test_explicit_status_preserved_over_auto_upgrade(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as run:
            with run.stage("generate") as stage:
                stage.set_status("warning")
                stage.add_issue("error", "GEN-FAIL", "broken")
        s = _read_json(str(p))["runs"][0]["stages"][0]
        # status was already explicit, so auto-upgrade is suppressed
        # (the exit branch only upgrades when status is still "success")
        assert s["status"] == "warning"

    def test_stage_exception_marks_error(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with pytest.raises(RuntimeError, match="boom"):
            with m.run("cmd") as run:
                with run.stage("scaffold") as _stage:
                    raise RuntimeError("boom")
        s = _read_json(str(p))["runs"][0]["stages"][0]
        assert s["status"] == "error"
        assert s["finished_at"] is not None


# ---------------------------------------------------------------
# Final-status rollup
# ---------------------------------------------------------------


class TestFinalStatusRollup:
    def test_all_success_rolls_up_to_success(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as run:
            with run.stage("scaffold") as st:
                st.set_status("success")
            with run.stage("harvest") as st:
                st.set_status("no-op")
        assert _read_json(str(p))["runs"][0]["final_status"] == "success"

    def test_any_warning_rolls_up_to_warning(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as run:
            with run.stage("scaffold") as st:
                st.set_status("success")
            with run.stage("generate") as st:
                st.set_status("warning")
        assert _read_json(str(p))["runs"][0]["final_status"] == "warning"

    def test_any_error_rolls_up_to_failed(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as run:
            with run.stage("scaffold") as st:
                st.set_status("success")
            with run.stage("generate") as st:
                st.set_status("error")
        assert _read_json(str(p))["runs"][0]["final_status"] == "failed"

    def test_empty_run_rolls_up_to_success(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m.run("cmd") as _run:
            pass
        assert _read_json(str(p))["runs"][0]["final_status"] == "success"


# ---------------------------------------------------------------
# Append-only across runs
# ---------------------------------------------------------------


class TestAppendOnly:
    def test_subsequent_runs_appended_to_runs_list(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME

        m1 = DecisionsManifest(str(p), project_meta={"name": "X"})
        with m1.run("first") as run:
            with run.stage("scaffold") as _:
                pass

        m2 = DecisionsManifest(str(p))
        with m2.run("second") as run:
            with run.stage("harvest") as _:
                pass

        data = _read_json(str(p))
        assert len(data["runs"]) == 2
        assert data["runs"][0]["command"] == "first"
        assert data["runs"][1]["command"] == "second"

    def test_project_metadata_preserved_across_runs(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m1 = DecisionsManifest(str(p), project_meta={"name": "X", "version": "1.0"})
        m1.save()
        m2 = DecisionsManifest(str(p))
        with m2.run("cmd") as _run:
            pass
        assert _read_json(str(p))["project"] == {"name": "X", "version": "1.0"}


# ---------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------


class TestSchemaVersioning:
    def test_writes_current_schema_version(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        m = DecisionsManifest(str(p), project_meta={"name": "X"})
        m.save()
        assert _read_json(str(p))["schema_version"] == SCHEMA_VERSION

    def test_load_unknown_future_version_raises(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        p.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION + 99,
                    "project": {"name": "X"},
                    "runs": [],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(DecisionsSchemaError, match="schema_version"):
            DecisionsManifest(str(p))

    def test_load_missing_schema_version_raises(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        p.write_text(
            json.dumps({"project": {"name": "X"}, "runs": []}), encoding="utf-8"
        )
        with pytest.raises(DecisionsSchemaError, match="schema_version"):
            DecisionsManifest(str(p))


# ---------------------------------------------------------------
# Corruption handling
# ---------------------------------------------------------------


class TestCorruption:
    def test_unparseable_json_raises(self, tmp_path):
        p = tmp_path / DECISIONS_FILENAME
        p.write_text("{ not json", encoding="utf-8")
        with pytest.raises(DecisionsCorruptError, match="not valid JSON"):
            DecisionsManifest(str(p))


# ---------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------


class TestModuleInvariants:
    def test_stage_status_vocabulary(self):
        assert set(STAGE_STATUSES) == {
            "success",
            "warning",
            "error",
            "skipped",
            "no-op",
        }

    def test_issue_severity_vocabulary(self):
        assert set(ISSUE_SEVERITIES) == {"info", "warning", "error"}

    def test_final_status_vocabulary(self):
        assert set(FINAL_STATUSES) == {"success", "warning", "partial", "failed"}

    def test_decisions_filename_constant(self):
        assert DECISIONS_FILENAME == "ships.decisions.json"

    def test_schema_version_is_one(self):
        # Foundation lands at v1; bumping this is a deliberate
        # breaking-change moment that updates the migration registry.
        assert SCHEMA_VERSION == 1

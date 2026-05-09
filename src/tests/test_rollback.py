"""
test_rollback.py — Tests for deployment rollback behaviour.

Covers:
    - _deploy_replace_in_place captures SHOW DDL before replacing when
      the object already exists (the rollback file is written to disk
      and returned in the ObjectDeployResult).
    - _deploy_replace_in_place does NOT attempt a capture when the
      object is new (nothing to capture; rollback_file is None).
    - _rollback_single restores a REPLACE_IN_PLACE object from the
      captured rollback file (drop + re-execute saved DDL).
    - _rollback_single drops a newly created object when there is no
      prior definition to restore.
    - rollback_package processes all eligible objects in reverse order.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from database_package_deployer.deployer import (
    _deploy_replace_in_place,
    _rollback_single,
    rollback_package,
)
from database_package_deployer.manifest import DeploymentManifest, MANIFEST_FILENAME
from database_package_deployer.models import (
    DeployIntent,
    DeployState,
    DeployStrategy,
    ObjectDeployResult,
    ObjectType,
    ParsedStatement,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_view_parsed(db="Dev", obj="v_active") -> ParsedStatement:
    """Minimal ParsedStatement for a REPLACE VIEW."""
    return ParsedStatement(
        file_path=f"DDL/views/{db}.{obj}.viw",
        ddl_text=f"REPLACE VIEW {db}.{obj} AS SELECT 1 AS x;",
        original_text=f"REPLACE VIEW {db}.{obj} AS SELECT 1 AS x;",
        database_name=db,
        object_name=obj,
        object_type=ObjectType.VIEW,
        strategy=DeployStrategy.REPLACE_IN_PLACE,
        qualified_name=f"{db}.{obj}",
        deploy_intent=DeployIntent.IDEMPOTENT_DEPLOY,
    )


def _make_cursor(*, object_exists: bool, show_rows=None):
    """Return a mock cursor simulating object-existence checks and SHOW."""
    cursor = MagicMock()

    # _object_exists calls cursor.execute then fetchone — return a row
    # when the object is present, None when absent.
    if object_exists:
        cursor.fetchone.return_value = ("V",)
    else:
        cursor.fetchone.return_value = None

    # SHOW command returns DDL as a list of single-column rows.
    cursor.fetchall.return_value = [[row] for row in show_rows] if show_rows else []

    return cursor


# ---------------------------------------------------------------
# _deploy_replace_in_place — rollback file capture
# ---------------------------------------------------------------


class TestDeployReplaceInPlace:
    """Verify SHOW capture happens before REPLACE when the object exists."""

    def test_captures_rollback_file_when_object_exists(self, tmp_path):
        """
        When a view already exists, _deploy_replace_in_place must run
        SHOW VIEW and save the DDL to _rollback/ before executing REPLACE.
        The rollback file path is returned in ObjectDeployResult.rollback_file.
        """
        parsed = _make_view_parsed()
        manifest = DeploymentManifest(str(tmp_path))
        manifest.register_object(
            qualified_name=parsed.qualified_name,
            ddl_file="DDL/views/Dev.v_active.viw",
            object_type="VIEW",
        )

        prior_ddl = "REPLACE VIEW Dev.v_active AS SELECT 0 AS old_col;"
        cursor = _make_cursor(object_exists=True, show_rows=[prior_ddl])

        result = _deploy_replace_in_place(cursor, parsed, manifest, dry_run=False)

        assert result.state == DeployState.COMPLETED
        assert result.rollback_file is not None, (
            "rollback_file must be set when the object existed before deployment"
        )
        assert os.path.exists(result.rollback_file), (
            "rollback file must be written to disk"
        )
        saved = open(result.rollback_file, encoding="utf-8").read()
        assert prior_ddl in saved, (
            "saved rollback file must contain the captured SHOW output"
        )

    def test_no_rollback_file_for_new_object(self, tmp_path):
        """
        When the object does not exist before deployment there is
        nothing to capture — rollback_file must be None.
        """
        parsed = _make_view_parsed()
        manifest = DeploymentManifest(str(tmp_path))
        manifest.register_object(
            qualified_name=parsed.qualified_name,
            ddl_file="DDL/views/Dev.v_active.viw",
            object_type="VIEW",
        )

        cursor = _make_cursor(object_exists=False)

        result = _deploy_replace_in_place(cursor, parsed, manifest, dry_run=False)

        assert result.state == DeployState.COMPLETED
        assert result.rollback_file is None, (
            "no rollback file expected for a newly created object"
        )

    def test_dry_run_does_not_write_rollback_file(self, tmp_path):
        """In dry-run mode, no DDL is executed and no rollback file is written."""
        parsed = _make_view_parsed()
        manifest = DeploymentManifest(str(tmp_path))
        cursor = _make_cursor(
            object_exists=True,
            show_rows=["REPLACE VIEW Dev.v_active AS SELECT 1;"],
        )

        result = _deploy_replace_in_place(cursor, parsed, manifest, dry_run=True)

        assert result.state == DeployState.COMPLETED
        assert result.dry_run is True
        rollback_dir = os.path.join(str(tmp_path), "_rollback")
        assert not os.path.isdir(rollback_dir) or not os.listdir(rollback_dir), (
            "no rollback files should be written during dry-run"
        )


# ---------------------------------------------------------------
# _rollback_single — REPLACE_IN_PLACE path
# ---------------------------------------------------------------


class TestRollbackSingleReplaceInPlace:
    """Verify _rollback_single restores REPLACE_IN_PLACE objects correctly."""

    def _seed_with_rollback_file(self, tmp_path, prior_ddl: str):
        """
        Set up a manifest recording a COMPLETED view deployment that
        has a rollback file containing prior_ddl, simulating what
        _deploy_replace_in_place writes during a live deployment.
        """
        manifest = DeploymentManifest(str(tmp_path))
        manifest.register_object(
            qualified_name="Dev.v_active",
            ddl_file="DDL/views/Dev.v_active.viw",
            object_type="VIEW",
        )

        rollback_dir = os.path.join(str(tmp_path), "_rollback")
        os.makedirs(rollback_dir, exist_ok=True)
        rollback_path = os.path.join(rollback_dir, "Dev.v_active.sql")
        with open(rollback_path, "w", encoding="utf-8") as f:
            f.write(prior_ddl)

        manifest.update_state(
            "Dev.v_active",
            DeployState.COMPLETED,
            rollback_file=rollback_path,
        )
        return manifest

    def test_restores_prior_definition_from_rollback_file(self, tmp_path):
        """
        _rollback_single must:
        1. DROP the current (replaced) view.
        2. Re-execute the DDL from the rollback file to restore the
           prior definition.
        3. Update manifest state to ROLLED_BACK.
        """
        prior_ddl = "REPLACE VIEW Dev.v_active AS SELECT 0 AS old_col;"
        manifest = self._seed_with_rollback_file(tmp_path, prior_ddl)

        parsed = _make_view_parsed()
        cursor = _make_cursor(object_exists=True)

        result = _rollback_single(cursor, "Dev.v_active", parsed, manifest)

        assert result.state == DeployState.ROLLED_BACK
        assert manifest.get_state("Dev.v_active") == DeployState.ROLLED_BACK

        executed_sql = [
            str(c.args[0]) if c.args else "" for c in cursor.execute.call_args_list
        ]
        # _execute_ddl strips trailing semicolons when splitting; match
        # on a distinctive substring of the prior DDL instead.
        prior_signature = "SELECT 0 AS old_col"
        assert any(prior_signature in sql for sql in executed_sql), (
            f"prior DDL must be re-executed to restore the definition. "
            f"Calls: {executed_sql}"
        )

    def test_drops_object_when_no_rollback_file(self, tmp_path):
        """
        For a newly created object (no rollback file), rollback must
        drop the object and record ROLLED_BACK with a note that no
        prior definition existed.
        """
        manifest = DeploymentManifest(str(tmp_path))
        manifest.register_object(
            qualified_name="Dev.v_new",
            ddl_file="DDL/views/Dev.v_new.viw",
            object_type="VIEW",
        )
        manifest.update_state("Dev.v_new", DeployState.COMPLETED)

        parsed = _make_view_parsed(obj="v_new")
        cursor = _make_cursor(object_exists=True)

        result = _rollback_single(cursor, "Dev.v_new", parsed, manifest)

        assert result.state == DeployState.ROLLED_BACK
        drop_calls = [
            str(c.args[0]) if c.args else "" for c in cursor.execute.call_args_list
        ]
        assert any("DROP" in sql.upper() for sql in drop_calls), (
            f"DROP must be issued for a new object with no prior definition. "
            f"Calls: {drop_calls}"
        )

    def test_missing_rollback_file_on_disk_falls_through(self, tmp_path):
        """
        If the manifest records a rollback_file path but the file has
        been deleted from disk, rollback gracefully falls through to
        drop-only — it does not raise.
        """
        manifest = DeploymentManifest(str(tmp_path))
        manifest.register_object(
            qualified_name="Dev.v_active",
            ddl_file="DDL/views/Dev.v_active.viw",
            object_type="VIEW",
        )
        manifest.update_state(
            "Dev.v_active",
            DeployState.COMPLETED,
            rollback_file="/nonexistent/path/rollback.sql",
        )

        parsed = _make_view_parsed()
        cursor = _make_cursor(object_exists=True)

        result = _rollback_single(cursor, "Dev.v_active", parsed, manifest)

        # Still marks as ROLLED_BACK (fell through to drop-only path)
        assert result.state == DeployState.ROLLED_BACK


# ---------------------------------------------------------------
# rollback_package — integration-level ordering test
# ---------------------------------------------------------------


class TestRollbackPackage:
    """Verify rollback_package processes candidates in reverse deploy order."""

    def test_processes_objects_in_reverse_order(self, tmp_path):
        """
        Objects deployed in order (v_a first, v_b second) must be
        rolled back in reverse (v_b first, v_a second).
        """
        manifest = DeploymentManifest(str(tmp_path))

        for name in ["Dev.v_a", "Dev.v_b"]:
            manifest.register_object(
                qualified_name=name,
                ddl_file=f"DDL/views/{name}.viw",
                object_type="VIEW",
            )
            manifest.update_state(name, DeployState.COMPLETED)

        processed_order = []

        def fake_rollback_single(cursor, qn, parsed, mfst, dry_run=False):
            processed_order.append(qn)
            mfst.update_state(qn, DeployState.ROLLED_BACK)
            return ObjectDeployResult(
                database_name=qn.split(".")[0],
                object_name=qn.split(".")[1],
                object_type=ObjectType.VIEW,
                state=DeployState.ROLLED_BACK,
                message=f"Rolled back {qn}",
            )

        with patch(
            "database_package_deployer.deployer._rollback_single",
            side_effect=fake_rollback_single,
        ):
            result = rollback_package(MagicMock(), manifest.path)

        assert processed_order == ["Dev.v_b", "Dev.v_a"], (
            f"expected reverse order but got: {processed_order}"
        )
        assert result.rolled_back == 2


# ---------------------------------------------------------------
# Rollback dry-run
# ---------------------------------------------------------------


class TestRollbackDryRun:
    """
    Verify rollback --dry-run previews planned actions without
    executing DDL or mutating the manifest.
    """

    def test_dry_run_describes_table_restore(self, tmp_path):
        """Table with a backup: dry-run reports the rename plan."""
        manifest = DeploymentManifest(str(tmp_path))
        manifest.register_object(
            qualified_name="Dev.Customer",
            ddl_file="DDL/tables/Dev.Customer.tbl",
            object_type="TABLE",
        )
        manifest.update_state(
            "Dev.Customer",
            DeployState.COMPLETED,
            backup_table="Dev.Customer_bk_20260509",
        )

        result = rollback_package(MagicMock(), manifest.path, dry_run=True)

        assert len(result.results) == 1
        r = result.results[0]
        assert r.dry_run is True
        assert "DRY RUN" in r.message
        assert "Customer_bk_20260509" in r.message

    def test_dry_run_describes_view_restore_from_rollback_file(self, tmp_path):
        """View with a rollback file on disk: dry-run confirms it can be restored."""
        rollback_dir = tmp_path / "_rollback"
        rollback_dir.mkdir()
        rollback_file = rollback_dir / "Dev.v_active.sql"
        rollback_file.write_text(
            "REPLACE VIEW Dev.v_active AS SELECT 0;", encoding="utf-8"
        )

        manifest = DeploymentManifest(str(tmp_path))
        manifest.register_object(
            qualified_name="Dev.v_active",
            ddl_file="DDL/views/Dev.v_active.viw",
            object_type="VIEW",
        )
        manifest.update_state(
            "Dev.v_active",
            DeployState.COMPLETED,
            rollback_file=str(rollback_file),
        )

        result = rollback_package(MagicMock(), manifest.path, dry_run=True)

        r = result.results[0]
        assert r.dry_run is True
        assert "DRY RUN" in r.message
        assert "Dev.v_active.sql" in r.message

    def test_dry_run_flags_missing_rollback_file(self, tmp_path):
        """Rollback file recorded in manifest but deleted from disk: dry-run warns."""
        manifest = DeploymentManifest(str(tmp_path))
        manifest.register_object(
            qualified_name="Dev.v_active",
            ddl_file="DDL/views/Dev.v_active.viw",
            object_type="VIEW",
        )
        manifest.update_state(
            "Dev.v_active",
            DeployState.COMPLETED,
            rollback_file="/deleted/Dev.v_active.sql",
        )

        result = rollback_package(MagicMock(), manifest.path, dry_run=True)

        r = result.results[0]
        assert r.dry_run is True
        assert "CANNOT" in r.message or "missing" in r.message.lower()

    def test_dry_run_does_not_mutate_manifest(self, tmp_path):
        """Manifest state must be unchanged after a dry-run rollback."""
        import json

        manifest = DeploymentManifest(str(tmp_path))
        manifest.register_object(
            qualified_name="Dev.v_active",
            ddl_file="DDL/views/Dev.v_active.viw",
            object_type="VIEW",
        )
        manifest.update_state("Dev.v_active", DeployState.COMPLETED)

        # Read manifest before dry-run
        before = json.loads(open(manifest.path, encoding="utf-8").read())

        rollback_package(MagicMock(), manifest.path, dry_run=True)

        # Manifest must be identical after dry-run
        after = json.loads(open(manifest.path, encoding="utf-8").read())
        assert before["objects"] == after["objects"], (
            "dry-run must not mutate manifest object states"
        )
        assert before.get("status") == after.get("status"), (
            "dry-run must not mutate package-level status"
        )

    def test_dry_run_requires_no_database_connection(self, tmp_path):
        """Dry-run works with cursor=None — no DB connection needed."""
        manifest = DeploymentManifest(str(tmp_path))
        manifest.register_object(
            qualified_name="Dev.v_active",
            ddl_file="DDL/views/Dev.v_active.viw",
            object_type="VIEW",
        )
        manifest.update_state("Dev.v_active", DeployState.COMPLETED)

        # cursor=None simulates the CLI passing no connection in dry-run mode
        result = rollback_package(None, manifest.path, dry_run=True)

        assert len(result.results) == 1
        assert result.results[0].dry_run is True


# ---------------------------------------------------------------
# Wave-scoped rollback (issue #90)
# ---------------------------------------------------------------


class TestWaveRollback:
    """
    Wave-scoped rollback: --wave N rolls back only objects deployed
    in wave N, leaving other waves untouched.

    Test requirements from issue #90:
    - Wave-filtered candidate selection
    - Reverse-order processing within the filtered set
    - --dry-run describes planned action without executing DDL
    - --dry-run does not mutate manifest
    - --dry-run works without a database connection
    - PARTIALLY_ROLLED_BACK package status when only one wave rolled back
    """

    def _seed_two_wave_manifest(self, tmp_path):
        """
        Create a manifest with objects in two waves:
          wave 1: Dev.v_w1   (VIEW, COMPLETED)
          wave 2: Dev.v_w2a  (VIEW, COMPLETED)
                  Dev.v_w2b  (VIEW, COMPLETED)
        Returns the manifest object.
        """
        manifest = DeploymentManifest(str(tmp_path))
        for name, wave in [
            ("Dev.v_w1", 1),
            ("Dev.v_w2a", 2),
            ("Dev.v_w2b", 2),
        ]:
            manifest.register_object(
                qualified_name=name,
                ddl_file=f"DDL/views/{name}.viw",
                object_type="VIEW",
                wave_number=wave,
            )
            manifest.update_state(name, DeployState.COMPLETED)
        return manifest

    def test_wave_filter_selects_only_matching_wave(self, tmp_path):
        """get_rollback_candidates(wave_number=2) returns only wave-2 objects."""
        manifest = self._seed_two_wave_manifest(tmp_path)
        candidates = manifest.get_rollback_candidates(wave_number=2)
        assert set(candidates) == {"Dev.v_w2a", "Dev.v_w2b"}
        assert "Dev.v_w1" not in candidates

    def test_wave_filter_excludes_null_wave_objects(self, tmp_path):
        """Objects with wave_number=None are excluded from wave-scoped rollback."""
        manifest = DeploymentManifest(str(tmp_path))
        manifest.register_object(
            qualified_name="Dev.serial_obj",
            ddl_file="DDL/tables/Dev.serial_obj.tbl",
            object_type="TABLE",
            wave_number=None,
        )
        manifest.update_state("Dev.serial_obj", DeployState.COMPLETED)
        # wave_number=1 — serial object (wave=None) must be excluded
        candidates = manifest.get_rollback_candidates(wave_number=1)
        assert "Dev.serial_obj" not in candidates

    def test_wave_rollback_processes_in_reverse_order(self, tmp_path):
        """Objects within the wave are processed in reverse deployment order."""
        manifest = self._seed_two_wave_manifest(tmp_path)

        processed = []

        def fake_rollback(cursor, qn, parsed, mfst, dry_run=False):
            processed.append(qn)
            mfst.update_state(qn, DeployState.ROLLED_BACK)
            return ObjectDeployResult(
                database_name=qn.split(".")[0],
                object_name=qn.split(".")[1],
                object_type=ObjectType.VIEW,
                state=DeployState.ROLLED_BACK,
                message=f"ok",
            )

        with patch(
            "database_package_deployer.deployer._rollback_single",
            side_effect=fake_rollback,
        ):
            rollback_package(MagicMock(), manifest.path, wave_number=2)

        # v_w2b was registered after v_w2a → reversed → v_w2b first
        assert processed == ["Dev.v_w2b", "Dev.v_w2a"]

    def test_wave_rollback_sets_partially_rolled_back_status(self, tmp_path):
        """Wave rollback sets PARTIALLY_ROLLED_BACK, not ROLLED_BACK."""
        manifest = self._seed_two_wave_manifest(tmp_path)

        def fake_rollback(cursor, qn, parsed, mfst, dry_run=False):
            mfst.update_state(qn, DeployState.ROLLED_BACK)
            return ObjectDeployResult(
                database_name=qn.split(".")[0],
                object_name=qn.split(".")[1],
                object_type=ObjectType.VIEW,
                state=DeployState.ROLLED_BACK,
                message="ok",
            )

        with patch(
            "database_package_deployer.deployer._rollback_single",
            side_effect=fake_rollback,
        ):
            rollback_package(MagicMock(), manifest.path, wave_number=2)

        import json

        data = json.loads(open(manifest.path, encoding="utf-8").read())
        assert data.get("status") == "PARTIALLY_ROLLED_BACK"

    def test_full_rollback_sets_rolled_back_status(self, tmp_path):
        """Package rollback (no wave filter) sets ROLLED_BACK."""
        manifest = self._seed_two_wave_manifest(tmp_path)

        def fake_rollback(cursor, qn, parsed, mfst, dry_run=False):
            mfst.update_state(qn, DeployState.ROLLED_BACK)
            return ObjectDeployResult(
                database_name=qn.split(".")[0],
                object_name=qn.split(".")[1],
                object_type=ObjectType.VIEW,
                state=DeployState.ROLLED_BACK,
                message="ok",
            )

        with patch(
            "database_package_deployer.deployer._rollback_single",
            side_effect=fake_rollback,
        ):
            rollback_package(MagicMock(), manifest.path)

        import json

        data = json.loads(open(manifest.path, encoding="utf-8").read())
        assert data.get("status") == "ROLLED_BACK"

    def test_wave_dry_run_does_not_mutate_manifest(self, tmp_path):
        """Wave dry-run leaves manifest completely unchanged."""
        import json

        manifest = self._seed_two_wave_manifest(tmp_path)
        before = json.loads(open(manifest.path, encoding="utf-8").read())

        rollback_package(None, manifest.path, dry_run=True, wave_number=2)

        after = json.loads(open(manifest.path, encoding="utf-8").read())
        assert before["objects"] == after["objects"]
        assert before.get("status") == after.get("status")

    def test_wave_dry_run_needs_no_connection(self, tmp_path):
        """Wave dry-run works with cursor=None."""
        manifest = self._seed_two_wave_manifest(tmp_path)
        result = rollback_package(None, manifest.path, dry_run=True, wave_number=2)
        assert len(result.results) == 2
        assert all(r.dry_run is True for r in result.results)

    def test_wave_dry_run_only_describes_wave_objects(self, tmp_path):
        """Wave dry-run output includes wave-2 objects but not wave-1."""
        manifest = self._seed_two_wave_manifest(tmp_path)
        result = rollback_package(None, manifest.path, dry_run=True, wave_number=2)
        names = {f"{r.database_name}.{r.object_name}" for r in result.results}
        assert names == {"Dev.v_w2a", "Dev.v_w2b"}
        assert "Dev.v_w1" not in str(names)

"""
test_manifest_replay.py — Tests for the manifest replay-bug fix.

Covers the scenario where the deployer loads an existing
.deploy_manifest.json, finds objects marked COMPLETED, and skips
execution — but the underlying database has been dropped or cleaned
since the prior run, so the manifest is stale.

Tests:
    - prepare_for_redeploy() resets COMPLETED entries whose objects
      no longer exist in the database, and leaves intact those that
      still exist.
    - get_prior_completed() filters correctly.
    - register_object() does not overwrite a COMPLETED entry.
    - resume_package() invokes prepare_for_redeploy() on entry
      (closes the same bug pattern in the resume code path).
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from ddl_deployer.deployer import resume_package
from ddl_deployer.manifest import DeploymentManifest, MANIFEST_FILENAME
from ddl_deployer.models import DeployState


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _seed_completed_object(manifest, qualified_name, object_type="TABLE"):
    """Register a COMPLETED entry with a completed_at timestamp."""
    manifest.register_object(
        qualified_name=qualified_name,
        ddl_file=f"{qualified_name}.tbl",
        object_type=object_type,
    )
    manifest.update_state(qualified_name, DeployState.COMPLETED)


# ---------------------------------------------------------------
# prepare_for_redeploy
# ---------------------------------------------------------------


class TestPrepareForRedeploy:
    """Tests for DeploymentManifest.prepare_for_redeploy()."""

    def test_resets_when_object_missing(self, tmp_path):
        """An object missing from the database is reset to PENDING."""
        m = DeploymentManifest(str(tmp_path))
        _seed_completed_object(m, "DEV01_DB.MissingTable")

        # verify_exists_fn returns False — object is absent in DB
        reset = m.prepare_for_redeploy(
            verify_exists_fn=lambda cur, qn: False,
            cursor=MagicMock(),
        )

        assert "DEV01_DB.MissingTable" in reset
        assert m.get_state("DEV01_DB.MissingTable") == DeployState.PENDING

    def test_keeps_completed_when_object_present(self, tmp_path):
        """An object still in the database stays COMPLETED."""
        m = DeploymentManifest(str(tmp_path))
        _seed_completed_object(m, "DEV01_DB.LiveTable")

        reset = m.prepare_for_redeploy(
            verify_exists_fn=lambda cur, qn: True,
            cursor=MagicMock(),
        )

        assert reset == []
        assert m.get_state("DEV01_DB.LiveTable") == DeployState.COMPLETED

    def test_no_completed_objects_returns_empty(self, tmp_path):
        """No COMPLETED entries → no work, no calls."""
        m = DeploymentManifest(str(tmp_path))
        m.register_object("DEV01_DB.PendingOnly", "PendingOnly.tbl")

        called = []

        def checker(cursor, qn):
            called.append(qn)
            return False

        reset = m.prepare_for_redeploy(checker, MagicMock())

        assert reset == []
        assert called == []  # no completed → checker never invoked

    def test_partial_reset(self, tmp_path):
        """Only objects missing from DB are reset; others left intact."""
        m = DeploymentManifest(str(tmp_path))
        _seed_completed_object(m, "DEV01_DB.LiveOne")
        _seed_completed_object(m, "DEV01_DB.GoneOne")
        _seed_completed_object(m, "DEV01_DB.LiveTwo")

        # GoneOne is absent, LiveOne and LiveTwo still exist
        existing = {"DEV01_DB.LiveOne", "DEV01_DB.LiveTwo"}
        reset = m.prepare_for_redeploy(
            verify_exists_fn=lambda cur, qn: qn in existing,
            cursor=MagicMock(),
        )

        assert reset == ["DEV01_DB.GoneOne"]
        assert m.get_state("DEV01_DB.LiveOne") == DeployState.COMPLETED
        assert m.get_state("DEV01_DB.GoneOne") == DeployState.PENDING
        assert m.get_state("DEV01_DB.LiveTwo") == DeployState.COMPLETED

    def test_check_failure_keeps_completed_as_safe_default(self, tmp_path):
        """Existence check raising → leave COMPLETED untouched."""
        m = DeploymentManifest(str(tmp_path))
        _seed_completed_object(m, "DEV01_DB.UncheckedTable")

        def boom(cursor, qn):
            raise RuntimeError("DBC unavailable")

        reset = m.prepare_for_redeploy(boom, MagicMock())

        assert reset == []
        assert m.get_state("DEV01_DB.UncheckedTable") == DeployState.COMPLETED

    def test_reset_clears_artefacts(self, tmp_path):
        """Reset clears timestamps, error, backup, etc."""
        m = DeploymentManifest(str(tmp_path))
        _seed_completed_object(m, "DEV01_DB.OldTable")
        # Stamp some artefacts on the record
        m.update_state(
            "DEV01_DB.OldTable",
            DeployState.COMPLETED,
            backup_table="OldTable_bkp_20260101",
            rows_migrated=42,
            error="prior error message",
        )

        m.prepare_for_redeploy(
            verify_exists_fn=lambda cur, qn: False,
            cursor=MagicMock(),
        )

        rec = m.get_record("DEV01_DB.OldTable")
        assert rec["state"] == DeployState.PENDING.value
        assert rec["backup_table"] is None
        assert rec["rows_migrated"] == 0
        assert rec["error"] is None
        assert rec["started_at"] is None
        assert rec["completed_at"] is None


# ---------------------------------------------------------------
# get_prior_completed
# ---------------------------------------------------------------


class TestGetPriorCompleted:
    """Tests for DeploymentManifest.get_prior_completed()."""

    def test_returns_completed_with_timestamp(self, tmp_path):
        """COMPLETED entries with completed_at are returned."""
        m = DeploymentManifest(str(tmp_path))
        _seed_completed_object(m, "DEV01_DB.A")
        _seed_completed_object(m, "DEV01_DB.B")

        prior = m.get_prior_completed()

        names = {p["qualified_name"] for p in prior}
        assert names == {"DEV01_DB.A", "DEV01_DB.B"}

    def test_excludes_pending(self, tmp_path):
        """PENDING entries are not prior-completed."""
        m = DeploymentManifest(str(tmp_path))
        m.register_object("DEV01_DB.NotDoneYet", "NotDoneYet.tbl")

        assert m.get_prior_completed() == []

    def test_excludes_failed(self, tmp_path):
        """FAILED entries are not prior-completed."""
        m = DeploymentManifest(str(tmp_path))
        m.register_object("DEV01_DB.Broken", "Broken.tbl")
        m.update_state("DEV01_DB.Broken", DeployState.FAILED, error="boom")

        assert m.get_prior_completed() == []


# ---------------------------------------------------------------
# register_object on COMPLETED
# ---------------------------------------------------------------


class TestRegisterObjectOnCompleted:
    """Re-registering a COMPLETED object must not regress its state."""

    def test_completed_state_preserved(self, tmp_path):
        """register_object on a COMPLETED entry leaves it COMPLETED."""
        m = DeploymentManifest(str(tmp_path))
        _seed_completed_object(m, "DEV01_DB.AlreadyDone")

        # Simulate a fresh deploy run re-registering the same object
        m.register_object(
            qualified_name="DEV01_DB.AlreadyDone",
            ddl_file="AlreadyDone.tbl",
            wave_number=1,
            deploy_intent="IDEMPOTENT_DEPLOY",
            object_type="TABLE",
        )

        assert m.get_state("DEV01_DB.AlreadyDone") == DeployState.COMPLETED


# ---------------------------------------------------------------
# Manifest reload preserves COMPLETED state across processes
# ---------------------------------------------------------------


class TestManifestReload:
    """A new DeploymentManifest on the same dir loads the prior file."""

    def test_completed_persists_across_reload(self, tmp_path):
        """COMPLETED entries survive a fresh DeploymentManifest()."""
        m1 = DeploymentManifest(str(tmp_path))
        _seed_completed_object(m1, "DEV01_DB.Persistent")

        # Sanity: file exists on disc
        assert os.path.exists(os.path.join(str(tmp_path), MANIFEST_FILENAME))

        m2 = DeploymentManifest(str(tmp_path))
        assert m2.get_state("DEV01_DB.Persistent") == DeployState.COMPLETED
        assert m2.deployment_id == m1.deployment_id

    def test_manifest_file_is_valid_json(self, tmp_path):
        """The persisted manifest is parseable JSON with expected keys."""
        m = DeploymentManifest(str(tmp_path))
        _seed_completed_object(m, "DEV01_DB.X")

        with open(os.path.join(str(tmp_path), MANIFEST_FILENAME), encoding="utf-8") as f:
            data = json.load(f)

        assert "deployment_id" in data
        assert "objects" in data
        assert "DEV01_DB.X" in data["objects"]


# ---------------------------------------------------------------
# resume_package() — fix A
# ---------------------------------------------------------------


class TestResumePackageRedeployCheck:
    """resume_package must verify stale COMPLETED entries before resuming."""

    def test_resume_invokes_prepare_for_redeploy(self, tmp_path):
        """resume_package calls prepare_for_redeploy with the live cursor."""
        m = DeploymentManifest(str(tmp_path))
        _seed_completed_object(m, "DEV01_DB.Whatever")
        manifest_path = m.path

        cursor = MagicMock()
        with patch.object(
            DeploymentManifest,
            "prepare_for_redeploy",
            return_value=[],
        ) as spy:
            resume_package(cursor, manifest_path)

        assert spy.called, "resume_package did not call prepare_for_redeploy"
        # Second positional arg is the cursor (first is the checker)
        args, kwargs = spy.call_args
        assert args[1] is cursor or kwargs.get("cursor") is cursor

    def test_resume_skips_check_in_dry_run(self, tmp_path):
        """dry_run=True must NOT call prepare_for_redeploy (no live DB)."""
        m = DeploymentManifest(str(tmp_path))
        _seed_completed_object(m, "DEV01_DB.X")
        manifest_path = m.path

        with patch.object(
            DeploymentManifest,
            "prepare_for_redeploy",
            return_value=[],
        ) as spy:
            resume_package(MagicMock(), manifest_path, dry_run=True)

        assert not spy.called, (
            "prepare_for_redeploy should not run in dry-run mode"
        )

    def test_resume_skips_check_when_cursor_is_none(self, tmp_path):
        """cursor=None must NOT call prepare_for_redeploy (defensive)."""
        m = DeploymentManifest(str(tmp_path))
        _seed_completed_object(m, "DEV01_DB.X")
        manifest_path = m.path

        with patch.object(
            DeploymentManifest,
            "prepare_for_redeploy",
            return_value=[],
        ) as spy:
            resume_package(None, manifest_path)

        assert not spy.called, (
            "prepare_for_redeploy should not run when cursor is None"
        )

    def test_resume_missing_manifest_raises(self, tmp_path):
        """resume_package raises FileNotFoundError when manifest is absent."""
        with pytest.raises(FileNotFoundError):
            resume_package(MagicMock(), str(tmp_path / "nope.json"))

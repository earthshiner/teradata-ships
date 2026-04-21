"""
manifest.py — Deployment manifest for restartability.

The manifest is a JSON file persisted alongside the DDL package that
records the state of each table deployment. It is written after every
state transition, so the process can resume from the exact failure
point or roll back completed operations.

Manifest structure:
    {
        "deployment_id": "deploy_20260418_143022",
        "package_dir": "/path/to/ddl/files",
        "started_at": "2026-04-18T14:30:22.000000",
        "updated_at": "2026-04-18T14:31:45.000000",
        "status": "IN_PROGRESS",
        "objects": {
            "DEV01_MyDB.MyTable": {
                "ddl_file": "DEV01_MyDB.MyTable.tbl",
                "state": "COMPLETED",
                "backup_table": "MyTable_bkp_20260418143022",
                "rows_migrated": 1500,
                "started_at": "2026-04-18T14:30:23.000000",
                "completed_at": "2026-04-18T14:30:28.000000",
                "error": null,
                "blockers": [],
                "warnings": []
            }
        }
    }
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ddl_deployer.models import DeployState

logger = logging.getLogger(__name__)

# -- Manifest filename convention --
MANIFEST_FILENAME = ".deploy_manifest.json"


class DeploymentManifest:
    """
    Manages the deployment state file for a DDL package.

    The manifest is the single source of truth for what has been
    attempted, completed, and failed. It is designed to survive
    process crashes — each state transition is flushed to disc
    before the next operation begins.

    Attributes:
        path:           Full path to the manifest JSON file.
        deployment_id:  Unique identifier for this deployment run.
        data:           The in-memory manifest dictionary.
    """

    def __init__(self, package_dir: str, deployment_id: Optional[str] = None):
        """
        Initialise a manifest for a package directory.

        If a manifest already exists on disc, it is loaded (for resume).
        Otherwise, a new manifest is created with the given deployment_id.

        Args:
            package_dir:    Directory containing the DDL files.
            deployment_id:  Unique ID for a new deployment. Ignored if
                            loading an existing manifest.
        """
        self.path = os.path.join(package_dir, MANIFEST_FILENAME)

        if os.path.exists(self.path):
            self._load()
            logger.info(
                "Loaded existing manifest: %s (deployment: %s)",
                self.path, self.data.get("deployment_id")
            )
        else:
            if deployment_id is None:
                deployment_id = _generate_deployment_id()

            self.deployment_id = deployment_id
            self.data = {
                "deployment_id": deployment_id,
                "package_dir": os.path.abspath(package_dir),
                "started_at": _now_iso(),
                "updated_at": _now_iso(),
                "status": "IN_PROGRESS",
                "objects": {},
            }
            self._save()
            logger.info("Created new manifest: %s", self.path)

    def _load(self):
        """Load the manifest from disc."""
        with open(self.path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        self.deployment_id = self.data["deployment_id"]

    def _save(self):
        """
        Persist the manifest to disc.

        Uses write-to-temp-then-rename for atomicity. Includes a
        retry loop for Windows, where antivirus scanners and file
        indexers can briefly lock newly-created files.
        """
        self.data["updated_at"] = _now_iso()
        tmp_path = self.path + ".tmp"

        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        # Atomic replace — with retry for Windows file locking
        import stat
        import time

        for attempt in range(5):
            try:
                os.replace(tmp_path, self.path)
                break
            except PermissionError:
                if attempt < 4:
                    # Clear read-only on target if it exists
                    if os.path.exists(self.path):
                        try:
                            os.chmod(self.path, stat.S_IWRITE | stat.S_IREAD)
                        except Exception:
                            pass
                    time.sleep(0.1 * (attempt + 1))
                else:
                    # Final attempt: remove target then rename
                    if os.path.exists(self.path):
                        os.chmod(self.path, stat.S_IWRITE | stat.S_IREAD)
                        os.remove(self.path)
                    os.rename(tmp_path, self.path)

        logger.debug("Manifest saved: %s", self.path)

    def register_object(self, qualified_name: str, ddl_file: str,
                       wave_number: Optional[int] = None,
                       deploy_intent: Optional[str] = None,
                       object_type: Optional[str] = None):
        """
        Register an object in the manifest as PENDING.

        Args:
            qualified_name: Fully qualified 'Database.Object' identifier.
            ddl_file:       Filename of the DDL file.
            wave_number:    Wave number (1-based) if using wave-parallel.
            deploy_intent:  DeployIntent value string.
            object_type:    ObjectType value string (TABLE, VIEW, etc.).
        """
        if qualified_name not in self.data["objects"]:
            self.data["objects"][qualified_name] = {
                "ddl_file": ddl_file,
                "object_type": object_type,
                "state": DeployState.PENDING.value,
                "wave_number": wave_number,
                "deploy_intent": deploy_intent,
                "prior_existed": None,
                "rollback_file": None,
                "backup_table": None,
                "rows_migrated": 0,
                "started_at": None,
                "completed_at": None,
                "error": None,
                "blockers": [],
                "warnings": [],
            }
            self._save()
        else:
            existing_file = self.data["objects"][qualified_name].get("ddl_file", "?")
            logger.warning(
                "Duplicate qualified name '%s' — file '%s' collides "
                "with existing '%s'. Second file will be skipped.",
                qualified_name, ddl_file, existing_file,
            )

    def update_state(
        self,
        qualified_name: str,
        state: DeployState,
        backup_table: Optional[str] = None,
        rows_migrated: Optional[int] = None,
        error: Optional[str] = None,
        blockers: Optional[list] = None,
        warnings: Optional[list] = None,
        prior_existed: Optional[bool] = None,
        rollback_file: Optional[str] = None,
    ):
        """
        Transition a table to a new deployment state.

        Updates the manifest record and persists to disc immediately.

        Args:
            qualified_name: Fully qualified 'Database.Table' identifier.
            state:          The new DeployState.
            backup_table:   Name of backup table (if created/known).
            rows_migrated:  Count of rows migrated (if applicable).
            error:          Error message (for FAILED state).
            blockers:       Compatibility blockers (for SKIPPED state).
            warnings:       Non-fatal warnings.
        """
        record = self.data["objects"].get(qualified_name)
        if record is None:
            raise KeyError(
                f"Table '{qualified_name}' is not registered in the manifest."
            )

        record["state"] = state.value

        # Update optional fields only if provided
        if backup_table is not None:
            record["backup_table"] = backup_table
        if rows_migrated is not None:
            record["rows_migrated"] = rows_migrated
        if error is not None:
            record["error"] = error
        if blockers is not None:
            record["blockers"] = blockers
        if warnings is not None:
            record["warnings"] = warnings
        if prior_existed is not None:
            record["prior_existed"] = prior_existed
        if rollback_file is not None:
            record["rollback_file"] = rollback_file

        # Timestamp management
        if record["started_at"] is None:
            record["started_at"] = _now_iso()

        if state in (DeployState.COMPLETED, DeployState.SKIPPED,
                     DeployState.FAILED, DeployState.ROLLED_BACK):
            record["completed_at"] = _now_iso()

        self._save()

        logger.info(
            "State transition: %s → %s",
            qualified_name, state.value
        )

    def get_state(self, qualified_name: str) -> DeployState:
        """
        Get the current deployment state of a table.

        Args:
            qualified_name: Fully qualified 'Database.Table' identifier.

        Returns:
            The current DeployState.
        """
        record = self.data["objects"].get(qualified_name)
        if record is None:
            return DeployState.PENDING
        return DeployState(record["state"])

    def get_record(self, qualified_name: str) -> Optional[Dict[str, Any]]:
        """
        Get the full manifest record for a table.

        Args:
            qualified_name: Fully qualified 'Database.Table' identifier.

        Returns:
            The manifest record dictionary, or None if not registered.
        """
        return self.data["objects"].get(qualified_name)

    def get_tables_in_state(self, state: DeployState) -> list:
        """
        List all qualified table names currently in a given state.

        Args:
            state: The DeployState to filter by.

        Returns:
            List of qualified table names.
        """
        return [
            name for name, record in self.data["objects"].items()
            if record["state"] == state.value
        ]

    def get_pending_or_failed(self) -> list:
        """
        List tables that need processing: PENDING or FAILED.

        Used by the resume logic to determine what still needs work.

        Returns:
            List of qualified table names needing deployment.
        """
        resumable_states = {DeployState.PENDING.value, DeployState.FAILED.value}
        return [
            name for name, record in self.data["objects"].items()
            if record["state"] in resumable_states
        ]

    def get_rollback_candidates(self) -> list:
        """
        List tables that can be rolled back.

        A table is rollback-eligible if it has been backed up and/or
        created but not yet in a terminal safe state. The order is
        reversed (most recently processed first) for safe unwinding.

        Returns:
            List of qualified table names eligible for rollback,
            in reverse processing order.
        """
        rollback_states = {
            DeployState.BACKED_UP.value,
            DeployState.CREATED.value,
            DeployState.MIGRATED.value,
            DeployState.COMPLETED.value,
        }
        candidates = [
            name for name, record in self.data["objects"].items()
            if record["state"] in rollback_states
        ]
        # Reverse order — unwind most recent first
        return list(reversed(candidates))

    def set_package_status(self, status: str):
        """
        Set the overall package deployment status.

        Args:
            status: One of 'IN_PROGRESS', 'COMPLETED', 'FAILED',
                    'ROLLED_BACK', 'PARTIALLY_COMPLETED'.
        """
        self.data["status"] = status
        self._save()

    def summary(self) -> Dict[str, int]:
        """
        Count tables by state for reporting.

        Returns:
            Dictionary of state_name → count.
        """
        counts = {}
        for record in self.data["objects"].values():
            state = record["state"]
            counts[state] = counts.get(state, 0) + 1
        return counts


def _generate_deployment_id() -> str:
    """Generate a unique deployment ID from the current timestamp."""
    return datetime.now(timezone.utc).strftime("deploy_%Y%m%d_%H%M%S")


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()

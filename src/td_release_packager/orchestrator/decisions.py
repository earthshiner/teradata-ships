"""
decisions.py — Append-only writer for ``ships.decisions.json``.

``ships.decisions.json`` is the single source of truth for what SHIPS did
to a project: every stage of every run records its inputs, outputs,
resolved configuration, decisions, and issues into one file under the
machine-managed ``.ships/`` directory at the project root (resolved via
``project_paths.decisions_json_path``). ``td_release_packager explain``
reads it. CI uses it to compare runs. Auditors trust it.

This module supplies the writer. Reading / explaining is a separate
concern (build-order item 6).

Public API
----------
    DecisionsManifest(path, project_meta=None)
        Load an existing ships.decisions.json or create a new one in
        memory. Calling .save() persists.

    manifest.run(command, run_id=None) → context manager
        Open a new run. On exit, finalises started_at/finished_at,
        computes final_status, persists to disc.

    run.stage(name) → context manager
        Open a stage entry within the current run. On exit,
        finalises duration_ms and persists.

    stage.set_status(status)
        One of "success" / "warning" / "error" / "skipped" / "no-op".

    stage.set_config_resolved(name, value, source, source_path)
        Record an effective configuration setting and its provenance.

    stage.set_inputs(**fields)         arbitrary stage-defined keys
    stage.set_outputs(**fields)        arbitrary stage-defined keys
    stage.set_decisions(**fields)      stage-specific extension shape
    stage.add_issue(severity, code, message, location=None)

Concurrency / safety
--------------------
- ``threading.Lock`` on all mutating operations.
- Atomic file writes via tempfile + os.replace, with retry on
  Windows file-locking failures (matches the pattern used by
  ``database_package_deployer.manifest``).
- Append-only: prior runs are never rewritten or trimmed unless
  the caller explicitly invokes a (future) prune helper.

Schema versioning
-----------------
- ``SCHEMA_VERSION`` constant covers the **universal** shape only —
  top-level structure plus the five universal per-stage sections
  (config_resolved, inputs, outputs, decisions, issues).
- Adding optional fields → no version bump.
- Removing/renaming/changing semantics → bump.
- Reading a manifest with an unknown ``schema_version`` raises
  ``DecisionsSchemaError``. Older versions are auto-migrated in
  memory via ``MIGRATIONS`` (empty for v1; populated when v2 ships).

Failure modes (per the orchestrator design)
-------------------------------------------
- Run interrupted (Ctrl-C/crash): the partial run entry is preserved
  on disc with whichever stages had completed, plus
  ``interrupted: true`` if the caller marks it so. Distinguishable
  from a clean failure via that flag.
- Manifest corrupted (JSON parse fails): refuses to proceed; the
  caller is expected to back it up and recreate.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

# Re-exported (orchestrator/__init__) and used by tests as the canonical
# decisions filename. Sourced from project_paths so the name is defined
# exactly once across the codebase (issue #283).
from td_release_packager.project_paths import DECISIONS_FILENAME


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------

#: Current schema version. See module docstring for bump rules.
SCHEMA_VERSION: int = 1

#: Migration registry: ``{from_version: callable(data) -> data}``.
#: Each callable upgrades a manifest by exactly one version. Reading
#: a v1 manifest is a no-op; future v2/v3 entries plug in here.
MIGRATIONS: Dict[int, Any] = {
    # 1: lambda data: _v1_to_v2(data),  # populated when v2 lands
}

#: Stage status vocabulary.
STAGE_STATUSES = ("success", "warning", "error", "skipped", "no-op")

#: Issue severity vocabulary.
ISSUE_SEVERITIES = ("info", "warning", "error")

#: Run-level final status vocabulary.
FINAL_STATUSES = ("success", "warning", "partial", "failed")

# DECISIONS_FILENAME is imported from project_paths (see top of module).


# ---------------------------------------------------------------
# Errors
# ---------------------------------------------------------------


class DecisionsError(Exception):
    """Base class for ships.decisions.json errors."""


class DecisionsSchemaError(DecisionsError):
    """The on-disc manifest has an unknown / unsupported schema version."""


class DecisionsCorruptError(DecisionsError):
    """The on-disc manifest could not be parsed as JSON."""


# ---------------------------------------------------------------
# DecisionsManifest
# ---------------------------------------------------------------


class DecisionsManifest:
    """
    Append-only manifest of every run, with provenance.

    Construction is non-destructive: an existing file is loaded and
    migrated into memory; a missing file is initialised in memory
    and not persisted until ``save()`` runs (or a context-managed
    run completes).

    Use ``manifest.run(command)`` as a context manager to open a
    new run; use ``run.stage(name)`` within that to open a stage.
    Both context managers persist on exit, including on exception.

    Attributes:
        path:    Filesystem path to the manifest JSON.
        data:    The in-memory manifest dict (mutate via the context
                 managers; direct mutation is not recommended).
    """

    def __init__(
        self,
        path: str,
        project_meta: Optional[Dict[str, Any]] = None,
    ):
        """
        Load or initialise a ships.decisions.json.

        Args:
            path:         Filesystem path to the manifest. Typically
                          ``<project-root>/ships.decisions.json``.
            project_meta: Used only when creating a fresh manifest;
                          ignored when loading an existing one. Should
                          contain at least ``name``; ``version`` and
                          ``scaffolded_at`` are recommended.

        Raises:
            DecisionsCorruptError: The file exists but isn't valid JSON.
            DecisionsSchemaError:  The file has an unknown schema_version.
        """
        self.path = path
        self._lock = threading.Lock()

        if os.path.exists(path):
            self.data = self._load_and_migrate(path)
            return

        self.data = {
            "schema_version": SCHEMA_VERSION,
            "project": dict(project_meta) if project_meta else {},
            "runs": [],
        }

    # ---- persistence ------------------------------------------

    def save(self) -> None:
        """Persist the in-memory manifest to disc atomically."""
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        """Atomic write — caller must hold ``self._lock``."""
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(suffix=".tmp", prefix=".decisions_", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            for attempt in range(5):
                try:
                    os.replace(tmp_path, self.path)
                    break
                except OSError:
                    if attempt < 4:
                        if os.path.exists(self.path):
                            try:
                                os.chmod(self.path, stat.S_IWRITE | stat.S_IREAD)
                            except Exception:
                                pass
                        time.sleep(0.1 * (attempt + 1))
                    else:
                        if os.path.exists(self.path):
                            try:
                                os.chmod(self.path, stat.S_IWRITE | stat.S_IREAD)
                                os.remove(self.path)
                            except Exception:
                                pass
                        os.rename(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ---- loading / migration ----------------------------------

    @staticmethod
    def _load_and_migrate(path: str) -> Dict[str, Any]:
        """Read, parse, and migrate a manifest file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise DecisionsCorruptError(
                f"ships.decisions.json is not valid JSON ({path}): {e}"
            ) from e
        except OSError as e:
            raise DecisionsCorruptError(
                f"ships.decisions.json unreadable ({path}): {e}"
            ) from e

        version = data.get("schema_version")
        if not isinstance(version, int):
            raise DecisionsSchemaError(f"missing or invalid schema_version in {path}")
        if version > SCHEMA_VERSION:
            raise DecisionsSchemaError(
                f"ships.decisions.json at {path} has schema_version={version} "
                f"but this build only understands up to v{SCHEMA_VERSION}. "
                f"Upgrade the tool or use an older manifest."
            )

        # Apply migrations until we reach the current version.
        while version < SCHEMA_VERSION:
            migrator = MIGRATIONS.get(version)
            if migrator is None:
                raise DecisionsSchemaError(
                    f"no migration registered from v{version} → v{version + 1}"
                )
            data = migrator(data)
            version += 1
        data["schema_version"] = SCHEMA_VERSION

        # Defensive: ensure runs[] exists even on a hand-edited file.
        data.setdefault("runs", [])
        data.setdefault("project", {})
        return data

    # ---- run lifecycle ----------------------------------------

    @contextmanager
    def run(
        self,
        command: str,
        run_id: Optional[str] = None,
    ) -> Iterator["RunRecorder"]:
        """
        Open a new run as a context manager.

        On clean exit: finalises ``finished_at`` / ``duration_ms``,
        rolls up ``final_status`` from the stage statuses, and saves.
        On exception: marks the run ``interrupted=True``, sets
        ``final_status="failed"``, and saves before re-raising.

        Args:
            command:  The CLI command that triggered this run, recorded
                      verbatim so ``explain`` can show it.
            run_id:   Optional explicit run id. If omitted, generated
                      from the current UTC time plus a 4-char tag.

        Yields:
            ``RunRecorder`` — has a ``stage(name)`` context manager.
        """
        if run_id is None:
            run_id = _generate_run_id()

        started = _now_iso()
        run_entry: Dict[str, Any] = {
            "run_id": run_id,
            "command": command,
            "started_at": started,
            "finished_at": None,
            "duration_ms": 0,
            "final_status": "success",
            "stages": [],
        }
        with self._lock:
            self.data["runs"].append(run_entry)

        recorder = RunRecorder(self, run_entry)
        start_monotonic = time.monotonic()
        try:
            yield recorder
        except BaseException:
            run_entry["interrupted"] = True
            run_entry["final_status"] = "failed"
            run_entry["finished_at"] = _now_iso()
            run_entry["duration_ms"] = int((time.monotonic() - start_monotonic) * 1000)
            self.save()
            raise
        else:
            run_entry["finished_at"] = _now_iso()
            run_entry["duration_ms"] = int((time.monotonic() - start_monotonic) * 1000)
            run_entry["final_status"] = _rollup_final_status(run_entry["stages"])
            self.save()


# ---------------------------------------------------------------
# RunRecorder / StageRecorder
# ---------------------------------------------------------------


class RunRecorder:
    """
    Per-run recorder. Use ``stage(name)`` to open a stage entry.

    Created and finalised by ``DecisionsManifest.run()``; not meant
    to be constructed directly.
    """

    def __init__(
        self,
        manifest: DecisionsManifest,
        run_entry: Dict[str, Any],
    ):
        self._manifest = manifest
        self._run_entry = run_entry

    @property
    def run_id(self) -> str:
        return self._run_entry["run_id"]

    @contextmanager
    def stage(self, name: str) -> Iterator["StageRecorder"]:
        """
        Open a stage entry. Persists on context exit.

        Args:
            name: Stage name (typically one of the canonical seven).

        Yields:
            ``StageRecorder`` to record config_resolved / inputs /
            outputs / decisions / issues.
        """
        started = _now_iso()
        stage_entry: Dict[str, Any] = {
            "stage": name,
            "started_at": started,
            "finished_at": None,
            "duration_ms": 0,
            "status": "success",
            "config_resolved": {},
            "inputs": {},
            "outputs": {},
            "decisions": {},
            "issues": [],
        }
        with self._manifest._lock:
            self._run_entry["stages"].append(stage_entry)

        recorder = StageRecorder(stage_entry)
        start_monotonic = time.monotonic()
        try:
            yield recorder
        except BaseException:
            stage_entry["status"] = "error"
            stage_entry["finished_at"] = _now_iso()
            stage_entry["duration_ms"] = int(
                (time.monotonic() - start_monotonic) * 1000
            )
            self._manifest.save()
            raise
        else:
            stage_entry["finished_at"] = _now_iso()
            stage_entry["duration_ms"] = int(
                (time.monotonic() - start_monotonic) * 1000
            )
            # If status not explicitly set and any error issue was
            # added, upgrade to "error". A warning issue alone does
            # NOT auto-upgrade — callers who want that should call
            # set_status("warning") explicitly.
            if stage_entry["status"] == "success" and any(
                i.get("severity") == "error" for i in stage_entry["issues"]
            ):
                stage_entry["status"] = "error"
            self._manifest.save()


class StageRecorder:
    """
    Per-stage recorder. Mutates the underlying stage entry in place.

    Not thread-safe by itself — DecisionsManifest's lock protects
    the manifest as a whole, but a single stage entry is expected
    to be driven by one thread.
    """

    def __init__(self, stage_entry: Dict[str, Any]):
        self._entry = stage_entry

    # ---- status -----------------------------------------------

    @property
    def status(self) -> str:
        """Return the current recorded status for this stage.

        Readable after the stage context has closed — the auto-upgrade
        logic in ``RunRecorder.__exit__`` may have promoted the status
        to ``"error"`` if any error-severity issue was added. Use this
        in the ``process`` meta-verb to decide whether to abort.
        """
        return self._entry.get("status", "success")

    def set_status(self, status: str) -> None:
        """
        Set the stage status. One of:
        ``success`` / ``warning`` / ``error`` / ``skipped`` / ``no-op``.
        """
        if status not in STAGE_STATUSES:
            raise ValueError(
                f"unknown stage status {status!r}; valid: {list(STAGE_STATUSES)}"
            )
        self._entry["status"] = status

    # ---- universal sections -----------------------------------

    def set_config_resolved(
        self,
        name: str,
        value: Any,
        source: str,
        source_path: str,
    ) -> None:
        """
        Record one resolved configuration setting and its provenance.

        Args:
            name:        Setting name as recorded in ships.decisions.json
                         (typically the dotted path or its leaf).
            value:       Effective value used for this run.
            source:      The cascade layer that supplied it
                         (e.g. ``"layer-3"``).
            source_path: Best-effort filesystem origin
                         (e.g. ``"ships.yaml"``).
        """
        self._entry["config_resolved"][name] = {
            "value": value,
            "source": source,
            "source_path": source_path,
        }

    def set_inputs(self, **fields: Any) -> None:
        """Merge ``fields`` into the stage's ``inputs`` block."""
        self._entry["inputs"].update(fields)

    def set_outputs(self, **fields: Any) -> None:
        """Merge ``fields`` into the stage's ``outputs`` block."""
        self._entry["outputs"].update(fields)

    def set_decisions(self, **fields: Any) -> None:
        """Merge ``fields`` into the stage's ``decisions`` block."""
        self._entry["decisions"].update(fields)

    def add_issue(
        self,
        severity: str,
        code: str,
        message: str,
        location: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Append an issue to the stage's ``issues`` list.

        Args:
            severity: One of ``info`` / ``warning`` / ``error``.
            code:     Short stable identifier (e.g. ``GEN-COLLISION``).
            message:  Human-readable description.
            location: Optional file/line reference.
            details:  Optional machine-readable metadata for the finding
                      (e.g. custom-policy remediation: safe_fix_available,
                      automation_level, requires_human_review, …). Carried
                      verbatim into the JSON so agents and CI can act on it.
        """
        if severity not in ISSUE_SEVERITIES:
            raise ValueError(
                f"unknown severity {severity!r}; valid: {list(ISSUE_SEVERITIES)}"
            )
        issue: Dict[str, Any] = {
            "severity": severity,
            "code": code,
            "message": message,
        }
        if location is not None:
            issue["location"] = location
        if details:
            issue["details"] = details
        self._entry["issues"].append(issue)


# ---------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------


@dataclass
class PruneResult:
    """Summary of a prune operation (real or dry-run)."""

    total_runs: int
    kept_runs: int
    pruned_runs: int
    pruned_run_ids: List[str] = field(default_factory=list)
    pruned_started_at: List[str] = field(default_factory=list)
    dry_run: bool = False


def prune_decisions(
    path: str,
    keep_runs: Optional[int] = None,
    keep_days: Optional[int] = None,
    dry_run: bool = False,
) -> PruneResult:
    """
    Prune old run entries from a ``ships.decisions.json`` file.

    Exactly one of ``keep_runs`` or ``keep_days`` must be provided.
    The most recent runs (by ``started_at``) are kept; older ones are
    removed.  When ``dry_run=True`` the file is never written — the
    returned ``PruneResult`` describes what *would* be removed.

    Args:
        path:       Path to the ``ships.decisions.json`` file.
        keep_runs:  Retain the N most recent runs. Older runs are pruned.
        keep_days:  Retain runs started within the last N calendar days.
        dry_run:    If True, compute but do not apply the prune.

    Returns:
        ``PruneResult`` with counts and the run_ids that were (or would
        be) removed.

    Raises:
        ValueError:            Neither or both of keep_runs/keep_days given.
        DecisionsCorruptError: The file cannot be parsed.
        DecisionsSchemaError:  Unknown schema version.
        FileNotFoundError:     The file does not exist.
    """
    if (keep_runs is None) == (keep_days is None):
        raise ValueError("Provide exactly one of keep_runs or keep_days.")
    if keep_runs is not None and keep_runs < 0:
        raise ValueError("keep_runs must be >= 0.")
    if keep_days is not None and keep_days < 0:
        raise ValueError("keep_days must be >= 0.")

    if not os.path.exists(path):
        raise FileNotFoundError(f"ships.decisions.json not found: {path}")

    data = DecisionsManifest._load_and_migrate(path)
    runs: List[Dict[str, Any]] = data.get("runs", [])

    # Sort oldest-first so we can slice the tail to keep.
    def _started(run: Dict[str, Any]) -> str:
        return run.get("started_at") or ""

    sorted_runs = sorted(runs, key=_started)

    if keep_runs is not None:
        keep_count = min(keep_runs, len(sorted_runs))
        to_keep = sorted_runs[-keep_count:] if keep_count > 0 else []
        to_prune = sorted_runs[:-keep_count] if keep_count > 0 else sorted_runs
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        cutoff_str = cutoff.isoformat()
        to_keep = [r for r in sorted_runs if _started(r) >= cutoff_str]
        to_prune = [r for r in sorted_runs if _started(r) < cutoff_str]

    result = PruneResult(
        total_runs=len(runs),
        kept_runs=len(to_keep),
        pruned_runs=len(to_prune),
        pruned_run_ids=[r.get("run_id", "") for r in to_prune],
        pruned_started_at=[r.get("started_at", "") for r in to_prune],
        dry_run=dry_run,
    )

    if not dry_run and to_prune:
        data["runs"] = to_keep
        manifest = DecisionsManifest.__new__(DecisionsManifest)
        manifest.path = path
        manifest._lock = threading.Lock()
        manifest.data = data
        manifest.save()

    return result


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _generate_run_id() -> str:
    """Generate a unique run id: ``<UTC ISO>-<4-hex>``."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tag = uuid.uuid4().hex[:4]
    return f"{ts}-{tag}"


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _rollup_final_status(stages: List[Dict[str, Any]]) -> str:
    """
    Compute the run's ``final_status`` from per-stage statuses.

    Rules (highest precedence first):
        - any stage ``error`` → ``failed``
        - any stage ``warning`` → ``warning``
        - all stages ``success`` / ``no-op`` / ``skipped`` → ``success``
        - empty stage list → ``success`` (no work done is not a failure)

    The ``partial`` final status (errors occurred but pipeline kept
    going under ``on_error: continue``) is set explicitly by the
    orchestrator — this rollup conservatively reports ``failed`` if
    any error landed, on the principle that a stage error is a real
    outcome and the caller can override after the fact.
    """
    has_error = False
    has_warning = False
    for s in stages:
        status = s.get("status")
        if status == "error":
            has_error = True
        elif status == "warning":
            has_warning = True
    if has_error:
        return "failed"
    if has_warning:
        return "warning"
    return "success"

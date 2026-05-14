"""
ships_lineage.py — Optional OpenLineage event emission for SHIPS pipeline stages.

Emits OpenLineage ``RunEvent`` messages from the deploy pipeline so that
data catalogs (Marquez, DataHub, Apache Atlas, OpenMetadata) can ingest
deployment lineage natively.

Activation
----------
Set ``OPENLINEAGE_URL`` to the target transport endpoint:

    http://marquez:5000          → HTTP/JSON push to a catalog backend
    https://datahub:8080         → HTTPS push
    file:///var/log/ol.ndjson    → append-only NDJSON file (CI, air-gapped)

Optional environment variables:

    OPENLINEAGE_NAMESPACE    Override the dataset namespace (default:
                             ``teradata://<host>`` or ``teradata://unknown``)
    OPENLINEAGE_DISABLED     Set to ``true`` or ``1`` to disable entirely

No transport configured (``OPENLINEAGE_URL`` unset or empty) → silent
no-op.  Zero runtime cost, no imports beyond stdlib.

Transport
---------
HTTP/HTTPS:  Events are POSTed to ``<OPENLINEAGE_URL>/api/v1/lineage``
             with ``Content-Type: application/json``.  Connection timeout
             is 5 seconds; failures are logged at DEBUG and silently
             swallowed so a lineage backend outage never blocks a deploy.

File:        Events are appended as NDJSON lines to the path after
             the ``file://`` prefix.  Parent directories must already
             exist.

Events emitted
--------------
The ``deploy_package`` pipeline emits three event types per run:

    START     — at entry, before any DDL is executed
    COMPLETE  — when all objects deployed successfully
    FAIL      — when one or more objects failed

Each event carries:

    job       — ``ships.deploy`` in the package's job namespace
    run       — UUID run ID generated at START and reused for
                COMPLETE / FAIL
    inputs    — empty for Phase 1 (Phase 2 adds view dependency edges)
    outputs   — successfully deployed objects as ``(namespace, name)``
                Dataset pairs.  ``namespace`` is derived from
                ``OPENLINEAGE_NAMESPACE`` or the host argument;
                ``name`` is the fully qualified ``DATABASE.OBJECT_NAME``.

Run facet
---------
A custom ``ShipsRunFacet`` is attached to every event and carries:

    build_number    From ships.build.json (empty string if not present)
    environment     From ships.build.json
    package_name    From ships.build.json
    package_filename From ships.build.json
    dry_run         True when the deploy ran in dry-run mode

OpenLineage spec
----------------
Events conform to OpenLineage spec 2-0-2.  The NDJSON output is
compatible with:

    Marquez (reference implementation)  http://marquezproject.ai
    DataHub                             https://datahubproject.io
    Apache Atlas                        https://atlas.apache.org
    OpenMetadata                        https://open-metadata.org

See also
--------
``src/td_release_packager/graph_export.py`` — static OpenLineage export
from ``analyse_project`` (analysis-time lineage without a running catalog).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PRODUCER = "https://github.com/earthshiner/teradata-ships"

_OL_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"

_SHIPS_RUN_FACET_SCHEMA = (
    "https://github.com/earthshiner/teradata-ships"
    "/blob/main/docs/openlineage/ShipsRunFacet.json"
)

_JOB_TYPE_FACET_SCHEMA = (
    "https://openlineage.io/spec/facets/2-0-2/"
    "JobTypeJobFacet.json#/$defs/JobTypeJobFacet"
)

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _is_disabled() -> bool:
    return os.getenv("OPENLINEAGE_DISABLED", "").lower() in ("true", "1")


def _ol_url() -> str:
    return os.getenv("OPENLINEAGE_URL", "").strip()


def _active() -> bool:
    """True when lineage emission is configured and not disabled."""
    return bool(_ol_url()) and not _is_disabled()


def _namespace(db_host: str = "") -> str:
    """Return the dataset namespace, preferring the env-var override."""
    override = os.getenv("OPENLINEAGE_NAMESPACE", "").strip()
    if override:
        return override
    if db_host:
        return f"teradata://{db_host}"
    return "teradata://unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# ships.build.json reader
# ---------------------------------------------------------------------------


def _read_build_meta(package_dir: str) -> dict:
    """Read deployment metadata from ships.build.json in ``package_dir``.

    Returns a dict with keys ``build_number``, ``environment``,
    ``package_name``, ``package_filename``.  Missing or unreadable
    ships.build.json → all values are empty strings.
    """
    defaults = {
        "build_number": "",
        "environment": "",
        "package_name": "",
        "package_filename": "",
    }
    build_json = os.path.join(package_dir, "ships.build.json")
    if not os.path.isfile(build_json):
        return defaults
    try:
        with open(build_json, encoding="utf-8") as f:
            data = json.load(f)
        for key in defaults:
            if isinstance(data.get(key), str):
                defaults[key] = data[key]
    except Exception:  # noqa: BLE001
        logger.debug(
            "ships_lineage: could not read ships.build.json at %s", package_dir
        )
    return defaults


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _emit_event(event: dict) -> None:
    """Dispatch one RunEvent dict to the configured transport.

    HTTP/HTTPS:  POSTs to ``<OPENLINEAGE_URL>/api/v1/lineage``.
    File:        Appends NDJSON line to the path after ``file://``.

    All transport errors are swallowed and logged at DEBUG — a lineage
    backend outage must never block a deploy.
    """
    url = _ol_url()
    if not url or _is_disabled():
        return

    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))

    if url.startswith("file://"):
        _emit_to_file(url[7:], line)
    elif url.startswith("http://") or url.startswith("https://"):
        _emit_to_http(url, line)
    else:
        logger.debug("ships_lineage: unsupported OPENLINEAGE_URL scheme: %s", url)


def _emit_to_file(file_path: str, line: str) -> None:
    try:
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        logger.debug("ships_lineage: appended event to %s", file_path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ships_lineage: file write failed (%s): %s", file_path, exc)


def _emit_to_http(base_url: str, line: str) -> None:
    import urllib.request

    endpoint = base_url.rstrip("/") + "/api/v1/lineage"
    body = line.encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            logger.debug(
                "ships_lineage: emitted event to %s (status %s)",
                endpoint,
                resp.status,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("ships_lineage: HTTP emit failed (%s): %s", endpoint, exc)


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def _ships_run_facet(meta: dict, dry_run: bool) -> dict:
    """Build the custom ShipsRunFacet dict."""
    return {
        "_producer": _PRODUCER,
        "_schemaURL": _SHIPS_RUN_FACET_SCHEMA,
        "build_number": meta["build_number"],
        "environment": meta["environment"],
        "package_name": meta["package_name"],
        "package_filename": meta["package_filename"],
        "dry_run": dry_run,
    }


def _job_type_facet() -> dict:
    return {
        "_producer": _PRODUCER,
        "_schemaURL": _JOB_TYPE_FACET_SCHEMA,
        "processingType": "BATCH",
        "integration": "SHIPS",
        "jobType": "DDL_DEPLOYMENT",
    }


def _dataset(namespace: str, db_name: str, obj_name: str) -> dict:
    """Build an OpenLineage Dataset dict for a Teradata object.

    The dataset namespace is the server namespace (e.g.
    ``teradata://host:1025``); the name is the fully qualified
    object identifier (e.g. ``MYDB.ORDERS_T``).
    """
    fq_name = f"{db_name}.{obj_name}" if db_name else obj_name
    return {"namespace": namespace, "name": fq_name}


def _run_event(
    event_type: str,
    run_id: str,
    job_namespace: str,
    job_name: str,
    meta: dict,
    dry_run: bool,
    inputs: List[dict],
    outputs: List[dict],
    failed_objects: Optional[List[Tuple[str, str, str]]] = None,
) -> dict:
    """Assemble a complete OpenLineage RunEvent dict."""
    run_facets: dict = {
        "ships": _ships_run_facet(meta, dry_run),
    }
    if failed_objects:
        run_facets["shipsFailures"] = {
            "_producer": _PRODUCER,
            "_schemaURL": _SHIPS_RUN_FACET_SCHEMA,
            "failed_objects": [
                {"database": db, "object": obj, "error": err}
                for db, obj, err in failed_objects
            ],
        }

    return {
        "eventTime": _now(),
        "eventType": event_type,
        "producer": _PRODUCER,
        "schemaURL": _OL_SCHEMA_URL,
        "job": {
            "namespace": job_namespace,
            "name": job_name,
            "facets": {
                "jobType": _job_type_facet(),
            },
        },
        "run": {
            "runId": run_id,
            "facets": run_facets,
        },
        "inputs": inputs,
        "outputs": outputs,
    }


# ---------------------------------------------------------------------------
# Public API — called from deploy_package
# ---------------------------------------------------------------------------


def start_deploy_run(
    package_dir: str,
    dry_run: bool = False,
    db_host: str = "",
) -> str:
    """Emit a START RunEvent and return the run ID.

    Must be called before ``_deploy_package_impl``.  The returned
    run ID must be passed to ``complete_deploy_run`` or
    ``fail_deploy_run`` to close the run.

    Args:
        package_dir: Package directory containing ships.build.json.
        dry_run:     Whether this is a dry-run deployment.
        db_host:     Database host string used to derive the dataset
                     namespace.  Falls back to ``OPENLINEAGE_NAMESPACE``
                     env var or ``teradata://unknown`` if empty.

    Returns:
        A UUID string that identifies this run.
    """
    run_id = str(uuid.uuid4())
    if not _active():
        return run_id

    meta = _read_build_meta(package_dir)
    ns = _namespace(db_host)
    job_ns = meta["package_name"] or "ships"
    event = _run_event(
        event_type="START",
        run_id=run_id,
        job_namespace=job_ns,
        job_name="ships.deploy",
        meta=meta,
        dry_run=dry_run,
        inputs=[],
        outputs=[],
    )
    _emit_event(event)
    logger.debug("ships_lineage: START run_id=%s namespace=%s", run_id, ns)
    return run_id


def complete_deploy_run(
    run_id: str,
    package_dir: str,
    completed_objects: List[Tuple[str, str]],
    db_host: str = "",
) -> None:
    """Emit a COMPLETE RunEvent with output datasets.

    Args:
        run_id:             Run ID returned by ``start_deploy_run``.
        package_dir:        Package directory containing ships.build.json.
        completed_objects:  List of ``(database_name, object_name)``
                            tuples for objects that reached COMPLETED
                            state.
        db_host:            Database host string (namespace hint).
    """
    if not _active():
        return

    meta = _read_build_meta(package_dir)
    ns = _namespace(db_host)
    job_ns = meta["package_name"] or "ships"
    outputs = [_dataset(ns, db, obj) for db, obj in completed_objects]
    event = _run_event(
        event_type="COMPLETE",
        run_id=run_id,
        job_namespace=job_ns,
        job_name="ships.deploy",
        meta=meta,
        dry_run=False,
        inputs=[],
        outputs=outputs,
    )
    _emit_event(event)
    logger.debug("ships_lineage: COMPLETE run_id=%s outputs=%d", run_id, len(outputs))


def fail_deploy_run(
    run_id: str,
    package_dir: str,
    db_host: str = "",
    error: str = "",
    completed_objects: Optional[List[Tuple[str, str]]] = None,
    failed_objects: Optional[List[Tuple[str, str, str]]] = None,
) -> None:
    """Emit a FAIL RunEvent with partial output datasets.

    Args:
        run_id:             Run ID returned by ``start_deploy_run``.
        package_dir:        Package directory containing ships.build.json.
        db_host:            Database host string (namespace hint).
        error:              Top-level error message (used when the
                            deployer raised an unexpected exception).
        completed_objects:  ``(database_name, object_name)`` tuples
                            for objects that reached COMPLETED state
                            before the failure.
        failed_objects:     ``(database_name, object_name, error)``
                            tuples for objects that failed.
    """
    if not _active():
        return

    meta = _read_build_meta(package_dir)
    ns = _namespace(db_host)
    job_ns = meta["package_name"] or "ships"
    outputs = [_dataset(ns, db, obj) for db, obj in (completed_objects or [])]
    event = _run_event(
        event_type="FAIL",
        run_id=run_id,
        job_namespace=job_ns,
        job_name="ships.deploy",
        meta=meta,
        dry_run=False,
        inputs=[],
        outputs=outputs,
        failed_objects=failed_objects,
    )
    if error:
        event["run"]["facets"]["ships"]["error"] = error
    _emit_event(event)
    logger.debug(
        "ships_lineage: FAIL run_id=%s failed=%d",
        run_id,
        len(failed_objects or []),
    )

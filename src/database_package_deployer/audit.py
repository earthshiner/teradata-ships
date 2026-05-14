"""
audit.py — Immutable audit logging for SHIPS deployments (GAP-007).

Emits a structured JSON audit event at the conclusion of every Ship run
(both success and failure paths).  Sink failures do NOT cause Ship to exit
non-zero — the deployment outcome takes precedence over audit delivery.

Supported sink URI schemes:

    file:///path/to/audit.jsonl      Append JSON-Lines record to file.
    splunk://host:port?token=...&index=...  HTTP POST to Splunk HEC.
    syslog://host:port               RFC 5424 UDP syslog structured data.

Sink resolution order (highest priority first):

    1. sink_uri argument passed to emit_audit_event().
    2. ships.yaml key  audit_sink: <uri>.
    3. SHIPS_AUDIT_SINK environment variable.
    4. Not set → emit JSON to stderr (always done as fallback).

Audit event schema (minimum fields):

    event            "ships.deploy"
    timestamp        ISO 8601 UTC
    package_name     From ships.build.json
    package_hash     SHA-256 hex digest of the ZIP (if available)
    target_env       From ships.build.json
    change_ref       From ships.build.json (null if not set)
    operator         os.getlogin() or SHIPS_OPERATOR env var
    hostname         socket.gethostname()
    trust_label      From ships.build.json trust.label
    outcome          "SUCCESS" or "FAILURE"
    objects_deployed Count of COMPLETED objects
    objects_failed   Count of FAILED objects
    duration_seconds Wall-clock seconds for the Ship run
"""

import json
import logging
import os

from database_package_deployer.package_metadata import package_file
import socket
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_ENV_AUDIT_SINK = "SHIPS_AUDIT_SINK"
_ENV_OPERATOR = "SHIPS_OPERATOR"


def _resolve_operator() -> str:
    """Determine the current operator name.

    Prefers the ``SHIPS_OPERATOR`` environment variable; falls back to
    ``os.getlogin()``; uses the empty string if both fail.
    """
    op = os.environ.get(_ENV_OPERATOR, "").strip()
    if op:
        return op
    try:
        return os.getlogin()
    except Exception:
        try:
            import getpass

            return getpass.getuser()
        except Exception:
            return ""


def _read_build_json(package_dir: str) -> Dict[str, Any]:
    """Read ships.build.json from package_dir; return empty dict on failure."""
    path = package_file(package_dir, "ships.build.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _sha256_of_zip(package_dir: str, manifest: Dict[str, Any]) -> str:
    """Compute SHA-256 of the release ZIP (GAP-001 helper reuse)."""
    package_filename = manifest.get("package_filename", "")
    if not package_filename:
        return ""
    from pathlib import Path

    zip_path = Path(package_dir).parent / package_filename
    if not zip_path.exists():
        return ""
    try:
        from database_package_deployer.preflight import _sha256_of_file

        return _sha256_of_file(str(zip_path))
    except Exception:
        return ""


def build_audit_event(
    package_dir: str,
    outcome: str,
    objects_deployed: int,
    objects_failed: int,
    duration_seconds: float,
) -> Dict[str, Any]:
    """Construct the audit event dictionary.

    Args:
        package_dir:       Extracted package directory.
        outcome:           "SUCCESS" or "FAILURE".
        objects_deployed:  Count of COMPLETED objects.
        objects_failed:    Count of FAILED objects.
        duration_seconds:  Wall-clock seconds for the Ship run.

    Returns:
        Dict ready to serialise as JSON.
    """
    manifest = _read_build_json(package_dir)
    trust = manifest.get("trust", {})

    return {
        "event": "ships.deploy",
        "timestamp": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "package_name": manifest.get("package_name", ""),
        "package_hash": _sha256_of_zip(package_dir, manifest),
        "target_env": manifest.get("target_env") or manifest.get("environment", ""),
        "change_ref": manifest.get("change_ref"),
        "operator": _resolve_operator(),
        "hostname": socket.gethostname(),
        "trust_label": trust.get("label", "") if isinstance(trust, dict) else "",
        "outcome": outcome,
        "objects_deployed": objects_deployed,
        "objects_failed": objects_failed,
        "duration_seconds": round(duration_seconds, 1),
    }


def _sink_file(event: Dict[str, Any], uri: str) -> None:
    """Append *event* as a JSON-Lines record to the file described by *uri*.

    URI format: ``file:///path/to/audit.jsonl``
    """
    from urllib.request import url2pathname

    parsed = urlparse(uri)
    # url2pathname handles the Windows /C:/path → C:\path conversion.
    raw = parsed.netloc + parsed.path
    file_path = url2pathname(raw) if raw else ""
    if not file_path:
        raise ValueError(f"Invalid file sink URI: {uri}")
    with open(file_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    logger.debug("audit: event written to file sink '%s'.", file_path)


def _sink_splunk(event: Dict[str, Any], uri: str) -> None:
    """HTTP POST the event to a Splunk HEC endpoint.

    URI format: ``splunk://host:port?token=<hec_token>&index=<index>``

    TODO: Implement full Splunk HEC integration with retry and TLS.
    """
    parsed = urlparse(uri)
    params = parse_qs(parsed.query)
    token = (params.get("token", [""])[0]).strip()
    index = params.get("index", ["main"])[0]
    host = parsed.hostname or "localhost"
    port = parsed.port or 8088
    scheme = "https"

    hec_url = f"{scheme}://{host}:{port}/services/collector/event"
    payload = json.dumps({"event": event, "index": index}).encode("utf-8")
    req = Request(
        hec_url,
        data=payload,
        headers={
            "Authorization": f"Splunk {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:  # noqa: S310
        logger.debug("audit: Splunk HEC response %d.", resp.status)


def _sink_syslog(event: Dict[str, Any], uri: str) -> None:
    """Send the event to a syslog host via UDP.

    URI format: ``syslog://host:port``

    TODO: Implement full RFC 5424 structured-data syslog formatting.
    """
    import socket as _socket

    parsed = urlparse(uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 514
    msg = json.dumps(event, ensure_ascii=False)
    with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as sock:
        sock.sendto(msg.encode("utf-8"), (host, port))
    logger.debug("audit: event sent to syslog %s:%d.", host, port)


def emit_audit_event(
    package_dir: str,
    outcome: str,
    objects_deployed: int,
    objects_failed: int,
    duration_seconds: float,
    sink_uri: Optional[str] = None,
) -> None:
    """Build and emit an audit event to the configured sink.

    Sink resolution order: *sink_uri* argument > ships.yaml audit_sink >
    SHIPS_AUDIT_SINK env var > stderr.

    Sink failures are logged as warnings and do NOT propagate.

    Args:
        package_dir:       Extracted package directory.
        outcome:           "SUCCESS" or "FAILURE".
        objects_deployed:  Count of COMPLETED objects.
        objects_failed:    Count of FAILED objects.
        duration_seconds:  Wall-clock seconds.
        sink_uri:          Optional explicit sink URI.
    """
    event = build_audit_event(
        package_dir=package_dir,
        outcome=outcome,
        objects_deployed=objects_deployed,
        objects_failed=objects_failed,
        duration_seconds=duration_seconds,
    )

    # Resolve effective sink
    if not sink_uri:
        sink_uri = os.environ.get(_ENV_AUDIT_SINK, "").strip()

    if not sink_uri:
        # Try ships.yaml
        build_json = package_file(package_dir, "ships.build.json")
        if os.path.isfile(build_json):
            try:
                with open(build_json, encoding="utf-8") as fh:
                    manifest = json.load(fh)
                sink_uri = manifest.get("audit_sink", "").strip()
            except Exception:
                pass

    # Always emit to stderr as fallback (and as the primary record when no sink)
    event_json = json.dumps(event, ensure_ascii=False)
    print(event_json, file=sys.stderr)

    if not sink_uri:
        return

    try:
        scheme = urlparse(sink_uri).scheme.lower()
        if scheme == "file":
            _sink_file(event, sink_uri)
        elif scheme == "splunk":
            _sink_splunk(event, sink_uri)
        elif scheme == "syslog":
            _sink_syslog(event, sink_uri)
        else:
            logger.warning(
                "audit: unknown sink scheme '%s' — only stderr used.", scheme
            )
    except Exception as exc:
        logger.warning("audit: sink '%s' failed (non-fatal): %s", sink_uri, exc)

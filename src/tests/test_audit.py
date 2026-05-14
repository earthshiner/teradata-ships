"""
test_audit.py — Tests for the immutable audit log (GAP-007).

Covers:
    - File sink: audit event written as valid JSON-Lines to file after deploy.
    - No sink configured: event emitted to stderr as JSON.
    - Sink failure: unwriteable file path → Ship does not fail (warning logged).
    - Event schema: all minimum fields present in emitted event.
    - Splunk sink: HTTP POST made to correct URL with correct headers (mocked).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock


from database_package_deployer.audit import (
    build_audit_event,
    emit_audit_event,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _pkg_dir(tmp_path: Path) -> str:
    """Create a minimal package directory with ships.build.json."""
    pkg = tmp_path / "DEV_Pkg_BUILD_0001"
    pkg.mkdir()
    (pkg / "ships.build.json").write_text(
        json.dumps(
            {
                "package_filename": "DEV_Pkg_BUILD_0001.zip",
                "package_name": "Pkg",
                "environment": "DEV",
                "target_env": "DEV",
                "change_ref": "CHG0012345",
                "trust": {"label": "READY"},
            }
        ),
        encoding="utf-8",
    )
    return str(pkg)


# ---------------------------------------------------------------
# Event schema: all minimum fields present
# ---------------------------------------------------------------


def test_build_audit_event_minimum_fields(tmp_path):
    """build_audit_event returns a dict with all required minimum fields."""
    pkg = _pkg_dir(tmp_path)
    event = build_audit_event(
        package_dir=pkg,
        outcome="SUCCESS",
        objects_deployed=5,
        objects_failed=0,
        duration_seconds=12.3,
    )
    required = {
        "event",
        "timestamp",
        "package_name",
        "package_hash",
        "target_env",
        "change_ref",
        "operator",
        "hostname",
        "trust_label",
        "outcome",
        "objects_deployed",
        "objects_failed",
        "duration_seconds",
    }
    assert required.issubset(event.keys())
    assert event["event"] == "ships.deploy"
    assert event["outcome"] == "SUCCESS"
    assert event["objects_deployed"] == 5
    assert event["objects_failed"] == 0


# ---------------------------------------------------------------
# No sink → emit to stderr
# ---------------------------------------------------------------


def test_emit_audit_event_no_sink_emits_to_stderr(tmp_path, monkeypatch, capsys):
    """When no sink is configured, the event is printed to stderr as JSON."""
    monkeypatch.delenv("SHIPS_AUDIT_SINK", raising=False)
    pkg = _pkg_dir(tmp_path)

    emit_audit_event(
        package_dir=pkg,
        outcome="SUCCESS",
        objects_deployed=3,
        objects_failed=0,
        duration_seconds=5.0,
        sink_uri=None,
    )

    captured = capsys.readouterr()
    event = json.loads(captured.err.strip())
    assert event["event"] == "ships.deploy"
    assert event["outcome"] == "SUCCESS"


# ---------------------------------------------------------------
# File sink: event written as valid JSON-Lines
# ---------------------------------------------------------------


def test_emit_audit_event_file_sink(tmp_path, monkeypatch, capsys):
    """File sink writes a valid JSON-Lines record to the specified file."""
    monkeypatch.delenv("SHIPS_AUDIT_SINK", raising=False)
    pkg = _pkg_dir(tmp_path)
    audit_file = tmp_path / "audit.jsonl"

    emit_audit_event(
        package_dir=pkg,
        outcome="SUCCESS",
        objects_deployed=7,
        objects_failed=0,
        duration_seconds=8.5,
        sink_uri=audit_file.as_uri(),
    )

    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "ships.deploy"
    assert event["objects_deployed"] == 7


# ---------------------------------------------------------------
# File sink appends: multiple calls produce multiple lines
# ---------------------------------------------------------------


def test_file_sink_appends(tmp_path, monkeypatch, capsys):
    """Multiple emit calls append separate lines to the audit file."""
    monkeypatch.delenv("SHIPS_AUDIT_SINK", raising=False)
    pkg = _pkg_dir(tmp_path)
    audit_file = tmp_path / "audit.jsonl"
    sink = audit_file.as_uri()

    emit_audit_event(pkg, "SUCCESS", 5, 0, 3.0, sink_uri=sink)
    emit_audit_event(pkg, "FAILURE", 0, 2, 1.5, sink_uri=sink)

    lines = [
        line for line in audit_file.read_text(encoding="utf-8").splitlines() if line
    ]
    assert len(lines) == 2
    assert json.loads(lines[1])["outcome"] == "FAILURE"


# ---------------------------------------------------------------
# Sink failure: unwriteable path → Ship does not fail
# ---------------------------------------------------------------


def test_emit_audit_event_sink_failure_non_fatal(tmp_path, monkeypatch, capsys):
    """Unwriteable file sink path → warning logged, no exception raised."""
    monkeypatch.delenv("SHIPS_AUDIT_SINK", raising=False)
    pkg = _pkg_dir(tmp_path)

    # Pass an impossible path that will fail to open
    bad_sink = "file:///nonexistent/deeply/nested/path/audit.jsonl"

    # Must NOT raise — sink failure is non-fatal
    emit_audit_event(
        package_dir=pkg,
        outcome="SUCCESS",
        objects_deployed=1,
        objects_failed=0,
        duration_seconds=1.0,
        sink_uri=bad_sink,
    )
    # Event still goes to stderr
    captured = capsys.readouterr()
    assert "ships.deploy" in captured.err


# ---------------------------------------------------------------
# Splunk sink: HTTP POST with correct headers (mocked)
# ---------------------------------------------------------------


def test_emit_audit_event_splunk_sink(tmp_path, monkeypatch, capsys):
    """Splunk sink makes an HTTP POST with Authorization header."""
    monkeypatch.delenv("SHIPS_AUDIT_SINK", raising=False)
    pkg = _pkg_dir(tmp_path)

    posted_headers: dict = {}

    class _MockResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _mock_urlopen(req, timeout=None):
        posted_headers.update(dict(req.headers))
        return _MockResp()

    with mock.patch(
        "database_package_deployer.audit.urlopen", _mock_urlopen, create=True
    ):
        emit_audit_event(
            package_dir=pkg,
            outcome="SUCCESS",
            objects_deployed=2,
            objects_failed=0,
            duration_seconds=4.0,
            sink_uri="splunk://localhost:8088?token=test-token&index=ships",
        )

    # Verify Authorization header was sent
    assert any("splunk" in v.lower() or "Splunk" in v for v in posted_headers.values())

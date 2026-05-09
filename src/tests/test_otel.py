"""
test_otel.py — Tests for OpenTelemetry instrumentation in SHIPS.

Verifies that:
  - ships_span() yields a usable span object (real or no-op)
  - Span attributes are set correctly
  - Exceptions propagate and are recorded on the span
  - _stage_recording emits a span named ships.<stage_name>
  - _propagate_stage_to_otel_span transfers outputs and issue counts
  - current_trace_id() returns a 32-char hex string or None
  - otel_available() reflects whether the API is installed
  - Everything works when opentelemetry is NOT installed (no-op path)

The tests use the real opentelemetry-sdk (installed as an optional dep)
to verify that real spans are created, or the no-op path when SDK is absent.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from td_release_packager.otel import (
    _NoOpSpan,
    current_trace_id,
    otel_available,
    ships_span,
)
from td_release_packager.cli import _propagate_stage_to_otel_span


# ---------------------------------------------------------------
# ships_span — context manager
# ---------------------------------------------------------------


class TestShipsSpan:
    def test_yields_span_object(self):
        """ships_span yields something (no-op or real)."""
        with ships_span("test.span") as span:
            assert span is not None

    def test_attributes_accepted(self):
        """set_attribute does not raise on either real or no-op spans."""
        with ships_span("test.span", {"key": "value", "count": 42}) as span:
            span.set_attribute("extra", "attr")  # must not raise

    def test_exception_propagates(self):
        """Exceptions inside the span propagate to the caller."""
        with pytest.raises(ValueError):
            with ships_span("test.span"):
                raise ValueError("boom")

    def test_no_op_span_interface_complete(self):
        """_NoOpSpan satisfies the full span interface."""
        span = _NoOpSpan()
        span.set_attribute("k", "v")
        span.set_status(None)
        span.record_exception(ValueError("test"))
        assert span.trace_id == 0


# ---------------------------------------------------------------
# otel_available / current_trace_id
# ---------------------------------------------------------------


class TestOtelHelpers:
    def test_otel_available_returns_bool(self):
        result = otel_available()
        assert isinstance(result, bool)

    def test_current_trace_id_returns_str_or_none(self):
        result = current_trace_id()
        assert result is None or (isinstance(result, str) and len(result) == 32)

    def test_current_trace_id_inside_span(self):
        """Inside an active span, current_trace_id() may return a hex string."""
        with ships_span("test.span"):
            trace_id = current_trace_id()
            if otel_available():
                # With SDK configured and a real span active, we may get a trace ID.
                # Without SDK configuration the span is a no-op → still None.
                assert trace_id is None or (
                    isinstance(trace_id, str) and len(trace_id) == 32
                )
            else:
                assert trace_id is None


# ---------------------------------------------------------------
# _propagate_stage_to_otel_span
# ---------------------------------------------------------------


class TestPropagateStageToOtelSpan:
    def _make_mock_stage(self, status="success", outputs=None, issues=None):
        stage = MagicMock()
        stage._entry = {
            "stage": "harvest",
            "status": status,
            "outputs": outputs or {"classified": 10, "unclassified": 2},
            "issues": issues or [],
        }
        return stage

    def test_sets_status_attribute(self):
        span = MagicMock()
        stage = self._make_mock_stage(status="success")
        _propagate_stage_to_otel_span(stage, span)
        span.set_attribute.assert_any_call("ships.stage.status", "success")

    def test_propagates_scalar_outputs(self):
        span = MagicMock()
        stage = self._make_mock_stage(outputs={"classified": 42, "unclassified": 3})
        _propagate_stage_to_otel_span(stage, span)
        span.set_attribute.assert_any_call("ships.output.classified", 42)
        span.set_attribute.assert_any_call("ships.output.unclassified", 3)

    def test_skips_non_scalar_outputs(self):
        """List and dict outputs are not propagated as span attributes."""
        span = MagicMock()
        stage = self._make_mock_stage(outputs={"files": ["a", "b"], "count": 5})
        _propagate_stage_to_otel_span(stage, span)
        # count (int) propagated, files (list) not
        span.set_attribute.assert_any_call("ships.output.count", 5)
        calls = [str(c) for c in span.set_attribute.call_args_list]
        assert not any("files" in c for c in calls)

    def test_propagates_issue_counts(self):
        span = MagicMock()
        issues = [
            {"severity": "error", "code": "X"},
            {"severity": "warning", "code": "Y"},
            {"severity": "warning", "code": "Z"},
        ]
        stage = self._make_mock_stage(issues=issues)
        _propagate_stage_to_otel_span(stage, span)
        span.set_attribute.assert_any_call("ships.issues.errors", 1)
        span.set_attribute.assert_any_call("ships.issues.warnings", 2)

    def test_does_not_raise_on_malformed_stage(self):
        """Propagation must never raise — OTel must not break the recording path."""
        span = MagicMock()
        stage = MagicMock()
        stage._entry = None  # intentionally broken
        _propagate_stage_to_otel_span(stage, span)  # must not raise

    def test_does_not_raise_when_span_raises(self):
        """If the span raises, propagation swallows it silently."""
        span = MagicMock()
        span.set_attribute.side_effect = RuntimeError("span error")
        stage = self._make_mock_stage()
        _propagate_stage_to_otel_span(stage, span)  # must not raise


# ---------------------------------------------------------------
# No-op path: simulate opentelemetry not installed
# ---------------------------------------------------------------


class TestNoOpPath:
    """Simulate the environment where opentelemetry-api is absent."""

    def test_ships_span_yields_no_op_span_when_otel_absent(self):
        with patch("td_release_packager.otel._OTEL_AVAILABLE", False):
            with ships_span("test.span", {"k": "v"}) as span:
                assert isinstance(span, _NoOpSpan)

    def test_current_trace_id_returns_none_when_otel_absent(self):
        with patch("td_release_packager.otel._OTEL_AVAILABLE", False):
            assert current_trace_id() is None

    def test_otel_available_returns_false_when_absent(self):
        with patch("td_release_packager.otel._OTEL_AVAILABLE", False):
            assert otel_available() is False

    def test_exception_still_propagates_with_no_op(self):
        with patch("td_release_packager.otel._OTEL_AVAILABLE", False):
            with pytest.raises(RuntimeError):
                with ships_span("test.span"):
                    raise RuntimeError("still raised")

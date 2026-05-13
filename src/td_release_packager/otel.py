"""
otel.py — Optional OpenTelemetry instrumentation for SHIPS.

SHIPS follows the standard library instrumentation pattern:
  - This module uses opentelemetry-api (the interface) only.
  - SDK configuration (exporters, processors, sampling) is done
    externally by the operator via standard OTel env vars.
  - If opentelemetry-api is not installed, all instrumentation is
    a zero-overhead no-op — SHIPS works identically either way.

Standard env vars (https://opentelemetry.io/docs/languages/sdk-configuration/):
    OTEL_SERVICE_NAME              Service name (default: ships)
    OTEL_EXPORTER_OTLP_ENDPOINT   OTLP endpoint, e.g. http://localhost:4318
    OTEL_EXPORTER_OTLP_HEADERS    Auth headers (comma-separated key=value)
    OTEL_SDK_DISABLED              Set to "true" to disable all tracing
    OTEL_TRACES_SAMPLER            Sampling strategy (default: parentbased_always_on)

Install OTel support:
    uv sync --extra otel
    # or: pip install "teradata-ships[otel]"

Example: send traces to a local Jaeger instance
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
    export OTEL_SERVICE_NAME=ships-pipeline
    uv run python -m td_release_packager process ...
"""

from __future__ import annotations

import contextlib
from typing import Any, Dict, Iterator, Optional

# ---------------------------------------------------------------------------
# Try to import the OTel API — fall back to no-ops if not installed
# ---------------------------------------------------------------------------

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import SpanKind, StatusCode

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover — tested in isolation
    _OTEL_AVAILABLE = False

_TRACER_NAME = "ships"
_TRACER_VERSION = "0.4.0"


# ---------------------------------------------------------------------------
# No-op span (used when OTel is not installed)
# ---------------------------------------------------------------------------


class _NoOpSpan:
    """Lightweight no-op span returned when opentelemetry-api is absent."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any, description: str = "") -> None:
        pass

    def record_exception(self, exc: Exception) -> None:
        pass

    @property
    def trace_id(self) -> int:
        return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def ships_span(
    name: str,
    attributes: Optional[Dict[str, Any]] = None,
) -> Iterator[Any]:
    """
    Context manager that emits an OTel span named *name*.

    When ``opentelemetry-api`` is not installed or no SDK is configured
    (the default), this is a zero-overhead no-op.  A ``_NoOpSpan`` is
    yielded so call sites need no ``if otel_available`` guards.

    When an SDK and exporter are configured by the operator (via env
    vars), a real span is created and exported automatically.

    Exceptions propagate unchanged after being recorded on the span.

    Args:
        name:        Span name, e.g. ``"ships.harvest"``.
        attributes:  Initial span attributes set before yielding.

    Yields:
        A real ``opentelemetry.trace.Span`` or ``_NoOpSpan``.

    Example::

        from td_release_packager.otel import ships_span

        with ships_span("ships.harvest", {"source_dir": src}) as span:
            result = ingest_directory(src, project)
            span.set_attribute("classified", result.classified)
    """
    if not _OTEL_AVAILABLE:
        yield _NoOpSpan()
        return

    tracer = _otel_trace.get_tracer(_TRACER_NAME, _TRACER_VERSION)
    with tracer.start_as_current_span(name, kind=SpanKind.INTERNAL) as span:
        if attributes:
            for key, value in attributes.items():
                if value is not None:
                    span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            raise


def current_trace_id() -> Optional[str]:
    """
    Return the active OTel trace ID as a 32-character lowercase hex string.

    Returns ``None`` when OTel is not installed or no span is active.
    Use this to embed the trace ID in ``ships.decisions.json`` so the two
    systems can be correlated in a post-mortem.

    Example::

        trace_id = current_trace_id()
        # → "4bf92f3577b34da6a3ce929d0e0e4736" or None
    """
    if not _OTEL_AVAILABLE:
        return None
    ctx = _otel_trace.get_current_span().get_span_context()
    if ctx.is_valid:
        return format(ctx.trace_id, "032x")
    return None


def otel_available() -> bool:
    """Return True if the opentelemetry-api package is installed."""
    return _OTEL_AVAILABLE

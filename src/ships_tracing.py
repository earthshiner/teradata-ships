"""
ships_tracing.py — Optional OpenTelemetry instrumentation for SHIPS pipeline stages.

When ``opentelemetry-sdk`` and ``opentelemetry-exporter-otlp-proto-http`` are
installed *and* ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, this module bootstraps
a ``TracerProvider`` and exports spans to that endpoint.

When OTel is not installed, the endpoint is not configured, or
``OTEL_SDK_DISABLED=true``, every call is a no-op — pipeline behaviour is
completely unchanged.

Standard OTEL environment variables honoured:
    OTEL_SERVICE_NAME             Service name tag on all spans (default: ships)
    OTEL_EXPORTER_OTLP_ENDPOINT   OTLP/HTTP endpoint, e.g. http://localhost:4318
    OTEL_SDK_DISABLED             Set to "true" to disable without uninstalling
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Generator

# ---------------------------------------------------------------------------
# Optional OTel import
# ---------------------------------------------------------------------------

_OTEL_AVAILABLE = False

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    _OTEL_AVAILABLE = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

_provider: Any = None  # TracerProvider or None


def _is_disabled() -> bool:
    return os.getenv("OTEL_SDK_DISABLED", "").lower() in ("true", "1")


def _endpoint() -> str:
    return os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()


def _bootstrap() -> None:
    """Initialise the TracerProvider once.  Called lazily on first span request."""
    global _provider

    if _provider is not None:
        return

    if not _OTEL_AVAILABLE or _is_disabled():
        _provider = False  # sentinel: tried, disabled
        return

    endpoint = _endpoint()
    if not endpoint:
        _provider = False
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "ships")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _otel_trace.set_tracer_provider(provider)
    _provider = provider


def get_tracer(name: str = "ships") -> Any:
    """Return an OTel Tracer, or a no-op object if OTel is not active."""
    _bootstrap()
    if _provider:
        return _otel_trace.get_tracer(name)
    return _NoOpTracer()


# ---------------------------------------------------------------------------
# Public context manager
# ---------------------------------------------------------------------------


@contextmanager
def stage_span(
    span_name: str,
    tracer_name: str = "ships",
    **attributes: Any,
) -> Generator[Any, None, None]:
    """
    Context manager that wraps a pipeline stage in an OTel span.

    Usage::

        with stage_span("ships.ingest", source_dir=source_dir) as span:
            result = _do_ingest(...)
            span.set_attribute("ships.files_processed", result.total)

    When OTel is not active, ``span`` is a ``_NoOpSpan`` and every call on
    it is a no-op.  The wrapped code is always executed.

    Args:
        span_name:    The span name (e.g. ``ships.ingest``).
        tracer_name:  Tracer name (default: ``ships``).
        **attributes: Initial span attributes set before the body executes.
    """
    tracer = get_tracer(tracer_name)
    with tracer.start_as_current_span(span_name) as span:
        for key, value in attributes.items():
            span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            _record_exception(span, exc)
            raise


def _record_exception(span: Any, exc: Exception) -> None:
    """Set error status and record exception on span if OTel is active."""
    if not _OTEL_AVAILABLE or not _provider:
        return
    try:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.ERROR, str(exc)))
        span.record_exception(exc)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# No-op stubs (used when OTel is absent or disabled)
# ---------------------------------------------------------------------------


class _NoOpSpan:
    """A span that silently accepts all attribute/status calls."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_exception(self, exc: Exception, *args: Any, **kwargs: Any) -> None:
        pass

    def add_event(self, name: str, *args: Any, **kwargs: Any) -> None:
        pass


class _NoOpTracer:
    """A tracer that returns _NoOpSpan instances."""

    @contextmanager
    def start_as_current_span(
        self, name: str, **kwargs: Any
    ) -> Generator[_NoOpSpan, None, None]:
        yield _NoOpSpan()

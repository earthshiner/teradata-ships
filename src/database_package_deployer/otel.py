"""
otel.py — Optional OpenTelemetry instrumentation for database_package_deployer.

Same pattern as td_release_packager.otel — instrument the interface,
let the operator configure the SDK externally via standard OTel env vars.

All exports are no-ops when opentelemetry-api is not installed.
"""

from __future__ import annotations

import contextlib
from typing import Any, Dict, Iterator, Optional

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import SpanKind, StatusCode

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

_TRACER_NAME = "ships.deployer"
_TRACER_VERSION = "0.4.0"


class _NoOpSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any, description: str = "") -> None:
        pass

    def record_exception(self, exc: Exception) -> None:
        pass


@contextlib.contextmanager
def deployer_span(
    name: str,
    attributes: Optional[Dict[str, Any]] = None,
) -> Iterator[Any]:
    """
    Context manager emitting an OTel span for a deployer operation.

    Yields a real span when opentelemetry-api is installed and an SDK
    is configured, otherwise a zero-overhead no-op.
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

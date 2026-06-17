"""
reporting — Shared HTML report generation for SHIPS.

Houses the chrome shared by every SHIPS report (``common``) and the
pre-package pipeline report (``pipeline_report``, #324). The package and
deploy reports will migrate onto ``common`` in a later slice so the page
shell is defined exactly once.

``regenerate_reports`` is the single entry point the pipeline calls after
every step. It is deliberately fail-safe: reporting is a read-only
projection of ``ships.decisions.json`` and must never break the pipeline
that produced it.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["regenerate_reports", "generate_pipeline_report"]


def regenerate_reports(project_dir: str) -> Optional[str]:
    """Regenerate the pre-package pipeline report for a project.

    Called after each pipeline step (harvest, inspect, scan, analyse) so
    the report always reflects the latest ``ships.decisions.json``. Any
    failure is swallowed and logged at debug level — a reporting error
    must never abort or fail the step that triggered it.

    Args:
        project_dir: SHIPS project root.

    Returns:
        Absolute path to the written report, or ``None`` when nothing was
        written (no run recorded yet, or an error was suppressed).
    """
    try:
        from td_release_packager.reporting.pipeline_report import (
            generate_pipeline_report,
        )

        return generate_pipeline_report(project_dir)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("pipeline report regeneration failed: %s", exc)
        return None


def generate_pipeline_report(project_dir: str) -> Optional[str]:
    """Thin re-export of :func:`pipeline_report.generate_pipeline_report`."""
    from td_release_packager.reporting.pipeline_report import (
        generate_pipeline_report as _impl,
    )

    return _impl(project_dir)

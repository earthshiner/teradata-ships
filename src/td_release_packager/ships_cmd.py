"""ships_cmd.py — Detect the friendliest SHIPS invocation for the current shell.

The Navigator wizard and onboarding helpers print command snippets the
user is expected to copy.  Hard-coding ``python -m td_release_packager``
defeats the friction-reduction principle: users on Windows without an
active venv get ``The term 'ships' is not recognized``, but typing
``python -m td_release_packager …`` is ugly even when it works (#403).

``ships_cmd()`` picks, at runtime, the friendliest invocation that
actually resolves on *this* shell, in this order:

1. ``ships`` on PATH — works from any cwd, any shell, no extra tooling.
   This is the form #393 introduced via ``[project.scripts]``; it's the
   right answer when ``uv tool install --editable .`` (or a global pip
   install, or an activated venv) has put ``ships`` on PATH.
2. ``uv run ships`` — works if ``uv`` is on PATH AND we're inside a
   project whose ``pyproject.toml`` names ``teradata-ships`` /
   ``td_release_packager``.  ``uv run`` walks parents from cwd to find
   the right project, so any subdir of the project works; outside the
   project ``uv run ships`` would resolve to the wrong env (or fail),
   so we don't suggest it there.
3. ``python -m td_release_packager`` — universal fallback.  Requires the
   right Python on PATH and the module importable (active venv, or a
   global install).  Always valid; just ugliest.

``install_hint()`` returns a one-line suggestion the wizard can print
when the chosen verb isn't bare ``ships`` — so users know how to get
out of the fallback state.

The detection is cached for the process lifetime since the answer is a
function of the shell environment, which is stable within a run.
"""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path

# Names that, when present in a pyproject.toml, indicate ``uv run`` will
# resolve to the SHIPS environment.  ``teradata-ships`` is the
# distribution name (see pyproject.toml [project] name); the underscore
# / hyphen variants cover possible local renames or forks.
_PROJECT_MARKERS: tuple[str, ...] = (
    "teradata-ships",
    "teradata_ships",
    "td-release-packager",
    "td_release_packager",
)


def _in_ships_project(start: Path | None = None) -> bool:
    """True when *start* (or an ancestor) holds a pyproject.toml that
    names this project — i.e. ``uv run`` from here resolves the SHIPS
    environment.  Walks at most 12 levels to avoid pathological loops.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *list(here.parents)[:12]]:
        pyproject = candidate / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            text = pyproject.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            return False
        return any(marker in text for marker in _PROJECT_MARKERS)
    return False


@lru_cache(maxsize=1)
def ships_cmd() -> str:
    """Return the friendliest SHIPS invocation that resolves *now*.

    See the module docstring for the detection order.  Result is
    cached: the shell environment doesn't change mid-process.
    """
    if shutil.which("ships"):
        return "ships"
    if shutil.which("uv") and _in_ships_project():
        return "uv run ships"
    return "python -m td_release_packager"


def install_hint() -> str | None:
    """One-line install suggestion when the wizard isn't using bare
    ``ships``.  Returns ``None`` when ``ships`` is already on PATH so
    callers can skip printing.
    """
    if ships_cmd() == "ships":
        return None
    return (
        "Tip: for a friendlier prompt anywhere, install ships globally:\n"
        "       uv tool install --editable ."
    )


def reset_cache() -> None:
    """Drop the cached detection result.  Used by tests that patch
    ``shutil.which`` / ``Path.cwd`` to exercise the branches."""
    ships_cmd.cache_clear()

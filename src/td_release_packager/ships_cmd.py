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
    """Verb-aware cwd / install guidance for the wizard.

    Returns ``None`` when the detected verb is bare ``ships`` —
    cwd doesn't matter for a globally-installed console script and
    there's nothing to suggest.

    When the detected verb is ``uv run ships`` the hint tells the user
    where to run from (``uv run`` only resolves the SHIPS environment
    when cwd is inside the project).

    When the detected verb is the ``python -m`` fallback the hint
    suggests a global install so the user can leave the fallback
    state — and mentions the cwd assumption that makes ``python -m``
    work today (an active venv where the module is importable).
    """
    cmd = ships_cmd()
    if cmd == "ships":
        return None
    if cmd == "uv run ships":
        return (
            "Run from: inside the SHIPS project checkout (any subdir works) —\n"
            "          `uv run` resolves the env from `pyproject.toml` in a parent.\n"
            "Tip:      for a verb that works from anywhere, install ships globally:\n"
            "          uv tool install --editable ."
        )
    # python -m td_release_packager fallback
    return (
        "Run from: anywhere a Python with `td_release_packager` importable is\n"
        "          active (typically an activated venv).\n"
        "Tip:      for a friendlier prompt anywhere, install ships globally:\n"
        "          uv tool install --editable ."
    )


def run_from_hint() -> str:
    """One-line ``Run from:`` description for the detected verb.

    Used at the top of wizard banners as a quick orientation cue.
    Always returns a string — even for bare ``ships`` where cwd is
    irrelevant, the explicit "anywhere" callout is useful because
    several SHIPS commands write to cwd (``--output releases/``,
    relative ``--graph`` paths) and users want that confirmed.
    """
    cmd = ships_cmd()
    if cmd == "ships":
        return (
            "Run from: anywhere (cwd-independent invocation). "
            "Relative paths in args — e.g. `--output releases/` — "
            "are resolved against cwd."
        )
    if cmd == "uv run ships":
        return (
            "Run from: inside the SHIPS project checkout, any subdir. "
            "Relative paths in args are resolved against cwd."
        )
    return (
        "Run from: a shell where Python can import `td_release_packager` "
        "(activated venv). Relative paths in args are resolved against cwd."
    )


def reset_cache() -> None:
    """Drop the cached detection result.  Used by tests that patch
    ``shutil.which`` / ``Path.cwd`` to exercise the branches."""
    ships_cmd.cache_clear()

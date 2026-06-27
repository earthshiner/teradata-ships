"""
build_invocation.py — Capture a redacted snapshot of the command and
arguments that produced a package (issue #397).

``context/ships.build.json`` travels inside every package; the
project-side ``ships.decisions.json`` does not. So once a package is
handed off or extracted on another machine, "what command and args
built this?" can only be answered if the invocation was stamped into
the package itself. This module builds that snapshot.

The snapshot deliberately captures ONLY the invocation surface — the
command, its (redacted) args, the working directory, the resolved
env-config path, a timestamp, and the SHIPS / Python versions. It does
NOT capture the full process environment or shell state (too invasive,
a needless security surface), and it does NOT copy ``ships.decisions.json``
wholesale (it carries unrelated runs and grows unbounded).
"""

from __future__ import annotations

import platform
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from td_release_packager._version import __version__ as _SHIPS_VERSION

#: Placeholder substituted for any redacted argument value.
REDACTED = "***REDACTED***"

# Flags whose VALUE is a secret and must never be recorded. Matched
# case-insensitively against the flag name with leading dashes stripped,
# so ``--password``, ``--td-password`` and ``--signing-key`` all hit.
_SECRET_FLAG_TOKENS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "apikey",
    "api-key",
    "credential",
    "private-key",
    "signing-key",
)

# A flag is secret-bearing when its name contains a secret token but is
# not an obviously-safe path flag (e.g. --public-key is safe to record).
_SAFE_FLAG_EXCEPTIONS = ("public-key", "key-file", "key-id")


def _flag_name(arg: str) -> str:
    """Return the lower-cased flag name (no leading dashes, no ``=value``)."""
    return arg.lstrip("-").split("=", 1)[0].strip().lower()


def _is_secret_flag(arg: str) -> bool:
    """True when ``arg`` is a ``--flag`` whose value should be redacted."""
    if not arg.startswith("-"):
        return False
    name = _flag_name(arg)
    if any(safe in name for safe in _SAFE_FLAG_EXCEPTIONS):
        return False
    return any(tok in name for tok in _SECRET_FLAG_TOKENS)


def _is_secret_key(key: str) -> bool:
    """True when a bare ``KEY=VALUE`` arg's KEY names a secret."""
    name = key.strip().lower()
    if any(safe in name for safe in _SAFE_FLAG_EXCEPTIONS):
        return False
    return any(tok in name for tok in _SECRET_FLAG_TOKENS)


def redact_args(args: Sequence[str]) -> List[str]:
    """Return a copy of ``args`` with secret values masked.

    Handles three shapes:

    - ``--password secret``        → the following token is masked.
    - ``--password=secret``        → the inline value is masked.
    - ``TD_PASSWORD=secret``       → bare KEY=VALUE with a secret key.

    Flag NAMES are preserved (they are not secrets); only values are
    replaced with :data:`REDACTED`, so the snapshot still shows that a
    password was supplied without revealing it.
    """
    redacted: List[str] = []
    mask_next = False
    for arg in args:
        if mask_next:
            redacted.append(REDACTED)
            mask_next = False
            continue

        if _is_secret_flag(arg):
            if "=" in arg:
                # --flag=value → keep the flag, mask the value.
                flag = arg.split("=", 1)[0]
                redacted.append(f"{flag}={REDACTED}")
            else:
                # --flag value → emit flag now, mask the next token.
                redacted.append(arg)
                mask_next = True
            continue

        # Bare KEY=VALUE (no leading dash) with a secret-looking key.
        if not arg.startswith("-") and "=" in arg:
            key, _ = arg.split("=", 1)
            if _is_secret_key(key):
                redacted.append(f"{key}={REDACTED}")
                continue

        redacted.append(arg)

    return redacted


def snapshot(
    command: str,
    args: Sequence[str],
    cwd: str,
    env_config: Optional[str] = None,
    *,
    timestamp: Optional[str] = None,
) -> Dict[str, object]:
    """Build the ``build_invocation`` block for ``ships.build.json``.

    Args:
        command:    The high-level command recorded, e.g. ``"ships package"``
                    or ``"ships process"``.
        args:       The raw argument list (everything after the command).
                    Secret values are redacted via :func:`redact_args`.
        cwd:        The working directory the command was run from.
        env_config: Resolved path to the env-config file, if any.
        timestamp:  ISO-8601 timestamp; defaults to now (UTC). Injectable
                    so callers/tests can pin it.

    Returns:
        A JSON-serialisable dict with ``command``, redacted ``args``,
        ``cwd``, ``env_config``, ``timestamp``, ``ships_version`` and
        ``python_version``.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()

    return {
        "command": command,
        "args": redact_args(list(args)),
        "cwd": cwd,
        "env_config": env_config,
        "timestamp": timestamp,
        "ships_version": _SHIPS_VERSION,
        "python_version": platform.python_version(),
    }


def snapshot_from_argv(
    command: str,
    cwd: str,
    env_config: Optional[str] = None,
) -> Dict[str, object]:
    """Convenience wrapper that snapshots the live ``sys.argv[1:]``."""
    return snapshot(command, sys.argv[1:], cwd, env_config)

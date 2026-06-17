"""
redaction.py — Secret-safe display helpers for SHIPS reports.

The tokenisation preview (#325) renders resolved token values into HTML that
is bundled inside the package (``context/reports/``).  ``read_env_config``
resolves ``$env:`` / ``vault:`` references to their **real** secret values, so
the report must never display them.  These helpers classify a token from its
*raw* config value (before resolution) plus its name, and return a
display-safe string — used both for the value column and for the
before/after rendering so a password can never leak into the report.
"""

from __future__ import annotations

import re
from typing import Optional

#: Raw value prefixes that denote an external secret reference resolved at
#: load time by the token engine.
SECRET_REF_PREFIXES = ("$env:", "vault:")

#: Token names that conventionally carry secrets — masked even when the env
#: config holds a plain literal value (e.g. a password typed directly).
_SENSITIVE_NAME_RE = re.compile(
    r"(password|passwd|pwd|secret|credential|key)", re.IGNORECASE
)

#: Display placeholders (never reveal a value).
SECRET_REF_DISPLAY = "«secret ref»"
MASKED_DISPLAY = "«masked»"
EMPTY_DISPLAY = "«empty»"


def is_secret_ref(raw_value: object) -> bool:
    """True when a raw config value is an external secret reference."""
    return str(raw_value or "").strip().startswith(SECRET_REF_PREFIXES)


def is_sensitive_name(name: object) -> bool:
    """True when a token name conventionally holds a secret."""
    return bool(_SENSITIVE_NAME_RE.search(str(name or "")))


def classify(name: object, raw_value: object) -> str:
    """Classify a token as ``secret-ref`` / ``sensitive`` / ``plain``.

    ``secret-ref`` and ``sensitive`` values must never be displayed; only
    ``plain`` values may be shown in full.
    """
    if is_secret_ref(raw_value):
        return "secret-ref"
    if is_sensitive_name(name):
        return "sensitive"
    return "plain"


def is_redacted(name: object, raw_value: object) -> bool:
    """True when the token's resolved value must not be displayed."""
    return classify(name, raw_value) != "plain"


def masked_display(
    name: object,
    raw_value: object,
    resolved_value: Optional[str],
) -> str:
    """Return a display-safe string for a token's resolved value.

    Args:
        name:           Token name (drives sensitive-name masking).
        raw_value:      The raw env-config value before resolution (drives
                        secret-reference detection).
        resolved_value: The resolved value (only shown for ``plain`` tokens).

    Returns:
        The resolved value for plain tokens, or a placeholder that reveals
        nothing for secret-reference / sensitive / empty values.
    """
    kind = classify(name, raw_value)
    if kind == "secret-ref":
        return SECRET_REF_DISPLAY
    if kind == "sensitive":
        return MASKED_DISPLAY
    if resolved_value is None or resolved_value == "":
        return EMPTY_DISPLAY
    return str(resolved_value)

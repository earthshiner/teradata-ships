"""
mpa.py — Multi-Person Authorisation (4-eyes) for SHIPS deployments (GAP-006).

Provides the approval code generation and verification logic.

Approval code:

    HMAC-SHA256(
        key  = SHIPS_SIGNING_KEY (same shared key as package signing),
        msg  = sha256_of_zip + ":" + UTC_date_YYYY-MM-DD
    )

The code is valid for the calendar day on which it was generated (UTC).
This gives a natural 24-hour expiry without requiring a timestamp inside
the code itself — the receiver recomputes the code for today's UTC date
and compares with ``hmac.compare_digest()``.

Usage:
    # Second operator generates an approval code:
    ships approve /releases/PRD_MyPkg_BUILD_0001.zip
    # → prints a 64-char hex string to stdout

    # Deploying operator passes it to ship:
    ships deploy /extracted/PRD_MyPkg_BUILD_0001/ --approval-code <code>
"""

import hashlib
import hmac as _hmac
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from database_package_deployer.signing import compute_hmac, resolve_signing_key, verify_hmac
from database_package_deployer.preflight import _sha256_of_file

logger = logging.getLogger(__name__)


def _today_utc() -> str:
    """Return today's UTC date as 'YYYY-MM-DD'."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _approval_message(zip_hash: str, date_str: str) -> str:
    """Build the HMAC message: sha256_of_zip + ':' + date."""
    return f"{zip_hash}:{date_str}"


def generate_approval_code(zip_path: str) -> Optional[str]:
    """Generate a time-limited approval code for a package ZIP.

    The code is ``HMAC-SHA256(key, sha256_of_zip + ':' + UTC_date_YYYY-MM-DD)``.
    It is valid only for the calendar day it was generated (UTC).

    Args:
        zip_path: Path to the release ZIP archive.

    Returns:
        64-char hex approval code, or ``None`` if no signing key is available.
    """
    key = resolve_signing_key()
    if key is None:
        logger.error("mpa: SHIPS_SIGNING_KEY not set — cannot generate approval code.")
        return None

    zip_hash = _sha256_of_file(zip_path)
    msg = _approval_message(zip_hash, _today_utc())
    code = compute_hmac(key, msg)
    logger.info("mpa: approval code generated for '%s'.", Path(zip_path).name)
    return code


def verify_approval_code(zip_path: str, provided_code: str) -> bool:
    """Verify that *provided_code* is a valid approval code for *zip_path*.

    Checks the code against today's UTC date only. Yesterday's code is
    explicitly rejected.

    Args:
        zip_path:      Path to the release ZIP archive.
        provided_code: Hex HMAC string provided by the approving operator.

    Returns:
        True if the code is valid for today.
    """
    key = resolve_signing_key()
    if key is None:
        return False

    zip_hash = _sha256_of_file(zip_path)
    msg = _approval_message(zip_hash, _today_utc())
    return verify_hmac(key, msg, provided_code)

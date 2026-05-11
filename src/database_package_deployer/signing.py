"""
signing.py — HMAC-SHA256 package signing and verification (GAP-005).

Provides a thin, stdlib-only signing layer for SHIPS release packages.
Uses HMAC-SHA256 with a shared secret key so CI/CD pipelines can sign
packages with a key that is not available to individual developers.

Key resolution order (both signing and verification):

    1. Explicit key bytes / path passed by the caller.
    2. SHIPS_SIGNING_KEY environment variable (raw key string).
    3. Not set → signing/verification skipped (or ERROR if required).

The HMAC is computed over the SHA-256 hex digest of the ZIP archive
(not the archive bytes themselves) so the sidecar `.sha256` and the
HMAC `.hmac` are complementary: the `.sha256` covers transit integrity
of the ZIP container; the `.hmac` covers authenticity (who signed it).

Timing-safe comparison uses ``hmac.compare_digest()`` — never ``==``.
"""

import hmac as _hmac
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ENV_KEY = "SHIPS_SIGNING_KEY"


def resolve_signing_key(key_path: Optional[str] = None) -> Optional[bytes]:
    """Resolve the signing key from the given path or environment variable.

    Resolution order:
        1. *key_path* — path to a file whose contents are the raw key.
        2. ``SHIPS_SIGNING_KEY`` environment variable.
        3. Neither set → returns ``None``.

    Args:
        key_path: Optional filesystem path to a file containing the key.

    Returns:
        Key bytes, or ``None`` if no key is available.
    """
    if key_path:
        try:
            raw = Path(key_path).read_bytes().rstrip(b"\n\r")
            if raw:
                return raw
        except (OSError, IOError) as exc:
            logger.warning("signing: could not read key file '%s': %s", key_path, exc)

    env_val = os.environ.get(_ENV_KEY, "").strip()
    if env_val:
        return env_val.encode("utf-8")

    return None


def compute_hmac(key: bytes, sha256_hex: str) -> str:
    """Compute HMAC-SHA256 of *sha256_hex* using *key*.

    The message is the SHA-256 hex digest of the archive (not the raw
    archive bytes), so the computation is cheap and the relationship
    between the ``.sha256`` sidecar and the ``.hmac`` sidecar is explicit.

    Args:
        key:        Raw signing key bytes.
        sha256_hex: SHA-256 hex digest of the ZIP archive.

    Returns:
        Lowercase hex HMAC digest string.
    """
    mac = _hmac.new(key, sha256_hex.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


def verify_hmac(key: bytes, sha256_hex: str, expected_hex: str) -> bool:
    """Verify that *expected_hex* matches the HMAC of *sha256_hex* under *key*.

    Uses ``hmac.compare_digest()`` for timing-safe comparison.

    Args:
        key:          Raw signing key bytes.
        sha256_hex:   SHA-256 hex digest of the ZIP archive.
        expected_hex: HMAC hex digest to compare against.

    Returns:
        True if the HMAC matches; False otherwise.
    """
    actual = compute_hmac(key, sha256_hex)
    return _hmac.compare_digest(actual, expected_hex.lower())


def sign_package(zip_path: str, key_path: Optional[str] = None) -> Optional[str]:
    """Sign a release ZIP archive and write a ``.hmac`` sidecar.

    Resolves the signing key, computes HMAC-SHA256 of the archive's
    SHA-256 digest, and writes the result to ``<zip_path>.hmac``.

    Args:
        zip_path:  Path to the ZIP archive to sign.
        key_path:  Optional path to a file containing the signing key.
                   Falls back to ``SHIPS_SIGNING_KEY`` env var.

    Returns:
        Path to the written ``.hmac`` sidecar, or ``None`` when no key
        is available (signing is silently skipped).
    """
    key = resolve_signing_key(key_path)
    if key is None:
        logger.debug("signing: no key available — skipping package signing.")
        return None

    from database_package_deployer.preflight import _sha256_of_file

    zip_hash = _sha256_of_file(zip_path)
    hmac_hex = compute_hmac(key, zip_hash)

    hmac_path = zip_path + ".hmac"
    Path(hmac_path).write_text(hmac_hex + "\n", encoding="utf-8")
    logger.info("signing: signed '%s' → %s", os.path.basename(zip_path), hmac_path)
    return hmac_path

"""
asym_signing.py — Ed25519 asymmetric package signing and verification (Option C).

Provides an asymmetric signing layer using Ed25519 (via the ``cryptography``
package). Only the CI pipeline holds the private key; DBAs verify with the
public key which is committed to the project repository.

This is stronger than HMAC-SHA256 (GAP-005): with HMAC a compromised shared
key allows forgery; with Ed25519 the private key never leaves the CI pipeline.

Key management (minimal — no PKI or HSM required):

    1. Generate once:  ships keygen → ships_signing_private.pem + ships_signing_public.pem
    2. Private key:    stored in CI/CD secret (SHIPS_PRIVATE_KEY_PATH env var)
                       never on developer workstations, never in source control
    3. Public key:     commit ships_signing_public.pem to the project repo —
                       it is a public key and safe to share
    4. Key rotation:   re-run ships keygen, rotate the CI secret, update the
                       committed public key, rebuild packages

Sidecar format (.sig file):
    Base64-encoded Ed25519 signature of the SHA-256 hex digest of the ZIP.
    The message signed is the same string used by HMAC (the hex digest), so
    .sha256, .hmac, and .sig are complementary.

Requirements:
    pip install cryptography>=42.0  (or: pip install ships[signing])
    The package gracefully degrades when cryptography is not installed.
"""

import base64
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ENV_PRIVATE_KEY_PATH = "SHIPS_PRIVATE_KEY_PATH"
_ENV_ASYMMETRIC_KEY = "SHIPS_ASYMMETRIC_KEY"
_ENV_PUBLIC_KEY_PATH = "SHIPS_PUBLIC_KEY_PATH"
_ENV_PUBLIC_KEY = "SHIPS_PUBLIC_KEY"

_IMPORT_ERROR_MSG = (
    "The 'cryptography' package is required for Ed25519 signing.\n"
    "Install it with:  pip install cryptography>=42.0\n"
    "Or, if using uv:  uv pip install 'teradata-ships[signing]'"
)


def _require_cryptography():
    """Import and return the cryptography Ed25519 primitives, or raise ImportError."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
            load_pem_private_key,
            load_pem_public_key,
        )

        return (
            Ed25519PrivateKey,
            Ed25519PublicKey,
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
            load_pem_private_key,
            load_pem_public_key,
        )
    except ImportError:
        raise ImportError(_IMPORT_ERROR_MSG)


def generate_keypair() -> tuple:
    """Generate a new Ed25519 keypair.

    Returns:
        Tuple of (private_pem, public_pem) — both as PEM-encoded strings.

    Raises:
        ImportError: When the ``cryptography`` package is not installed.
    """
    (
        Ed25519PrivateKey,
        _Ed25519PublicKey,
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
        _load_pem_private_key,
        _load_pem_public_key,
    ) = _require_cryptography()

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode("utf-8")

    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=Encoding.PEM,
            format=PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )

    return (private_pem, public_pem)


def sign_zip(zip_path: str, private_key_pem: str) -> str:
    """Sign a ZIP archive with an Ed25519 private key and write a ``.sig`` sidecar.

    The message signed is the SHA-256 hex digest of the ZIP (matching the
    convention used by HMAC signing in ``signing.py``).  The signature bytes
    are base64-encoded (standard encoding, with padding) and written as a
    single line followed by a newline.

    Args:
        zip_path:        Path to the ZIP archive to sign.
        private_key_pem: PEM-encoded Ed25519 private key string.

    Returns:
        Path to the written ``.sig`` sidecar file.

    Raises:
        ImportError: When the ``cryptography`` package is not installed.
    """
    import hashlib

    (
        _Ed25519PrivateKey,
        _Ed25519PublicKey,
        _Encoding,
        _NoEncryption,
        _PrivateFormat,
        _PublicFormat,
        load_pem_private_key,
        _load_pem_public_key,
    ) = _require_cryptography()

    # Compute SHA-256 hex digest of the ZIP (same message as HMAC signing)
    with open(zip_path, "rb") as fh:
        zip_sha256 = hashlib.sha256(fh.read()).hexdigest()

    private_key = load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    signature_bytes = private_key.sign(zip_sha256.encode("utf-8"))
    sig_b64 = base64.b64encode(signature_bytes).decode("ascii")

    sig_path = zip_path + ".sig"
    Path(sig_path).write_text(sig_b64 + "\n", encoding="utf-8")
    logger.info("asym_signing: signed '%s' → %s", os.path.basename(zip_path), sig_path)
    return sig_path


def verify_zip(zip_path: str, public_key_pem: str, sig_path: str = None) -> bool:
    """Verify an Ed25519 ``.sig`` sidecar against a ZIP archive.

    Args:
        zip_path:       Path to the ZIP archive.
        public_key_pem: PEM-encoded Ed25519 public key string.
        sig_path:       Optional explicit path to the ``.sig`` file.
                        Defaults to ``zip_path + ".sig"``.

    Returns:
        True if the signature is valid; False on any verification failure
        (invalid signature, missing file, malformed data, missing library).
        Never raises.
    """
    import hashlib

    try:
        (
            _Ed25519PrivateKey,
            _Ed25519PublicKey,
            _Encoding,
            _NoEncryption,
            _PrivateFormat,
            _PublicFormat,
            _load_pem_private_key,
            load_pem_public_key,
        ) = _require_cryptography()
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        logger.error("asym_signing: cryptography not installed — cannot verify.")
        return False

    try:
        _sig_path = sig_path or (zip_path + ".sig")
        sig_b64 = Path(_sig_path).read_text(encoding="utf-8").strip()
        signature_bytes = base64.b64decode(sig_b64)

        with open(zip_path, "rb") as fh:
            zip_sha256 = hashlib.sha256(fh.read()).hexdigest()

        public_key = load_pem_public_key(public_key_pem.encode("utf-8"))
        public_key.verify(signature_bytes, zip_sha256.encode("utf-8"))
        logger.info(
            "asym_signing: signature valid for '%s'.", os.path.basename(zip_path)
        )
        return True

    except InvalidSignature:
        logger.warning(
            "asym_signing: signature INVALID for '%s'.", os.path.basename(zip_path)
        )
        return False
    except (OSError, ValueError, Exception) as exc:
        logger.warning(
            "asym_signing: verification error for '%s': %s",
            os.path.basename(zip_path),
            exc,
        )
        return False


def resolve_private_key_pem(key_path: str = None) -> Optional[str]:
    """Resolve the Ed25519 private key PEM from multiple sources.

    Resolution order:
        1. *key_path* argument — path to a PEM file.
        2. ``SHIPS_PRIVATE_KEY_PATH`` environment variable — path to a PEM file.
        3. ``SHIPS_ASYMMETRIC_KEY`` environment variable — raw PEM string.
        4. None — no private key available.

    Args:
        key_path: Optional filesystem path to a PEM file.

    Returns:
        PEM string, or None if no key is available.
    """
    if key_path:
        try:
            content = Path(key_path).read_text(encoding="utf-8").strip()
            if content:
                return content
        except (OSError, IOError) as exc:
            logger.warning(
                "asym_signing: could not read private key file '%s': %s", key_path, exc
            )

    env_path = os.environ.get(_ENV_PRIVATE_KEY_PATH, "").strip()
    if env_path:
        try:
            content = Path(env_path).read_text(encoding="utf-8").strip()
            if content:
                return content
        except (OSError, IOError) as exc:
            logger.warning(
                "asym_signing: could not read SHIPS_PRIVATE_KEY_PATH '%s': %s",
                env_path,
                exc,
            )

    raw_pem = os.environ.get(_ENV_ASYMMETRIC_KEY, "").strip()
    if raw_pem:
        return raw_pem

    return None


def resolve_public_key_pem(key_path: str = None) -> Optional[str]:
    """Resolve the Ed25519 public key PEM from multiple sources.

    Resolution order:
        1. *key_path* argument — path to a PEM file.
        2. ``SHIPS_PUBLIC_KEY_PATH`` environment variable — path to a PEM file.
        3. ``SHIPS_PUBLIC_KEY`` environment variable — raw PEM string.
        4. None — no public key available.

    Args:
        key_path: Optional filesystem path to a PEM file.

    Returns:
        PEM string, or None if no key is available.
    """
    if key_path:
        try:
            content = Path(key_path).read_text(encoding="utf-8").strip()
            if content:
                return content
        except (OSError, IOError) as exc:
            logger.warning(
                "asym_signing: could not read public key file '%s': %s", key_path, exc
            )

    env_path = os.environ.get(_ENV_PUBLIC_KEY_PATH, "").strip()
    if env_path:
        try:
            content = Path(env_path).read_text(encoding="utf-8").strip()
            if content:
                return content
        except (OSError, IOError) as exc:
            logger.warning(
                "asym_signing: could not read SHIPS_PUBLIC_KEY_PATH '%s': %s",
                env_path,
                exc,
            )

    raw_pem = os.environ.get(_ENV_PUBLIC_KEY, "").strip()
    if raw_pem:
        return raw_pem

    return None

"""
ships_mcp_auth.py — JWT/Bearer token verification for the SHIPS MCP server.

Implements the FastMCP ``TokenVerifier`` protocol using JWKS-backed JWT
validation. Designed for enterprise deployments where the MCP server runs
as a standalone HTTP service behind an OAuth 2.0 / OIDC identity provider
(Azure AD, Okta, AWS Cognito, Keycloak, etc.).

Architecture
------------
The verifier acts as an OAuth 2.0 Resource Server (RS):

  Client → [Bearer JWT] → SHIPS MCP (RS) → [JWKS] → Identity Provider (AS)

SHIPS does NOT issue tokens. It validates tokens issued by an external
authorisation server using asymmetric key verification via JWKS.

Usage
-----
This module is not intended to be imported directly. ``ships_mcp.main()``
constructs a ``JWTTokenVerifier`` from CLI flags and assigns it to
``mcp._token_verifier`` before calling ``mcp.run()``.

Dependencies
------------
  PyJWT[crypto] >= 2.8.0   — JWT decode and signature verification
  httpx >= 0.27.0           — Async JWKS endpoint fetching

Both are declared in requirements.txt and installed by ``uv sync``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy imports — only needed when HTTP transport with auth is enabled
# ---------------------------------------------------------------------------

# These are imported inside methods rather than at module level so that
# the base SHIPS pipeline (td_release_packager) continues to work in
# environments where PyJWT and httpx are not installed.


def _jwt():
    """Lazily import PyJWT."""
    try:
        import jwt as _jwt_mod
        return _jwt_mod
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "PyJWT is required for JWT authentication support. "
            "Install it with: pip install 'PyJWT[crypto]'"
        ) from exc


def _httpx():
    """Lazily import httpx."""
    try:
        import httpx as _httpx_mod
        return _httpx_mod
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "httpx is required for JWKS fetching. "
            "Install it with: pip install httpx"
        ) from exc


# ---------------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------------


class JWKSCache:
    """Fetch and cache a JWKS endpoint, keyed by ``kid``.

    On a cache miss (unknown ``kid``), the cache is refreshed once before
    giving up. This handles key rotation by the identity provider without
    requiring a server restart.

    Args:
        jwks_uri:       Full URL of the JWKS endpoint, e.g.
                        ``https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys``
        ttl_seconds:    How long to cache the key set (default: 3600 = 1 hour).
        request_timeout: HTTP timeout for JWKS fetches in seconds (default: 10).
    """

    def __init__(
        self,
        jwks_uri: str,
        ttl_seconds: int = 3600,
        request_timeout: float = 10.0,
    ) -> None:
        self._jwks_uri = jwks_uri
        self._ttl_seconds = ttl_seconds
        self._request_timeout = request_timeout
        # kid → PyJWT-compatible key object
        self._keys: dict[str, object] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    def _is_stale(self) -> bool:
        """Return True if the cache is empty or past its TTL."""
        return not self._keys or (time.monotonic() - self._fetched_at) > self._ttl_seconds

    async def _fetch(self) -> None:
        """Fetch the JWKS endpoint and update the key cache."""
        httpx = _httpx()
        jwt = _jwt()
        logger.debug("Fetching JWKS from %s", self._jwks_uri)
        async with httpx.AsyncClient(timeout=self._request_timeout) as client:
            resp = await client.get(self._jwks_uri)
            resp.raise_for_status()
            jwks_data = resp.json()

        # Build kid → key mapping using PyJWT's JWKS utilities
        jwks = jwt.PyJWKSet.from_dict(jwks_data)
        self._keys = {k.key_id: k for k in jwks.keys if k.key_id}
        self._fetched_at = time.monotonic()
        logger.debug(
            "JWKS refreshed: %d keys loaded from %s", len(self._keys), self._jwks_uri
        )

    async def get_key(self, kid: str) -> Optional[object]:
        """Return the signing key for ``kid``, fetching JWKS if needed.

        Attempts a single JWKS refresh on cache miss before returning None.

        Args:
            kid: The ``kid`` (key ID) from the JWT header.

        Returns:
            A PyJWT-compatible signing key, or None if the kid is unknown
            after a refresh attempt.
        """
        async with self._lock:
            # Refresh if stale
            if self._is_stale():
                await self._fetch()

            if kid in self._keys:
                return self._keys[kid]

            # Cache miss — key may be new due to rotation; refresh once
            logger.debug("kid '%s' not found; refreshing JWKS", kid)
            await self._fetch()
            return self._keys.get(kid)


# ---------------------------------------------------------------------------
# JWT token verifier
# ---------------------------------------------------------------------------


class JWTTokenVerifier:
    """Verify Bearer JWTs against a JWKS endpoint.

    Implements the FastMCP ``TokenVerifier`` protocol so this verifier can
    be passed directly to ``FastMCP(token_verifier=...)`` or assigned to
    ``mcp._token_verifier`` before ``mcp.run()`` is called.

    Validation steps performed on each token:
        1. Decode the JWT header to extract ``kid``.
        2. Fetch the matching public key from the JWKS cache.
        3. Verify the JWT signature using RS256 / ES256 (or any alg
           present in the JWKS).
        4. Verify ``exp`` (expiry), ``iss`` (issuer), ``aud`` (audience).
        5. Extract ``client_id`` from the ``sub``, ``azp``, or
           ``client_id`` claim (in that order of preference).
        6. Extract scopes from the ``scope`` claim (space-separated string)
           or the ``scp`` claim (list).

    Args:
        jwks_uri:       JWKS endpoint URL.
        issuer:         Expected value of the ``iss`` claim. Pass None to
                        skip issuer validation (not recommended for production).
        audience:       Expected value of the ``aud`` claim. Pass None to
                        skip audience validation (not recommended for production).
        jwks_ttl:       JWKS cache TTL in seconds (default: 3600).
        leeway_seconds: Clock skew tolerance in seconds (default: 30).
    """

    def __init__(
        self,
        jwks_uri: str,
        issuer: Optional[str] = None,
        audience: Optional[str] = None,
        jwks_ttl: int = 3600,
        leeway_seconds: int = 30,
    ) -> None:
        self._jwks_cache = JWKSCache(jwks_uri, ttl_seconds=jwks_ttl)
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway_seconds

    async def verify_token(self, token: str) -> Optional[object]:
        """Verify a Bearer JWT and return an AccessToken on success.

        Args:
            token: The raw JWT string from the Authorization: Bearer header.

        Returns:
            A ``mcp.server.auth.provider.AccessToken`` instance if the token
            is valid, or None if validation fails for any reason.
        """
        jwt = _jwt()

        # -- Stage 1: decode header to get kid (no signature check yet) --
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.exceptions.DecodeError:
            logger.debug("JWT header decode failed — token is malformed")
            return None

        kid = unverified_header.get("kid")
        if not kid:
            logger.debug("JWT has no 'kid' header — cannot look up signing key")
            return None

        # -- Stage 2: fetch signing key from JWKS cache --
        try:
            signing_key = await self._jwks_cache.get_key(kid)
        except Exception as exc:
            logger.warning("JWKS fetch failed: %s", exc)
            return None

        if signing_key is None:
            logger.debug("Signing key for kid '%s' not found in JWKS", kid)
            return None

        # -- Stage 3: verify signature + registered claims --
        # signing_key is a PyJWK object; .key gives the raw cryptography key
        # needed by jwt.decode() in PyJWT 2.7.x.
        raw_key = signing_key.key if hasattr(signing_key, "key") else signing_key
        decode_kwargs: dict = {
            "algorithms": ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
            "leeway": self._leeway,
            "options": {"verify_exp": True},
        }
        if self._issuer:
            decode_kwargs["issuer"] = self._issuer
        else:
            decode_kwargs["options"]["verify_iss"] = False

        if self._audience:
            decode_kwargs["audience"] = self._audience
        else:
            decode_kwargs["options"]["verify_aud"] = False

        try:
            claims = jwt.decode(token, raw_key, **decode_kwargs)
        except jwt.exceptions.ExpiredSignatureError:
            logger.debug("JWT is expired")
            return None
        except jwt.exceptions.InvalidIssuerError:
            logger.debug("JWT issuer mismatch (expected '%s')", self._issuer)
            return None
        except jwt.exceptions.InvalidAudienceError:
            logger.debug("JWT audience mismatch (expected '%s')", self._audience)
            return None
        except jwt.exceptions.PyJWTError as exc:
            logger.debug("JWT verification failed: %s", exc)
            return None

        # -- Stage 4: build AccessToken from claims --
        # client_id: prefer azp (authorised party), fall back to sub
        client_id = (
            claims.get("azp")
            or claims.get("client_id")
            or claims.get("sub")
            or "unknown"
        )

        # scopes: "scope" is a space-separated string (RFC 6749 / OIDC)
        # "scp" is used by some providers (Azure AD) as a list
        raw_scope = claims.get("scope") or ""
        scp_list = claims.get("scp") or []
        if isinstance(raw_scope, str) and raw_scope:
            scopes = raw_scope.split()
        elif isinstance(scp_list, list):
            scopes = [str(s) for s in scp_list]
        else:
            scopes = []

        expires_at = claims.get("exp")

        # Import AccessToken lazily (requires mcp to be installed)
        try:
            from mcp.server.auth.provider import AccessToken
        except ImportError:  # pragma: no cover
            # Fallback: return a duck-typed object so tests can run without mcp
            class AccessToken:  # type: ignore[no-redef]
                def __init__(self, **kw):
                    for k, v in kw.items():
                        setattr(self, k, v)

        return AccessToken(
            token=token,
            client_id=str(client_id),
            scopes=scopes,
            expires_at=expires_at,
        )

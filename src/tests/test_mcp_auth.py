"""
test_mcp_auth.py — Tests for SHIPS MCP server JWT/Bearer authentication.

Covers:
  JWKSCache    — fetch, caching (TTL and cache hit), kid-miss refresh
  JWTTokenVerifier — valid token, expired, wrong issuer, wrong audience,
                     unknown kid, malformed token, scope extraction,
                     client_id extraction precedence
  CLI wiring   — auth flags accepted with HTTP transports, rejected with
                 stdio, --auth-resource-url required when --auth-jwks-uri set
"""

from __future__ import annotations

import asyncio
import base64
import sys
import time
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test fixtures — RSA key pair and JWKS helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key_pair():
    """Generate a 2048-bit RSA key pair for the test session."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    return private_key, private_key.public_key()


def _int_to_b64url(n: int) -> str:
    """Encode an integer as URL-safe Base64 (no padding) for JWK."""
    length = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


def _make_jwks(public_key, kid: str = "test-kid-1") -> dict:
    """Build a minimal JWKS dict for a given RSA public key."""
    numbers = public_key.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": _int_to_b64url(numbers.n),
                "e": _int_to_b64url(numbers.e),
            }
        ]
    }


def _make_token(
    private_key,
    *,
    kid: str = "test-kid-1",
    sub: str = "client-abc",
    iss: str = "https://issuer.example.com",
    aud: str = "api://ships-mcp",
    exp_offset: int = 3600,
    scope: str = "ships.read ships.deploy",
    extra_claims: dict | None = None,
) -> str:
    """Create a signed RS256 JWT for testing."""
    import jwt

    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "exp": now + exp_offset,
        "iat": now,
        "scope": scope,
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


# ---------------------------------------------------------------------------
# JWKSCache tests
# ---------------------------------------------------------------------------


class TestJWKSCache:
    """Unit tests for JWKSCache fetch and caching behaviour."""

    def test_get_key_fetches_on_first_call(self, rsa_key_pair):
        """A cold cache fetches JWKS and returns the key on first get_key()."""
        private_key, public_key = rsa_key_pair
        jwks = _make_jwks(public_key, kid="k1")

        from ships_mcp_auth import JWKSCache

        cache = JWKSCache("https://example.com/.well-known/jwks.json")

        async def run():
            with patch.object(cache, "_fetch", new=AsyncMock()) as mock_fetch:
                # Populate keys as _fetch would
                import jwt

                cache._keys = {
                    k.key_id: k for k in jwt.PyJWKSet.from_dict(jwks).keys if k.key_id
                }
                cache._fetched_at = time.monotonic()
                mock_fetch.side_effect = None
                key = await cache.get_key("k1")
                return key

        key = asyncio.run(run())
        assert key is not None

    def test_get_key_returns_none_for_unknown_kid_after_refresh(self, rsa_key_pair):
        """Unknown kid returns None after one refresh attempt."""
        _, public_key = rsa_key_pair
        jwks = _make_jwks(public_key, kid="known-kid")

        from ships_mcp_auth import JWKSCache
        import jwt as _jwt

        cache = JWKSCache("https://example.com/.well-known/jwks.json")

        async def _fake_fetch(self_inner=None):
            cache._keys = {
                k.key_id: k for k in _jwt.PyJWKSet.from_dict(jwks).keys if k.key_id
            }
            cache._fetched_at = time.monotonic()

        async def run():
            with patch.object(cache, "_fetch", side_effect=_fake_fetch):
                key = await cache.get_key("unknown-kid")
            return key

        key = asyncio.run(run())
        assert key is None

    def test_stale_cache_triggers_refetch(self, rsa_key_pair):
        """Cache is considered stale when fetched_at is past TTL."""
        from ships_mcp_auth import JWKSCache

        cache = JWKSCache("https://example.com/jwks", ttl_seconds=1)
        cache._fetched_at = time.monotonic() - 10  # expired
        cache._keys = {"old-kid": MagicMock()}

        assert cache._is_stale() is True

    def test_fresh_cache_is_not_stale(self):
        """Cache is not stale when within TTL."""
        from ships_mcp_auth import JWKSCache

        cache = JWKSCache("https://example.com/jwks", ttl_seconds=3600)
        cache._fetched_at = time.monotonic()
        cache._keys = {"some-kid": MagicMock()}

        assert cache._is_stale() is False

    def test_empty_cache_is_stale(self):
        """Empty cache (no keys) is always stale."""
        from ships_mcp_auth import JWKSCache

        cache = JWKSCache("https://example.com/jwks")
        cache._keys = {}
        cache._fetched_at = time.monotonic()

        assert cache._is_stale() is True


# ---------------------------------------------------------------------------
# JWTTokenVerifier tests
# ---------------------------------------------------------------------------


class TestJWTTokenVerifier:
    """Integration tests for JWTTokenVerifier using real RSA keys."""

    def _verifier_with_jwks(self, public_key, kid="test-kid-1"):
        """Return a JWTTokenVerifier whose JWKS cache is pre-populated."""
        import jwt as _jwt
        from ships_mcp_auth import JWTTokenVerifier

        jwks = _make_jwks(public_key, kid=kid)
        verifier = JWTTokenVerifier(
            jwks_uri="https://example.com/.well-known/jwks.json",
            issuer="https://issuer.example.com",
            audience="api://ships-mcp",
        )
        # Pre-populate the cache so no HTTP call is made
        verifier._jwks_cache._keys = {
            k.key_id: k for k in _jwt.PyJWKSet.from_dict(jwks).keys if k.key_id
        }
        verifier._jwks_cache._fetched_at = time.monotonic()
        return verifier

    def test_valid_token_returns_access_token(self, rsa_key_pair):
        """A well-formed, valid JWT returns an AccessToken."""
        private_key, public_key = rsa_key_pair
        verifier = self._verifier_with_jwks(public_key)
        token = _make_token(private_key)

        async def run():
            return await verifier.verify_token(token)

        result = asyncio.run(run())
        assert result is not None
        assert result.client_id == "client-abc"

    def test_valid_token_scopes_extracted(self, rsa_key_pair):
        """Scopes from the 'scope' claim are extracted correctly."""
        private_key, public_key = rsa_key_pair
        verifier = self._verifier_with_jwks(public_key)
        token = _make_token(private_key, scope="ships.read ships.deploy ships.admin")

        result = asyncio.run(verifier.verify_token(token))
        assert result is not None
        assert "ships.read" in result.scopes
        assert "ships.deploy" in result.scopes
        assert "ships.admin" in result.scopes

    def test_scp_list_scopes_extracted(self, rsa_key_pair):
        """Scopes from the 'scp' list claim (Azure AD style) are extracted."""
        private_key, public_key = rsa_key_pair
        verifier = self._verifier_with_jwks(public_key)
        token = _make_token(
            private_key, scope="", extra_claims={"scp": ["ships.read", "ships.deploy"]}
        )

        result = asyncio.run(verifier.verify_token(token))
        assert result is not None
        assert "ships.read" in result.scopes

    def test_azp_claim_used_as_client_id(self, rsa_key_pair):
        """azp claim is preferred over sub for client_id."""
        private_key, public_key = rsa_key_pair
        verifier = self._verifier_with_jwks(public_key)
        token = _make_token(
            private_key, sub="user-123", extra_claims={"azp": "service-client-xyz"}
        )

        result = asyncio.run(verifier.verify_token(token))
        assert result is not None
        assert result.client_id == "service-client-xyz"

    def test_expired_token_returns_none(self, rsa_key_pair):
        """An expired JWT returns None."""
        private_key, public_key = rsa_key_pair
        verifier = self._verifier_with_jwks(public_key)
        token = _make_token(private_key, exp_offset=-3600)  # expired 1 hour ago

        result = asyncio.run(verifier.verify_token(token))
        assert result is None

    def test_wrong_issuer_returns_none(self, rsa_key_pair):
        """A token with the wrong issuer returns None."""
        private_key, public_key = rsa_key_pair
        verifier = self._verifier_with_jwks(public_key)
        token = _make_token(private_key, iss="https://wrong-issuer.evil.com")

        result = asyncio.run(verifier.verify_token(token))
        assert result is None

    def test_wrong_audience_returns_none(self, rsa_key_pair):
        """A token with the wrong audience returns None."""
        private_key, public_key = rsa_key_pair
        verifier = self._verifier_with_jwks(public_key)
        token = _make_token(private_key, aud="api://some-other-service")

        result = asyncio.run(verifier.verify_token(token))
        assert result is None

    def test_unknown_kid_triggers_cache_refresh_then_returns_none(self, rsa_key_pair):
        """A token with an unknown kid triggers a JWKS refresh, then returns None."""
        private_key, public_key = rsa_key_pair
        verifier = self._verifier_with_jwks(public_key, kid="known-kid")
        # Token signed with a different kid
        token = _make_token(private_key, kid="unknown-kid")

        refresh_called = []

        async def mock_fetch():
            refresh_called.append(True)
            # Don't actually add the new key — simulates unknown kid post-refresh
            verifier._jwks_cache._fetched_at = time.monotonic()

        verifier._jwks_cache._fetch = mock_fetch

        result = asyncio.run(verifier.verify_token(token))
        assert result is None
        assert len(refresh_called) >= 1  # refresh was attempted

    def test_malformed_token_returns_none(self, rsa_key_pair):
        """A completely malformed token string returns None."""
        _, public_key = rsa_key_pair
        verifier = self._verifier_with_jwks(public_key)

        result = asyncio.run(verifier.verify_token("not.a.jwt"))
        assert result is None

    def test_issuer_not_validated_when_none(self, rsa_key_pair):
        """When issuer is None, iss claim is not checked."""
        private_key, public_key = rsa_key_pair
        import jwt as _jwt
        from ships_mcp_auth import JWTTokenVerifier

        jwks = _make_jwks(public_key)
        verifier = JWTTokenVerifier(
            jwks_uri="https://example.com/.well-known/jwks.json",
            issuer=None,  # no issuer check
            audience="api://ships-mcp",
        )
        verifier._jwks_cache._keys = {
            k.key_id: k for k in _jwt.PyJWKSet.from_dict(jwks).keys if k.key_id
        }
        verifier._jwks_cache._fetched_at = time.monotonic()

        token = _make_token(private_key, iss="https://anything.example.com")
        result = asyncio.run(verifier.verify_token(token))
        assert result is not None

    def test_audience_not_validated_when_none(self, rsa_key_pair):
        """When audience is None, aud claim is not checked."""
        private_key, public_key = rsa_key_pair
        import jwt as _jwt
        from ships_mcp_auth import JWTTokenVerifier

        jwks = _make_jwks(public_key)
        verifier = JWTTokenVerifier(
            jwks_uri="https://example.com/.well-known/jwks.json",
            issuer="https://issuer.example.com",
            audience=None,  # no audience check
        )
        verifier._jwks_cache._keys = {
            k.key_id: k for k in _jwt.PyJWKSet.from_dict(jwks).keys if k.key_id
        }
        verifier._jwks_cache._fetched_at = time.monotonic()

        token = _make_token(private_key, aud="api://any-audience")
        result = asyncio.run(verifier.verify_token(token))
        assert result is not None


# ---------------------------------------------------------------------------
# CLI auth flag tests (uses sys.modules mocking from test_mcp_transport.py)
# ---------------------------------------------------------------------------


class FakeSettings:
    """Minimal replica of FastMCP Settings for auth CLI tests."""

    def __init__(self):
        self.host = "127.0.0.1"
        self.port = 8000
        self.log_level = "INFO"
        self.stateless_http = False
        self.streamable_http_path = "/mcp"
        self.sse_path = "/sse"
        self.auth = None


class FakeFastMCP:
    """Minimal replica of FastMCP for auth CLI tests."""

    def __init__(self, *args, **kwargs):
        self.settings = FakeSettings()
        self._token_verifier = None
        self._run = MagicMock()
        # Stub so tests that inspect real FastMCP internals don't AttributeError
        self._tool_manager = MagicMock()
        self._tool_manager._tools = {}

    def tool(self, *args, **kwargs):
        # Real FastMCP accepts ``name=...``, ``description=...`` and other
        # registration kwargs. Accept and ignore them so module-level
        # ``@mcp.tool(name="...")`` decorators in ships_mcp don't error
        # during the fake-mcp reload.
        def decorator(fn):
            return fn

        return decorator

    def run(self, transport="stdio"):
        self._run(transport=transport)


def _install_fake_mcp_for_auth() -> FakeFastMCP:
    """Inject fake mcp modules and return the FakeFastMCP instance."""
    instance = FakeFastMCP()
    fake_fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fake_fastmcp_mod.FastMCP = lambda *a, **kw: instance  # type: ignore
    # ``ships_mcp`` does ``from mcp.server.fastmcp import Context, FastMCP``
    # — any attribute the real package exposes that ``ships_mcp`` reads at
    # module import time must be stubbed here, otherwise the reload at
    # the top of the auth fixture raises ``ImportError: cannot import
    # name 'Context' from 'mcp.server.fastmcp' (unknown location)`` and
    # pollutes ``sys.modules`` for every subsequent test in the run.
    fake_fastmcp_mod.Context = MagicMock  # type: ignore

    # Also fake mcp.server.auth.settings so the import in main() works
    fake_auth_settings_mod = types.ModuleType("mcp.server.auth.settings")

    class FakeAuthSettings:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    fake_auth_settings_mod.AuthSettings = FakeAuthSettings
    fake_auth_mod = types.ModuleType("mcp.server.auth")

    sys.modules.setdefault("mcp", types.ModuleType("mcp"))
    sys.modules.setdefault("mcp.server", types.ModuleType("mcp.server"))
    sys.modules["mcp.server.fastmcp"] = fake_fastmcp_mod
    sys.modules["mcp.server.auth"] = fake_auth_mod
    sys.modules["mcp.server.auth.settings"] = fake_auth_settings_mod

    return instance


@pytest.fixture(autouse=True)
def fresh_ships_mcp_for_auth():
    """Reload ships_mcp with fresh fakes before each auth CLI test.

    Saves the pre-test state of every sys.modules entry we modify and
    restores them on teardown so subsequent test files that use the real
    mcp package are not contaminated by the fakes.
    """
    MCP_KEYS = [
        "mcp",
        "mcp.server",
        "mcp.server.fastmcp",
        "mcp.server.auth",
        "mcp.server.auth.settings",
    ]

    # Snapshot originals before touching anything (None = didn't exist)
    originals = {k: sys.modules.get(k) for k in MCP_KEYS}
    ships_mcp_original = sys.modules.pop("ships_mcp", None)

    try:
        _install_fake_mcp_for_auth()
        import ships_mcp  # noqa: F401

        yield
    finally:
        # Teardown: drop ships_mcp and restore every mcp.* entry.
        # MUST run even if the setup ``import ships_mcp`` raises, or a
        # fakes-shaped ``sys.modules`` leaks into every subsequent test
        # file in the run and they fail with cascading ImportErrors.
        sys.modules.pop("ships_mcp", None)
        for key, original_value in originals.items():
            if original_value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = original_value

        if ships_mcp_original is not None:
            sys.modules["ships_mcp"] = ships_mcp_original


def _run_auth_main(argv):
    import ships_mcp as m

    old_argv = sys.argv
    try:
        sys.argv = ["ships_mcp"] + argv
        m.main()
    finally:
        sys.argv = old_argv


class TestAuthCLIFlags:
    """--auth-* CLI flag validation and wiring."""

    def test_auth_jwks_uri_with_stdio_raises(self):
        """--auth-jwks-uri with stdio is rejected."""
        with pytest.raises(SystemExit) as exc_info:
            _run_auth_main(
                [
                    "--auth-jwks-uri",
                    "https://example.com/jwks",
                    "--auth-resource-url",
                    "http://localhost:8000",
                ]
            )
        assert exc_info.value.code != 0

    def test_auth_without_resource_url_raises(self):
        """--auth-jwks-uri without --auth-resource-url is rejected."""
        with pytest.raises(SystemExit) as exc_info:
            _run_auth_main(
                [
                    "--transport",
                    "streamable-http",
                    "--auth-jwks-uri",
                    "https://example.com/jwks",
                ]
            )
        assert exc_info.value.code != 0

    def test_auth_with_http_transport_accepted(self):
        """--auth-jwks-uri with streamable-http and --auth-resource-url is valid."""
        with patch("ships_mcp_auth.JWTTokenVerifier") as MockVerifier:
            MockVerifier.return_value = MagicMock()
            _run_auth_main(
                [
                    "--transport",
                    "streamable-http",
                    "--auth-jwks-uri",
                    "https://example.com/jwks",
                    "--auth-issuer",
                    "https://issuer.example.com",
                    "--auth-audience",
                    "api://ships-mcp",
                    "--auth-resource-url",
                    "http://ships-mcp.internal:8000",
                ]
            )
        MockVerifier.assert_called_once()

    def test_auth_sets_token_verifier_on_mcp(self):
        """When auth is configured, mcp._token_verifier is set."""
        import ships_mcp as m

        fake_verifier = MagicMock()

        with patch("ships_mcp_auth.JWTTokenVerifier", return_value=fake_verifier):
            _run_auth_main(
                [
                    "--transport",
                    "streamable-http",
                    "--auth-jwks-uri",
                    "https://example.com/jwks",
                    "--auth-resource-url",
                    "http://ships-mcp.internal:8000",
                ]
            )
        assert m.mcp._token_verifier is fake_verifier

    def test_auth_jwks_uri_passed_to_verifier(self):
        """The JWKS URI is forwarded to JWTTokenVerifier."""
        with patch("ships_mcp_auth.JWTTokenVerifier") as MockVerifier:
            MockVerifier.return_value = MagicMock()
            _run_auth_main(
                [
                    "--transport",
                    "streamable-http",
                    "--auth-jwks-uri",
                    "https://my-idp.example.com/keys",
                    "--auth-resource-url",
                    "http://ships:8000",
                ]
            )
        call_kwargs = MockVerifier.call_args
        assert call_kwargs.kwargs.get("jwks_uri") == "https://my-idp.example.com/keys"

    def test_no_auth_leaves_token_verifier_none(self):
        """Without --auth-jwks-uri, mcp._token_verifier remains None."""
        import ships_mcp as m

        _run_auth_main(["--transport", "streamable-http"])
        assert m.mcp._token_verifier is None

    def test_auth_sse_transport_accepted(self):
        """--auth-jwks-uri with sse transport is valid."""
        with patch("ships_mcp_auth.JWTTokenVerifier") as MockVerifier:
            MockVerifier.return_value = MagicMock()
            _run_auth_main(
                [
                    "--transport",
                    "sse",
                    "--auth-jwks-uri",
                    "https://example.com/jwks",
                    "--auth-resource-url",
                    "http://ships-mcp.internal:8000",
                ]
            )
        MockVerifier.assert_called_once()

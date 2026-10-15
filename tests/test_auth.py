from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from fv_data_mcp.auth import Authenticator, Memberships, Principal
from fv_data_mcp.config import Settings
from fv_data_mcp.errors import ResearchError


@pytest.fixture
def oauth() -> tuple[Authenticator, Callable[..., str], list[httpx.Request]]:
    """Build an RSA-signed token factory, mocked JWKS endpoint, and invited identity."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())) | {
        "kid": "test-key"
    }
    settings = Settings(
        _env_file=None,
        cursor_secret="s" * 32,
        auth0_issuer="https://tenant.auth0.com/",
        auth0_audience="http://localhost:8000/mcp",
    )
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        """Record a key-discovery request and return the test RSA public key."""
        calls.append(request)
        return httpx.Response(200, json={"keys": [public]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    members = Memberships(settings)
    members.memory[Principal(settings.auth0_issuer, "auth0|owner").key] = True
    auth = Authenticator(settings, client, members)

    def token(**overrides: Any) -> str:
        """Sign standard test claims with optional adversarial claim overrides."""
        claims = {
            "iss": settings.auth0_issuer,
            "aud": settings.auth0_audience,
            "sub": "auth0|owner",
            "scope": "research:read",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        }
        return jwt.encode(
            claims | overrides, private, algorithm="RS256", headers={"kid": "test-key"}
        )

    return auth, token, calls


async def test_valid_auth_and_immediate_suspension(
    oauth: tuple[Authenticator, Callable[..., str], list[httpx.Request]],
) -> None:
    """Deny subsequent requests after suspension without repeatedly fetching known keys."""
    auth, token, calls = oauth
    principal = await auth.authenticate(token())
    await auth.members.set_access(principal, False)
    with pytest.raises(ResearchError, match="suspended"):
        await auth.authenticate(token())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "other"},
        {"iss": "https://attacker.example/"},
        {"exp": 1},
        {"scope": "openid"},
        {"sub": "auth0|uninvited"},
    ],
)
async def test_invalid_claims_and_uninvited_user(
    oauth: tuple[Authenticator, Callable[..., str], list[httpx.Request]], claims: dict[str, Any]
) -> None:
    """Reject invalid identity claims, insufficient scope, and uninvited subjects."""
    auth, token, _ = oauth
    with pytest.raises(ResearchError):
        await auth.authenticate(token(**claims))


async def test_unknown_kids_cannot_trigger_unbounded_jwks_fetches(
    oauth: tuple[Authenticator, Callable[..., str], list[httpx.Request]],
) -> None:
    """Bound key-discovery requests even when many unknown key IDs are supplied."""
    auth, token, calls = oauth
    await auth.authenticate(token())
    for index in range(20):
        with pytest.raises(ResearchError):
            await auth._key(f"unknown-{index}")
    assert len(calls) == 1

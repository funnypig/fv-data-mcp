from __future__ import annotations

import pytest
from pydantic import ValidationError

from fv_data_mcp.auth import member_keys
from fv_data_mcp.config import Settings


@pytest.mark.parametrize("issuer", ["https://tenant.auth0.com", "https://tenant.auth0.com/"])
@pytest.mark.parametrize("dev_auth_enabled", [False, True])
def test_auth0_issuer_normalization(issuer: str, dev_auth_enabled: bool) -> None:
    """Use identical canonical issuers and membership keys for server and operator settings."""
    settings = Settings(
        _env_file=None,
        cursor_secret="s" * 32,
        dev_token="d" * 32,
        dev_auth_enabled=dev_auth_enabled,
        auth0_issuer=issuer,
        auth0_audience="http://localhost:8000/mcp",
    )
    assert settings.auth0_issuer == "https://tenant.auth0.com/"
    assert member_keys(settings.auth0_issuer, "auth0|owner") == member_keys(
        "https://tenant.auth0.com/", "auth0|owner"
    )


@pytest.mark.parametrize(
    "issuer",
    [
        "http://tenant.auth0.com",
        "tenant.auth0.com",
        "https:///",
        "https://tenant.auth0.com/path",
        "https://tenant.auth0.com//",
        "https://tenant.auth0.com/?query=value",
        "https://tenant.auth0.com/#fragment",
        "https://user:password@tenant.auth0.com/",
    ],
)
@pytest.mark.parametrize("dev_auth_enabled", [False, True])
def test_invalid_auth0_issuer_rejected(issuer: str, dev_auth_enabled: bool) -> None:
    """Reject non-origin issuer URLs rather than repairing potentially unsafe configuration."""
    with pytest.raises(ValidationError, match="auth0_issuer"):
        Settings(
            _env_file=None,
            cursor_secret="s" * 32,
            dev_token="d" * 32,
            dev_auth_enabled=dev_auth_enabled,
            auth0_issuer=issuer,
            auth0_audience="http://localhost:8000/mcp",
        )


def test_empty_issuer_allowed_only_for_development_authentication() -> None:
    """Keep Auth0 optional for local development but mandatory for OAuth authentication."""
    settings = Settings(
        _env_file=None,
        cursor_secret="s" * 32,
        dev_token="d" * 32,
        dev_auth_enabled=True,
        auth0_issuer="",
    )
    assert settings.auth0_issuer == ""
    with pytest.raises(ValidationError, match="auth0_issuer is required"):
        Settings(_env_file=None, cursor_secret="s" * 32, auth0_issuer="")

from __future__ import annotations

from argparse import Namespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from fv_data_mcp.auth import Memberships, Principal, member_keys
from fv_data_mcp.cli import manage
from fv_data_mcp.config import Settings
from fv_data_mcp.errors import ResearchError


async def test_azure_async_clients_construct_without_network(settings: Settings) -> None:
    """Construct and close the Azure async clients using only local configuration."""
    settings.access_backend = "azure"
    settings.table_endpoint = "https://example.table.core.windows.net/"
    members = Memberships(settings)
    await members.close()


async def test_azure_allowlist_denies_missing_and_fails_closed_on_outage(
    settings: Settings,
) -> None:
    """Distinguish missing membership from backend outages and persist suspension state."""
    members = Memberships(settings)
    members.table = AsyncMock()
    principal = Principal("https://tenant.auth0.com/", "auth0|user")
    members.table.get_entity.return_value = {"enabled": True}
    assert await members.allowed(principal)
    members.table.get_entity.assert_awaited_with(*member_keys(principal.issuer, principal.subject))
    members.table.get_entity.side_effect = ResourceNotFoundError()
    assert not await members.allowed(principal)
    members.table.get_entity.side_effect = HttpResponseError()
    with pytest.raises(ResearchError, match="unavailable"):
        await members.allowed(principal)
    await members.set_access(principal, False)
    assert members.table.upsert_entity.call_args.args[0]["enabled"] is False
    await members.close()


async def test_operator_grant_and_suspend_use_real_issuer(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Administer the exact issuer-qualified identity without leaking user details to output."""
    settings.access_backend = "azure"
    settings.auth0_issuer = "https://tenant.auth0.com/"
    members = AsyncMock()
    monkeypatch.setattr("fv_data_mcp.cli.Memberships", lambda settings: members)
    await manage(Namespace(command="grant", subject="auth0|user"), settings)
    members.set_access.assert_awaited_with(Principal(settings.auth0_issuer, "auth0|user"), True)
    await manage(Namespace(command="suspend", subject="auth0|user"), settings)
    members.set_access.assert_awaited_with(Principal(settings.auth0_issuer, "auth0|user"), False)
    assert '"status": "ok"' in capsys.readouterr().out


@pytest.mark.parametrize(
    "overrides",
    [
        {"environment": "production"},
        {"public_url": "https://public.example"},
        {"cursor_secret": "too-short"},
        {"dev_token": "too-short"},
    ],
)
def test_unsafe_local_configuration_rejected(overrides: dict[str, Any]) -> None:
    """Reject development authentication settings that violate deployment safety constraints."""
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            **(
                {"dev_auth_enabled": True, "dev_token": "x" * 24, "cursor_secret": "s" * 32}
                | overrides
            ),
        )


def test_startup_validation_does_not_echo_environment_secrets() -> None:
    """Keep raw API and Redis credentials out of configuration-validation errors."""
    with pytest.raises(ValueError) as caught:
        Settings(
            _env_file=None,
            finviz_api_key="private-api-credential",
            redis_url="rediss://:private-password@cache.example",
            cursor_secret="short",
        )
    assert "private-api-credential" not in str(caught.value)
    assert "private-password" not in str(caught.value)

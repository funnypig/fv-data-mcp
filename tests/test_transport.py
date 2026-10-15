from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, NoReturn

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

from fv_data_mcp.auth import Principal
from fv_data_mcp.cache import Payload
from fv_data_mcp.config import Settings
from fv_data_mcp.errors import ResearchError
from fv_data_mcp.models import CachedResult
from fv_data_mcp.server import Runtime, create_app


@asynccontextmanager
async def client_for(
    settings: Settings, redis: FakeRedis
) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient]]:
    """Yield an authenticated in-process HTTP client and close all runtime resources."""
    runtime = Runtime(
        settings,
        redis=redis,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ),
    )
    app = create_app(settings, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=settings.public_url,
            headers={
                "Authorization": "Bearer " + settings.dev_token.get_secret_value(),
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            },
        ) as client,
    ):
        yield runtime, client


async def rpc(
    client: httpx.AsyncClient, method: str, params: dict[str, Any] | None = None
) -> httpx.Response:
    """Send one JSON-RPC request through the actual MCP HTTP endpoint."""
    return await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )


async def test_metadata_public_tools_protected(settings: Settings, redis: FakeRedis) -> None:
    """Keep resource discovery public while challenging unauthenticated MCP requests."""
    async with client_for(settings, redis) as (_, client):
        response = await client.get(
            "/.well-known/oauth-protected-resource", headers={"Authorization": ""}
        )
        assert response.json()["resource"] == "http://localhost:8000/mcp"
        response = await client.post("/mcp", headers={"Authorization": ""})
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]
        assert response.headers["x-request-id"]


async def test_initialization_tools_and_real_call(settings: Settings, redis: FakeRedis) -> None:
    """Check typed tool discovery, normalized source reuse, and warm-cache access revocation."""
    async with client_for(settings, redis) as (runtime, client):
        initialized = await rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        assert initialized.status_code == 200, initialized.text
        listing = await rpc(client, "tools/list")
        tools = listing.json()["result"]["tools"]
        assert len(tools) == 12
        assert all(
            tool["annotations"]["readOnlyHint"] and tool.get("outputSchema") for tool in tools
        )
        fetches = 0

        async def quote(ticker: str) -> CachedResult:
            """Return a normalized test quote through the real shared cache."""

            async def load() -> Payload:
                """Count the actual synthetic quote fetch and return its source payload."""
                nonlocal fetches
                fetches += 1
                return Payload({"ticker": ticker, "price": 100})

            return await runtime.cache.fetch("quote", {"ticker": ticker}, load)

        runtime.service.finviz.quote = quote
        for symbol in [" test ", "TEST"]:
            response = await rpc(
                client, "tools/call", {"name": "get_stock_quote", "arguments": {"ticker": symbol}}
            )
            body = response.json()["result"]
            assert not body["isError"], body
            assert body["structuredContent"]["data"]["price"] == 100
            assert body["structuredContent"]["request_id"] == response.headers["x-request-id"]
        assert fetches == 1
        await runtime.members.set_access(Principal("local", "developer"), False)
        response = await rpc(
            client, "tools/call", {"name": "get_stock_quote", "arguments": {"ticker": "TEST"}}
        )
        assert response.status_code == 403
        assert fetches == 1


async def test_tool_error_and_invalid_arguments(settings: Settings, redis: FakeRedis) -> None:
    """Expose structured tool failures and reject arguments outside declared bounds."""
    async with client_for(settings, redis) as (runtime, client):

        async def fail(ticker: str) -> NoReturn:
            """Simulate a retryable tool-level provider failure."""
            raise ResearchError("provider_unavailable", "temporarily unavailable", True)

        runtime.service.quote = fail
        response = await rpc(
            client, "tools/call", {"name": "get_stock_quote", "arguments": {"ticker": "TEST"}}
        )
        assert response.json()["result"]["isError"]
        assert (
            response.json()["result"]["structuredContent"]["error"]["code"]
            == "provider_unavailable"
        )
        response = await rpc(
            client,
            "tools/call",
            {
                "name": "get_option_chain",
                "arguments": {"ticker": "TEST", "expiration": "2027-01-15", "page_size": 251},
            },
        )
        assert response.json()["result"]["isError"]


@pytest.mark.parametrize(
    "headers,expected", [({"Origin": "https://evil.example"}, 403), ({"Host": "evil.example"}, 400)]
)
async def test_origin_host_protection(
    settings: Settings, redis: FakeRedis, headers: dict[str, str], expected: int
) -> None:
    """Reject requests carrying untrusted Origin or Host headers."""
    async with client_for(settings, redis) as (_, client):
        response = await client.post("/mcp", headers=headers, json={})
        assert response.status_code == expected


async def test_deadline_response_cap_and_health(settings: Settings, redis: FakeRedis) -> None:
    """Verify health routes, bounded tool output, and execution deadlines."""
    import asyncio

    async with client_for(settings, redis) as (runtime, client):
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200

        async def large(ticker: str) -> dict[str, Any]:
            """Return an intentionally oversized JSON payload to exercise response limits."""
            return {"data": {"text": "x" * 200000}}

        runtime.service.quote = large
        response = await rpc(
            client, "tools/call", {"name": "get_stock_quote", "arguments": {"ticker": "TEST"}}
        )
        assert (
            response.json()["result"]["structuredContent"]["error"]["code"] == "response_too_large"
        )
        settings.request_timeout = 0.01

        async def slow(ticker: str) -> None:
            """Delay completion beyond the test tool deadline."""
            await asyncio.sleep(5)

        runtime.service.quote = slow
        response = await rpc(
            client, "tools/call", {"name": "get_stock_quote", "arguments": {"ticker": "TEST"}}
        )
        assert (
            response.json()["result"]["structuredContent"]["error"]["code"] == "deadline_exceeded"
        )

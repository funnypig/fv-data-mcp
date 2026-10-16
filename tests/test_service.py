from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime
from typing import Any, NoReturn

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from pydantic import SecretStr

from fv_data_mcp.config import Settings
from fv_data_mcp.models import utc_now
from fv_data_mcp.server import Runtime, create_app


@pytest.fixture(autouse=True)
def fixed_market_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep modeled dates deterministic without changing event-loop or cache clocks."""
    fixed = datetime(2026, 10, 13, 14, tzinfo=UTC)
    for target in (
        "fv_data_mcp.service.utc_now",
        "fv_data_mcp.providers.finviz.utc_now",
        "fv_data_mcp.providers.rates.utc_now",
    ):
        monkeypatch.setattr(target, lambda: fixed)
    monkeypatch.setitem(globals(), "utc_now", lambda: fixed)


def market_transport() -> tuple[httpx.MockTransport, Counter[str]]:
    """Provide counted mock responses for supported market-data endpoints."""
    calls = Counter()

    def respond(request: httpx.Request) -> httpx.Response:
        """Return synthetic provider payloads and reject unexpected endpoint requests."""
        path = request.url.path
        calls[path] += 1
        if path == "/export/options":
            if "e" not in request.url.params:
                return httpx.Response(
                    200,
                    text="Contract Name,Expiry\nTEST270115C00100000,1/15/2027\nTEST270219C00100000,2/19/2027\n",
                )
            expiration = date.fromisoformat(request.url.params["e"])
            code = expiration.strftime("%y%m%d")
            return httpx.Response(
                200,
                text=f"Contract Name,Strike,Type,Open Int.,Bid,Ask,Volume,IV,Delta,Gamma\nTEST{code}C00100000,100,call,100,1,1.1,50,0.3,0.5,0.02\nTEST{code}P00110000,110,put,50,1,1.1,20,0.4,-0.5,0.01\n",
            )
        if path == "/quote_export":
            return httpx.Response(200, text="Date,Close\n10/13/2026 10:00 AM,100\n")
        if path == "/export":
            return httpx.Response(
                200,
                text="Ticker,Company,P/E,Dividend Yield\nTEST,Test company,20,0.00%\n",
            )
        if path == "/api/statement":
            return httpx.Response(200, json={"data": {"revenue": [100, 120]}})
        if path == "/news_export":
            return httpx.Response(
                200, text="Title,Date,Url\nTest headline,2026-10-13,https://example.com/story\n"
            )
        if path.endswith("/xml"):
            return httpx.Response(
                200,
                text=f"<feed><properties><NEW_DATE>{utc_now().date().isoformat()}T00:00:00</NEW_DATE><BC_3MONTH>4.0</BC_3MONTH><BC_1YEAR>4.5</BC_1YEAR></properties></feed>",
            )
        pytest.fail(f"Unexpected endpoint: {path}")

    return httpx.MockTransport(respond), calls


async def test_all_option_tools_share_api_fetches_and_snapshot_pages(
    settings: Settings, redis: FakeRedis
) -> None:
    """Exercise real MCP dispatch while asserting source reuse and stable cursor pages."""
    settings.finviz_api_key = SecretStr("testing-key")
    transport, calls = market_transport()
    runtime = Runtime(settings, redis=redis, client=httpx.AsyncClient(transport=transport))
    app = create_app(settings, runtime)
    headers = {
        "Authorization": "Bearer " + settings.dev_token.get_secret_value(),
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=settings.public_url, headers=headers
        ) as client,
    ):

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            """Invoke one MCP tool and return its successful structured response."""
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            body = response.json()["result"]
            assert not body["isError"], body
            return body["structuredContent"]

        arguments = {"ticker": "TEST", "expiration": "2027-01-15", "page_size": 1}
        first = await call("get_option_chain", arguments)
        second = await call(
            "get_option_chain", arguments | {"cursor": first["data"]["next_cursor"]}
        )
        assert first["sources"][0]["snapshot_id"] == second["sources"][0]["snapshot_id"]
        assert first["data"]["contracts"][0]["symbol"] != second["data"]["contracts"][0]["symbol"]
        await call("get_max_pain", {"ticker": "TEST", "expirations": ["2027-01-15"]})
        await call("get_option_metrics", {"ticker": "TEST", "expiration": "2027-01-15"})
        gamma = await call("get_gamma_exposure", {"ticker": "TEST", "expirations": ["2027-01-15"]})
        assert gamma["data"]["gamma_flip"]["status"] != "unavailable"
        assert calls["/export/options"] == 1
        await call("list_option_expirations", {"ticker": "TEST"})
        await call("get_max_pain", {"ticker": "TEST"})
        assert calls["/export/options"] == 2
        summary = await call("get_options_summary", {"ticker": "TEST"})
        assert len(summary["data"]["expirations"]) == 2
        assert calls["/export/options"] == 3
        assert calls["/quote_export"] == 1
        await call("get_stock_overview", {"ticker": "TEST"})
        await call("get_financial_statements", {"ticker": "TEST"})
        stock_news = await call("get_stock_news", {"ticker": "TEST"})
        await call("get_market_news", {})
        assert len(stock_news["data"]["items"][0]["article_id"]) == 64


async def test_missing_reference_data_degrades_flip_not_gex(
    settings: Settings, redis: FakeRedis
) -> None:
    """Preserve valid quoted exposure when missing reference rates prevent flip modeling."""
    from fv_data_mcp.errors import ResearchError

    settings.finviz_api_key = SecretStr("testing-key")
    transport, _ = market_transport()
    runtime = Runtime(settings, redis=redis, client=httpx.AsyncClient(transport=transport))

    async def unavailable() -> NoReturn:
        """Simulate missing Treasury reference data without returning a curve."""
        raise ResearchError("reference_data_unavailable", "missing", True)

    runtime.service.treasury.curve = unavailable
    try:
        output = await runtime.service.gamma("TEST", [date(2027, 1, 15)])
        assert output["status"] == "partial"
        assert output["data"]["net_gex"] == 15000
        assert output["data"]["gamma_flip"]["status"] == "unavailable"
    finally:
        await runtime.close()

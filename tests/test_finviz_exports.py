"""Regression coverage for verified Finviz export schemas and canonical endpoints."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

from fv_data_mcp.cache import SnapshotCache
from fv_data_mcp.errors import ResearchError
from fv_data_mcp.limits import Limits
from fv_data_mcp.providers.finviz import FinvizProvider, parse_bar_time
from fv_data_mcp.providers.parsing import parse_export_expirations, parse_overview_export


def test_export_expirations_are_sorted_and_cross_checked() -> None:
    """Deduplicate actual exported dates without inventing calendar-based expirations."""
    text = (
        "Contract Name,Expiry\n"
        "TEST270219C00100000,2/19/2027\n"
        "TEST270115P00100000,1/15/2027\n"
        "TEST270115C00100000,1/15/2027\n"
        "TEST1270319C00100000,3/19/2027\n"
    )
    assert parse_export_expirations(text, "TEST") == [date(2027, 1, 15), date(2027, 2, 19)]
    assert parse_export_expirations("Contract Name,Expiry\n", "TEST") == []


@pytest.mark.parametrize(
    "text",
    [
        "Contract Name,Strike\nTEST270115C00100000,100\n",
        "Contract Name,Expiry\nTEST270115C00100000,2/19/2027\n",
        "Contract Name,Expiry\nTEST270115C00100000,invalid\n",
        "Contract Name,Expiry\nnot-a-contract,1/15/2027\n",
        "Contract Name,Expiry\nOTHER270115C00100000,1/15/2027\n",
        "<html>Login required</html>",
    ],
)
def test_export_expirations_reject_inconsistent_metadata(text: str) -> None:
    """Fail closed instead of guessing dates from malformed or wrong-ticker exports."""
    with pytest.raises(ResearchError) as caught:
        parse_export_expirations(text, "TEST")
    assert caught.value.code == "provider_schema_changed"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0.32%", 0.0032), ("0.00%", 0.0), ("-", None), ("", None), ("0.32", None)],
)
def test_screener_overview_preserves_unknown_values(value: str, expected: float | None) -> None:
    """Read explicit percent units without inventing dividend yields or profile data."""
    result = parse_overview_export(
        "No.,Ticker,Company,P/E\n1,TEST,Test company,20\n",
        f"No.,Ticker,Dividend Yield,Market Cap\n1,TEST,{value},100\n",
        "TEST",
    )
    assert result["indicated_dividend_yield"] == expected
    assert result["description"] is None
    assert result["institutional_ownership"] == []
    assert result["metrics"]["Company"] == "Test company"
    assert "No." not in result["metrics"]


@pytest.mark.parametrize(
    "text",
    ["Ticker,Company\nOTHER,Other\n", "Ticker,Company\nTEST,Test\nOTHER,Other\n"],
)
def test_screener_overview_rejects_unexpected_rows(text: str) -> None:
    """Do not attach another company's fundamentals to the requested ticker."""
    with pytest.raises(ResearchError) as caught:
        parse_overview_export(text, "Ticker,Dividend Yield\nTEST,0.00%\n", "TEST")
    assert caught.value.code == "provider_schema_changed"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10/12/2026 04:00 AM", datetime(2026, 10, 12, 8, tzinfo=UTC)),
        ("01/12/2026 04:00 AM", datetime(2026, 1, 12, 9, tzinfo=UTC)),
        ("10/12/2026 12:00 PM", datetime(2026, 10, 12, 16, tzinfo=UTC)),
        ("10/12/2026 12:00 AM", datetime(2026, 10, 12, 4, tzinfo=UTC)),
        ("10/12/2026 04:00:30 AM", datetime(2026, 10, 12, 8, 0, 30, tzinfo=UTC)),
        ("2026-10-12T08:00:00Z", datetime(2026, 10, 12, 8, tzinfo=UTC)),
        ("invalid", None),
    ],
)
def test_export_bar_timestamp_formats(value: str, expected: datetime | None) -> None:
    """Interpret AM/PM bars in New York time across summer/winter without losing UTC support."""
    assert parse_bar_time(value) == expected


async def test_expiration_export_uses_shared_request_cache(
    redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filter expired dates and reuse the credentialed raw export across provider instances."""
    now = datetime(2026, 10, 16, 21, tzinfo=UTC)
    monkeypatch.setattr("fv_data_mcp.providers.finviz.utc_now", lambda: now)
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Return a fixed export and reject browser routes or untrusted origins."""
        calls.append(request)
        assert request.url.host == "elite.finviz.com"
        assert request.url.path == "/export/options"
        assert dict(request.url.params) == {"t": "TEST", "ty": "oc", "auth": "secret"}
        return httpx.Response(
            200,
            text="Contract Name,Expiry\nTEST261016C00100000,10/16/2026\nTEST270115C00100000,1/15/2027\n",
        )

    cache = SnapshotCache(redis, "account")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        first = FinvizProvider(client, cache, Limits(redis, cache.namespace), "secret")
        second = FinvizProvider(client, cache, Limits(redis, cache.namespace), "secret")
        raw = await first._get("/export/options", {"t": "TEST", "ty": "oc"})
        listed = await second.expirations("TEST")
        assert listed.snapshot.data["expirations"] == ["2027-01-15"]
        assert listed.snapshot.fetched_at == raw.fetched_at
        assert (await first.expirations("TEST")).cache_hit
        assert len(calls) == 1

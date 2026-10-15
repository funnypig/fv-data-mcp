from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

from fv_data_mcp.cache import SnapshotCache
from fv_data_mcp.cursors import Cursors
from fv_data_mcp.errors import ResearchError
from fv_data_mcp.limits import Limits
from fv_data_mcp.providers.articles import ArticleProvider, public_address, validate_url
from fv_data_mcp.providers.finviz import FinvizProvider
from fv_data_mcp.providers.parsing import number, parse_chain, parse_expirations
from fv_data_mcp.providers.rates import interpolate_rate, parse_curve
from fv_data_mcp.telemetry import JsonFormatter


def test_reference_chain_fixture() -> None:
    """Validate every row of the captured ordinary-contract chain without exclusions."""
    raw = (Path(__file__).parent / "fixtures/msft_chain.csv").read_text()
    chain = parse_chain(raw, "MSFT", date(2026, 1, 23))
    assert len(chain.contracts) == 184
    assert chain.rejected_rows == 0
    assert all(contract.multiplier == 100 for contract in chain.contracts)


def test_expirations_are_listed_not_guessed() -> None:
    """Extract an explicit listed expiration and fail on pages without recognizable dates."""
    assert parse_expirations(
        '<select name="e"><option value="2027-01-15">Jan</option></select>'
    ) == [date(2027, 1, 15)]
    with pytest.raises(ResearchError, match="recognizable"):
        parse_expirations("<html>No options data</html>")


@pytest.mark.parametrize("value", ["nan", "inf", "-", "N/A", None])
def test_missing_numbers(value: str | None) -> None:
    """Preserve missing or nonfinite provider numbers as unknown rather than zero."""
    assert number(value) is None


async def test_finviz_429_cooldown_no_secret_in_error(redis: FakeRedis) -> None:
    """Propagate safe throttling errors and activate a shared provider cooldown."""
    cache = SnapshotCache(redis, "account")
    limits = Limits(redis, cache.namespace)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "60"})
        )
    ) as client:
        provider = FinvizProvider(client, cache, limits, "top-secret")
        with pytest.raises(ResearchError) as caught:
            await provider.chain("TEST", date(2027, 1, 15))
        assert caught.value.code == "provider_rate_limited"
        assert "top-secret" not in str(caught.value)
        with pytest.raises(ResearchError):
            await limits.provider()


def test_cursor_binding_signature_and_expiry() -> None:
    """Reject tampered, query-mismatched, and expired pagination state."""
    cursors = Cursors("s" * 32)
    binding = {"ticker": "TEST"}
    token = cursors.encode(
        {"binding": binding, "offset": 100, "snapshot": "id", "expires": time.time() + 60}
    )
    assert cursors.decode(token, binding)["offset"] == 100
    for changed, parameters in [(token + "x", binding), (token, {"ticker": "OTHER"})]:
        with pytest.raises(ResearchError):
            cursors.decode(changed, parameters)
    expired = cursors.encode({"binding": binding, "offset": 0, "expires": 1})
    with pytest.raises(ResearchError, match="expired"):
        cursors.decode(expired, binding)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://127.0.0.1",
        "https://[::1]",
        "https://169.254.169.254/latest",
        "https://168.63.129.16",
        "https://user:pass@example.com",
        "https://example.com:444",
        "https://metadata.google.internal/",
    ],
)
def test_ssrf_rejected(url: str) -> None:
    """Reject unsafe protocols, destinations, credentials, and ports in article URLs."""
    with pytest.raises(ResearchError):
        validate_url(url, [])


def test_public_and_allowlist() -> None:
    """Apply publisher-domain boundaries and reject IPv4-mapped loopback addresses."""
    assert not public_address("::ffff:127.0.0.1")
    assert (
        validate_url("https://news.example.com/story#part", ["example.com"])
        == "https://news.example.com/story"
    )
    with pytest.raises(ResearchError):
        validate_url("https://notexample.com", ["example.com"])


async def test_article_registry_shared(redis: FakeRedis) -> None:
    """Reuse registered article IDs across instances and reject arbitrary URL input."""
    cache = SnapshotCache(redis, "account")
    first, second = ArticleProvider(cache, []), ArticleProvider(cache, [])
    identifier = await first.register("https://example.com/story")
    assert len(identifier) == 64
    assert identifier == await second.register("https://example.com/story")
    with pytest.raises(ResearchError):
        await second.fetch("https://example.com")
    await first.close()
    await second.close()


def test_treasury_parsing_and_interpolation() -> None:
    """Convert usable par yields and reject curves beyond the maximum observation age."""
    xml = "<feed><properties><NEW_DATE>2026-10-12T00:00:00</NEW_DATE><BC_3MONTH>4.0</BC_3MONTH><BC_1YEAR>4.5</BC_1YEAR></properties></feed>"
    curve = parse_curve(xml, date(2026, 10, 13))
    assert 0.039 < interpolate_rate(curve, 0.25) < 0.04
    with pytest.raises(ResearchError, match="seven days"):
        parse_curve(xml, date(2026, 10, 21))


def test_logs_redact_secrets_and_discard_exception_payload() -> None:
    """Remove configured and bearer credentials from serialized log messages."""
    record = logging.LogRecord(
        "fv", logging.ERROR, "", 0, "failed auth=private Bearer token secret-value", (), None
    )
    rendered = JsonFormatter(["secret-value"]).format(record)
    assert (
        "private" not in rendered
        and "Bearer token" not in rendered
        and "secret-value" not in rendered
    )
    assert json.loads(rendered)["level"] == "ERROR"

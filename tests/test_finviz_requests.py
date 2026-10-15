"""Verify request-boundary reuse and metered retries beneath dataset caches."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import NoReturn

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import ConnectionError

from fv_data_mcp.cache import SnapshotCache
from fv_data_mcp.errors import ResearchError
from fv_data_mcp.limits import Limits
from fv_data_mcp.providers.finviz import (
    MAX_CONCURRENT_REQUESTS,
    RAW_RESPONSE_FRESH_SECONDS,
    FinvizProvider,
    retry_after,
)


@pytest.mark.parametrize("initial_failure", [False, True])
async def test_direct_gets_coalesce_across_instances_and_reuse(
    redis: FakeRedis, initial_failure: bool
) -> None:
    """Deduplicate direct GET misses and hits without relying on any tool/dataset cache."""
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        """Count an overlapping authenticated GET and return a complete body."""
        nonlocal calls
        calls += 1
        assert request.url.params["auth"] == "private-key"
        await asyncio.sleep(0.02)
        if initial_failure and calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, text="shared body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        caches = [SnapshotCache(redis, "account") for _ in range(2)]
        providers = [
            FinvizProvider(client, cache, Limits(redis, cache.namespace), "private-key")
            for cache in caches
        ]
        results = await asyncio.gather(
            *(
                providers[index % 2]._get("/export/options", {"t": "TEST", "ty": "oc"})
                for index in range(20)
            )
        )
        reordered = await providers[1]._get("/export/options", {"ty": "oc", "t": "TEST"})
        assert calls == 1 + int(initial_failure)
        assert {result.snapshot_id for result in results} == {reordered.snapshot_id}
        assert reordered.data == {"body": "shared body"}
        assert await redis.zcard(f"{caches[0].namespace}:limit:finviz") == calls
        assert "private-key" not in " ".join(await redis.keys("*"))
        assert not await redis.keys("*:lock")


async def test_request_keys_partition_path_parameters_and_account(redis: FakeRedis) -> None:
    """Prevent unrelated endpoints, arguments, or provider accounts sharing responses."""
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        """Return a unique body for each actual network call."""
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=str(calls))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        first_cache, second_cache = SnapshotCache(redis, "first"), SnapshotCache(redis, "second")
        first = FinvizProvider(client, first_cache, Limits(redis, first_cache.namespace), "one")
        second = FinvizProvider(client, second_cache, Limits(redis, second_cache.namespace), "two")
        results = [
            await first._get("/quote.ashx", {"t": "TEST"}),
            await first._get("/quote.ashx", {"t": "OTHER"}),
            await first._get("/export/options", {"t": "TEST"}),
            await second._get("/quote.ashx", {"t": "TEST"}),
        ]
        assert calls == 4
        assert len({result.data["body"] for result in results}) == 4


async def test_rebuilding_dataset_preserves_source_age_and_expiry(redis: FakeRedis) -> None:
    """Reuse a raw body after dataset eviction without restarting source lifetime."""
    now = datetime(2026, 10, 15, tzinfo=UTC)
    cache = SnapshotCache(redis, "account", clock=lambda: now)
    calls = 0
    body = (Path(__file__).parent / "fixtures/msft_chain.csv").read_text()

    def respond(request: httpx.Request) -> httpx.Response:
        """Count chain HTTP requests while returning a representative fixture."""
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = FinvizProvider(client, cache, Limits(redis, cache.namespace), "key")
        first = await provider.chain("MSFT", date(2026, 1, 23))
        now += timedelta(seconds=RAW_RESPONSE_FRESH_SECONDS - 1)
        key = cache.key("chain", {"ticker": "MSFT", "expiration": "2026-01-23"})
        await redis.delete(key)
        rebuilt = await provider.chain("MSFT", date(2026, 1, 23))
        assert calls == 1
        assert rebuilt.snapshot.fetched_at == first.snapshot.fetched_at
        assert rebuilt.snapshot.fresh_until == first.snapshot.fresh_until
        assert rebuilt.snapshot.expires_at == first.snapshot.expires_at
        assert await redis.ttl(key) <= 601
        now += timedelta(seconds=2)
        refreshed = await provider.chain("MSFT", date(2026, 1, 23))
        assert calls == 2
        assert refreshed.snapshot.fetched_at == now


class TrackedStream(httpx.AsyncByteStream):
    """Record stream cleanup before replay begins."""

    closed: bool

    def __init__(self) -> None:
        """Initialize an open response stream."""
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield a small complete response body."""
        yield b"body"

    async def aclose(self) -> None:
        """Record that the HTTP response released its stream."""
        self.closed = True


async def test_transient_retry_releases_resources_and_meters_attempts(
    redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close streams and free capacity before retry waits; meter each wire attempt."""
    cache = SnapshotCache(redis, "account")
    streams: list[TrackedStream] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Fail once and succeed on the second metered request."""
        stream = TrackedStream()
        streams.append(stream)
        return httpx.Response(503 if len(streams) == 1 else 200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = FinvizProvider(client, cache, Limits(redis, cache.namespace), "key")

        def jitter(lower: float, upper: float) -> float:
            """Check resource release at the start of retry backoff."""
            assert streams[0].closed
            assert provider.concurrent._value == MAX_CONCURRENT_REQUESTS
            return 0

        monkeypatch.setattr("fv_data_mcp.retry.random.uniform", jitter)
        result = await provider._get("/quote.ashx", {"t": "TEST"})
        assert result.data["body"] == "body"
        assert len(streams) == 2
        assert all(stream.closed for stream in streams)
        assert await redis.zcard(f"{cache.namespace}:limit:finviz") == 2
        assert (await provider._get("/quote.ashx", {"t": "TEST"})).snapshot_id == result.snapshot_id
        assert len(streams) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 302])
async def test_permanent_http_failures_never_retry_or_cache_success(
    redis: FakeRedis, status: int
) -> None:
    """Do not replay permanent failures or store them as successful HTTP snapshots."""
    cache = SnapshotCache(redis, "account")
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        """Count requests returning the selected permanent status."""
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"Location": "https://example.com"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        provider = FinvizProvider(client, cache, Limits(redis, cache.namespace), "key")
        with pytest.raises(ResearchError):
            await provider._get("/quote.ashx", {"t": "TEST"})
        assert calls == 1
        assert not await redis.keys("*:snapshot:*")
        assert not await redis.keys("*:lock")


@pytest.mark.parametrize("status", [429, 503])
async def test_long_retry_after_is_shared_and_not_retried(redis: FakeRedis, status: int) -> None:
    """Preserve long server advice and publish an account-wide cooldown immediately."""
    cache = SnapshotCache(redis, "account")
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        """Return throttling or unavailability with a server-directed delay."""
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"Retry-After": "7200"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = FinvizProvider(client, cache, Limits(redis, cache.namespace), "key")
        with pytest.raises(ResearchError) as caught:
            await provider._get("/quote.ashx", {"t": "TEST"})
        assert caught.value.retry_after_seconds == 7200
        assert calls == 1
        assert await redis.ttl(f"{cache.namespace}:limit:finviz:cooldown") > 7100
        with pytest.raises(ResearchError):
            await provider._get("/quote.ashx", {"t": "OTHER"})
        assert calls == 1


async def test_short_retry_after_waits_then_reserves_again(redis: FakeRedis) -> None:
    """Wait for an allowed server cooldown before the second quota reservation."""
    cache = SnapshotCache(redis, "account")
    timestamps: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Throttle once, then return data after the retry delay."""
        timestamps.append(time.monotonic())
        return httpx.Response(429 if len(timestamps) == 1 else 200, headers={"Retry-After": "1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = FinvizProvider(client, cache, Limits(redis, cache.namespace), "key")
        await provider._get("/quote.ashx", {})
        assert len(timestamps) == 2
        assert timestamps[1] - timestamps[0] >= 1
        assert await redis.zcard(f"{cache.namespace}:limit:finviz") == 2


async def test_retry_exhaustion_is_bounded_and_negative_cached(
    redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spend at most two requests and suppress immediate repeats of the same failure."""
    cache = SnapshotCache(redis, "account")
    calls = 0

    def respond(request: httpx.Request) -> NoReturn:
        """Raise a transport error containing text that must not escape to clients."""
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("private-key", request=request)

    monkeypatch.setattr("fv_data_mcp.retry.random.uniform", lambda lower, upper: 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = FinvizProvider(client, cache, Limits(redis, cache.namespace), "private-key")
        for _ in range(2):
            with pytest.raises(ResearchError) as caught:
                await provider._get("/quote.ashx", {})
            assert caught.value.code == "provider_unavailable"
            assert "private-key" not in str(caught.value)
        assert calls == 2
        assert not await redis.keys("*:snapshot:*")


async def test_oversized_response_is_not_retried_or_cached(redis: FakeRedis) -> None:
    """Reject oversized complete responses without retrying or caching their data."""
    cache = SnapshotCache(redis, "account")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="oversized"))
    ) as client:
        provider = FinvizProvider(client, cache, Limits(redis, cache.namespace), "key", max_bytes=1)
        with pytest.raises(ResearchError) as caught:
            await provider._get("/quote.ashx", {})
        assert caught.value.code == "response_too_large"
        assert await redis.zcard(f"{cache.namespace}:limit:finviz") == 1
        assert not await redis.keys("*:snapshot:*")


async def test_direct_get_fails_closed_on_redis_failure(
    redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never bypass request caching when its distributed coordination is unavailable."""

    async def broken(*args: object, **kwargs: object) -> NoReturn:
        """Simulate an unavailable Redis connection."""
        raise ConnectionError("offline")

    def respond(request: httpx.Request) -> NoReturn:
        """Fail if a request bypasses shared coordination."""
        pytest.fail("Uncoordinated upstream request")

    monkeypatch.setattr(redis, "get", broken)
    cache = SnapshotCache(redis, "account")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = FinvizProvider(client, cache, Limits(redis, cache.namespace), "key")
        with pytest.raises(ResearchError, match="coordination"):
            await provider._get("/quote.ashx", {})


async def test_cancelled_request_releases_lock_and_capacity(redis: FakeRedis) -> None:
    """Cancel in-flight I/O without replay, publishing partial data, or leaking locks."""
    cache = SnapshotCache(redis, "account")
    started = asyncio.Event()
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        """Block the first call until cancelled, allowing the next caller to succeed."""
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()
        return httpx.Response(200, text="recovered")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = FinvizProvider(client, cache, Limits(redis, cache.namespace), "key")
        task = asyncio.create_task(provider._get("/quote.ashx", {}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not await redis.keys("*:lock")
        assert not await redis.keys("*:snapshot:*")
        assert provider.concurrent._value == MAX_CONCURRENT_REQUESTS
        assert (await provider._get("/quote.ashx", {})).data["body"] == "recovered"
        assert calls == 2


@pytest.mark.parametrize("value", [None, "bad", "nan", "inf", "-inf"])
def test_invalid_retry_after_uses_safe_default(value: str | None) -> None:
    """Handle malformed or nonfinite upstream retry advice safely."""
    assert retry_after(value) == 5


def test_retry_after_http_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interpret an HTTP date relative to UTC without shortening a long delay."""
    now = datetime(2026, 10, 15, tzinfo=UTC)
    monkeypatch.setattr("fv_data_mcp.providers.finviz.utc_now", lambda: now)
    assert retry_after(format_datetime(now + timedelta(seconds=120), usegmt=True)) == 120

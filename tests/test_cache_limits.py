from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import NoReturn

import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import ConnectionError

from fv_data_mcp.cache import Payload, SnapshotCache
from fv_data_mcp.errors import ResearchError
from fv_data_mcp.limits import Limits


async def test_hundred_misses_across_instances_only_fetch_once(redis: FakeRedis) -> None:
    """Coalesce a hundred concurrent source misses across cache instances into one fetch."""
    caches = [SnapshotCache(redis, "account") for _ in range(2)]
    fetches = 0

    async def load() -> Payload:
        """Count a deliberately overlapping provider fetch before returning test data."""
        nonlocal fetches
        fetches += 1
        await asyncio.sleep(0.02)
        return Payload({"value": 42})

    results = await asyncio.gather(
        *(caches[index % 2].fetch("chain", {"ticker": "TEST"}, load) for index in range(100))
    )
    assert fetches == 1
    assert len({result.snapshot.snapshot_id for result in results}) == 1
    assert sum(not result.cache_hit for result in results) == 1


async def test_stale_only_within_absolute_retention(redis: FakeRedis) -> None:
    """Allow bounded transient-error fallback but reject hard-expired source data."""
    now = datetime(2026, 10, 13, tzinfo=UTC)
    cache = SnapshotCache(redis, "account", clock=lambda: now)

    async def load() -> Payload:
        """Return the source data used to establish the initial cached snapshot."""
        return Payload({"value": 42})

    async def fail() -> NoReturn:
        """Simulate a retryable provider outage without returning data."""
        raise ResearchError("provider_unavailable", "offline", True)

    first = await cache.fetch("chain", {}, load)
    now += timedelta(seconds=301)
    stale = await cache.fetch("chain", {}, fail)
    assert stale.refresh_error == "provider_unavailable"
    assert stale.snapshot.snapshot_id == first.snapshot.snapshot_id
    now += timedelta(seconds=600)
    with pytest.raises(ResearchError, match="offline"):
        await cache.fetch("chain", {}, fail)


async def test_redis_failure_never_calls_provider(
    redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed on cache failure instead of bypassing quota coordination."""

    async def broken(*args: object, **kwargs: object) -> NoReturn:
        """Simulate a Redis connection failure containing sensitive diagnostic text."""
        raise ConnectionError("secret-connection-string")

    monkeypatch.setattr(redis, "get", broken)

    async def load() -> NoReturn:
        """Fail the test if an uncoordinated provider fetch is attempted."""
        pytest.fail("Must not fetch without cache/rate coordination")

    with pytest.raises(ResearchError, match="coordination"):
        await SnapshotCache(redis, "account").fetch("chain", {}, load)


async def test_provider_rolling_limit_shared(redis: FakeRedis) -> None:
    """Enforce one shared rolling-window budget across independent limiter instances."""
    limits = [Limits(redis, "test") for _ in range(2)]
    results = await asyncio.gather(
        *(limits[index % 2].reserve("finviz", 8, 5) for index in range(20)), return_exceptions=True
    )
    assert sum(result is None for result in results) == 8
    assert sum(isinstance(result, ResearchError) for result in results) == 12


async def test_cooldown_and_user_concurrency(redis: FakeRedis) -> None:
    """Enforce shared provider cooldown and release user slots after context exit."""
    limits = Limits(redis, "test")
    await limits.cooldown(60)
    with pytest.raises(ResearchError):
        await limits.provider()
    async with limits.user("user", 30, 1):
        with pytest.raises(ResearchError, match="concurrent"):
            async with limits.user("user", 30, 1):
                pytest.fail("Concurrent call allowed")
    async with limits.user("user", 30, 1):
        pass


async def test_calculated_retention_cannot_outlive_inputs(redis: FakeRedis) -> None:
    """Bound derived-cache TTL by the input snapshot's remaining lifetime."""
    cache = SnapshotCache(redis, "account")

    async def load() -> Payload:
        """Provide a short-lived snapshot for derived-retention assertions."""
        return Payload({"value": 42})

    source = await cache.fetch("source", {}, load, 5, 10)
    await cache.calculated("test", [source.snapshot.snapshot_id], {}, lambda: {"answer": 42})
    keys = await redis.keys(cache.namespace + ":calculation:*")
    assert 0 < await redis.ttl(keys[0]) <= 10


async def test_lost_lock_owner_cannot_publish(redis: FakeRedis) -> None:
    """Prevent a replaced refresher from publishing or releasing its successor's lock."""
    cache = SnapshotCache(redis, "account")
    key = cache.key("source", {})

    async def load() -> Payload:
        """Replace refresh ownership before returning a deliberately late provider result."""
        await redis.set(key + ":lock", "new-owner", ex=35)
        return Payload({"value": "late-result"})

    with pytest.raises(ResearchError, match="ownership expired"):
        await cache.fetch("source", {}, load)
    assert await redis.get(key) is None
    assert await redis.get(key + ":lock") == "new-owner"


async def test_cancellation_releases_fetch_lease(redis: FakeRedis) -> None:
    """Release the source refresh lock when its owning task is cancelled."""
    cache = SnapshotCache(redis, "account")
    started = asyncio.Event()

    async def load() -> None:
        """Signal entry and wait long enough for the owning task to be cancelled."""
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(cache.fetch("source", {}, load))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await redis.exists(cache.key("source", {}) + ":lock")


@pytest.mark.parametrize("age_seconds", [-1, 900, 901])
async def test_reused_source_must_have_usable_timestamp(redis: FakeRedis, age_seconds: int) -> None:
    """Reject future or hard-expired inputs rather than minting fresh snapshots."""
    now = datetime(2026, 10, 15, tzinfo=UTC)
    cache = SnapshotCache(redis, "account", clock=lambda: now)

    async def load() -> Payload:
        """Return cached input with an unusable original fetch timestamp."""
        return Payload({"value": 42}, fetched_at=now - timedelta(seconds=age_seconds))

    with pytest.raises(ResearchError) as caught:
        await cache.fetch("source", {}, load)
    assert caught.value.code == "snapshot_expired"
    assert not await redis.keys("*:snapshot:*")
    assert not await redis.keys("*:lock")


async def test_complete_loader_deadline_releases_lease(
    redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Include nested cache waits in the refresh deadline and clean up on timeout."""
    monkeypatch.setattr("fv_data_mcp.cache.SOURCE_LOAD_TIMEOUT_SECONDS", 0.01)
    cache = SnapshotCache(redis, "account")

    async def load() -> Payload:
        """Wait beyond the source budget without producing cacheable data."""
        await asyncio.Event().wait()
        return Payload({"unused": True})

    with pytest.raises(ResearchError) as caught:
        await cache.fetch("source", {}, load)
    assert caught.value.code == "provider_unavailable"
    assert not await redis.keys("*:lock")
    assert not await redis.keys("*:snapshot:*")

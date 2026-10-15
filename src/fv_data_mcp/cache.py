from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from fv_data_mcp.errors import ResearchError, unavailable
from fv_data_mcp.models import CachedResult, Snapshot, utc_now

logger = logging.getLogger(__name__)

SNAPSHOT_FRESH_SECONDS = 300
SNAPSHOT_RETAIN_SECONDS = 900
SOURCE_WAIT_TIMEOUT_SECONDS = 25
SOURCE_LOAD_TIMEOUT_SECONDS = 30
SOURCE_LOCK_LEASE_SECONDS = 35
SOURCE_POLL_INTERVAL_SECONDS = 0.1
REFRESH_RETRY_SECONDS = 2
TRANSIENT_FAILURE_SECONDS = 5
NOT_FOUND_FAILURE_SECONDS = 30
MAX_ENCODED_SNAPSHOT_BYTES = 10_000_000

RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

PUBLISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
redis.call('SET', KEYS[3], ARGV[2], 'EX', ARGV[3])
redis.call('DEL', KEYS[1])
return 1
"""


@dataclass
class Payload:
    """Carry normalized provider data and its provenance into the shared cache."""

    data: dict[str, Any]
    provider: str = "finviz"
    source_as_of: datetime | None = None
    delay_seconds: int | None = None
    warnings: list[str] = field(default_factory=list)
    fetched_at: datetime | None = None


class SnapshotCache:
    """Coordinate shared source snapshots and bounded derived-result caching."""

    redis: Redis
    namespace: str
    clock: Callable[[], datetime]

    def __init__(self, redis: Redis, account: str, clock: Callable[[], datetime] = utc_now) -> None:
        """Bind Redis, an account-scoped namespace, and an injectable UTC clock."""
        self.redis = redis
        self.namespace = "fv:v1:{" + hashlib.sha256(account.encode()).hexdigest()[:20] + "}"
        self.clock = clock

    def key(self, dataset: str, parameters: dict[str, Any]) -> str:
        """Hash canonical query parameters into an account-scoped dataset key."""
        encoded = json.dumps(parameters, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        return f"{self.namespace}:{dataset}:{digest}"

    async def by_id(self, snapshot_id: str) -> Snapshot:
        """Load an immutable snapshot, rejecting missing or hard-expired identifiers."""
        try:
            uuid.UUID(snapshot_id)
        except ValueError as exc:
            raise ResearchError("invalid_argument", "Invalid snapshot identifier.") from exc
        try:
            raw = await self.redis.get(f"{self.namespace}:snapshot:{snapshot_id}")
        except RedisError as exc:
            raise unavailable() from exc
        if raw is None:
            raise ResearchError("snapshot_expired", "Snapshot expired. Start a new query.")
        snapshot = Snapshot.model_validate_json(raw)
        if self.clock() >= snapshot.expires_at:
            raise ResearchError("snapshot_expired", "Snapshot expired. Start a new query.")
        return snapshot

    async def fetch(
        self,
        dataset: str,
        parameters: dict[str, Any],
        loader: Callable[[], Awaitable[Payload]],
        fresh_seconds: int = SNAPSHOT_FRESH_SECONDS,
        retain_seconds: int = SNAPSHOT_RETAIN_SECONDS,
    ) -> CachedResult:
        """Fetch through the shared cache with a single token-owned refresher.

        Args:
            dataset: Internal dataset name used for cache partitioning.
            parameters: Canonical source-query parameters, never credentials.
            loader: Asynchronous provider fetch performed only by the lock owner.
                A payload reusing cached input must preserve its original fetched_at.
            fresh_seconds: Fresh lifetime measured from a successful fetch.
            retain_seconds: Absolute maximum lifetime, including stale fallback.

        Returns:
            The source snapshot with cache-hit and optional refresh-failure details.

        Raises:
            ResearchError: Coordination or retrieval fails without a usable snapshot.
            ValueError: Retention is shorter than the fresh window.
        """
        if retain_seconds < fresh_seconds:
            raise ValueError("Retention must cover freshness")
        cache_key = self.key(dataset, parameters)
        lock_key = cache_key + ":lock"
        failure_key = cache_key + ":failure"
        owner = uuid.uuid4().hex
        deadline = time.monotonic() + SOURCE_WAIT_TIMEOUT_SECONDS
        try:
            while True:
                raw = await self.redis.get(cache_key)
                cached = Snapshot.model_validate_json(raw) if raw else None
                now = self.clock()
                if cached and now < cached.fresh_until:
                    return CachedResult(snapshot=cached, cache_hit=True)
                if cached and now >= cached.expires_at:
                    cached = None
                failure = await self.redis.get(failure_key)
                if failure:
                    details = json.loads(failure)
                    error = ResearchError(**details)
                    if cached and error.retryable:
                        return CachedResult(
                            snapshot=cached, cache_hit=True, refresh_error=error.code
                        )
                    raise error
                if await self.redis.set(lock_key, owner, nx=True, ex=SOURCE_LOCK_LEASE_SECONDS):
                    break
                if time.monotonic() >= deadline:
                    if cached:
                        return CachedResult(
                            snapshot=cached, cache_hit=True, refresh_error="refresh_busy"
                        )
                    raise ResearchError(
                        "refresh_busy",
                        "This dataset is being refreshed. Retry shortly.",
                        True,
                        REFRESH_RETRY_SECONDS,
                    )
                await asyncio.sleep(SOURCE_POLL_INTERVAL_SECONDS)

            try:
                raw = await self.redis.get(cache_key)
                latest = Snapshot.model_validate_json(raw) if raw else None
                if latest and self.clock() < latest.fresh_until:
                    return CachedResult(snapshot=latest, cache_hit=True)
                if latest and self.clock() < latest.expires_at:
                    cached = latest
                payload = await self._load(loader)
                now = self.clock()
                fetched_at = payload.fetched_at or now
                expires_at = fetched_at + timedelta(seconds=retain_seconds)
                if fetched_at > now or expires_at <= now:
                    raise ResearchError(
                        "snapshot_expired", "Source timestamp is outside the usable cache window."
                    )
                snapshot = Snapshot(
                    snapshot_id=str(uuid.uuid4()),
                    provider=payload.provider,
                    fetched_at=fetched_at,
                    fresh_until=fetched_at + timedelta(seconds=fresh_seconds),
                    expires_at=expires_at,
                    data=payload.data,
                    source_as_of=payload.source_as_of,
                    source_delay_seconds=payload.delay_seconds,
                    warnings=payload.warnings,
                )
                encoded = snapshot.model_dump_json()
                if len(encoded.encode()) > MAX_ENCODED_SNAPSHOT_BYTES:
                    raise ResearchError(
                        "response_too_large", "Provider dataset exceeds the snapshot limit."
                    )
                published = await self.redis.eval(
                    PUBLISH,
                    3,
                    lock_key,
                    cache_key,
                    f"{self.namespace}:snapshot:{snapshot.snapshot_id}",
                    owner,
                    encoded,
                    math.ceil((expires_at - now).total_seconds()),
                )
                if not published:
                    raise ResearchError(
                        "refresh_busy",
                        "Refresh ownership expired. Retry shortly.",
                        True,
                        REFRESH_RETRY_SECONDS,
                    )
                return CachedResult(snapshot=snapshot, cache_hit=False)
            except ResearchError as exc:
                failure = {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "retry_after_seconds": exc.retry_after_seconds,
                }
                if exc.retryable or exc.code == "not_found":
                    await self.redis.set(
                        failure_key,
                        json.dumps(failure),
                        ex=TRANSIENT_FAILURE_SECONDS
                        if exc.retryable
                        else NOT_FOUND_FAILURE_SECONDS,
                    )
                if cached and exc.retryable and self.clock() < cached.expires_at:
                    return CachedResult(snapshot=cached, cache_hit=True, refresh_error=exc.code)
                raise
            finally:
                await self.redis.eval(RELEASE_LOCK, 1, lock_key, owner)
        except RedisError as exc:
            logger.warning("cache_unavailable", extra={"dataset": dataset})
            raise unavailable(
                "Cache coordination is unavailable; no unmetered provider requests are allowed."
            ) from exc

    async def _load(self, loader: Callable[[], Awaitable[Payload]]) -> Payload:
        """Bound a complete loader, including nested cache waits, below the lock lease."""
        try:
            async with asyncio.timeout(SOURCE_LOAD_TIMEOUT_SECONDS):
                return await loader()
        except TimeoutError:
            raise ResearchError(
                "provider_unavailable",
                "Source refresh exceeded its time budget.",
                True,
                TRANSIENT_FAILURE_SECONDS,
            ) from None

    async def calculated(
        self,
        method: str,
        snapshot_ids: list[str],
        parameters: dict[str, Any],
        compute: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Cache a calculation by method, source IDs, and parameters.

        Returns:
            A cached or newly computed JSON-compatible result that cannot outlive
            its input snapshots.

        Raises:
            ResearchError: An input snapshot expires or Redis is unavailable.
        """
        key = self.key("calculation", {"method": method, "snapshots": snapshot_ids, **parameters})
        snapshots = [await self.by_id(snapshot_id) for snapshot_id in snapshot_ids]
        remaining = int(min((item.expires_at - self.clock()).total_seconds() for item in snapshots))
        if remaining <= 0:
            raise ResearchError("snapshot_expired", "Input snapshot expired. Start a new query.")
        try:
            encoded = await self.redis.get(key)
            if encoded is not None:
                return json.loads(encoded)
            result = await asyncio.to_thread(compute)
            remaining = int(
                min((item.expires_at - self.clock()).total_seconds() for item in snapshots)
            )
            if remaining <= 0:
                raise ResearchError(
                    "snapshot_expired", "Input snapshot expired during calculation."
                )
            await self.redis.set(key, json.dumps(result, allow_nan=False), ex=remaining)
            return result
        except RedisError as exc:
            raise unavailable() from exc

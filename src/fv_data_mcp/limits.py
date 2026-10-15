from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from redis.exceptions import RedisError

from fv_data_mcp.errors import ResearchError, unavailable

WINDOW = """
local stamp = redis.call('TIME')
local now = stamp[1] * 1000 + math.floor(stamp[2] / 1000)
local cooldown = redis.call('PTTL', KEYS[2])
if cooldown > 0 then return cooldown end
local window = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
    local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    return math.max(1, tonumber(oldest[2]) + window - now)
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], window + 1000)
return 0
"""

COOLDOWN = """
local remaining = redis.call('PTTL', KEYS[1])
if remaining < tonumber(ARGV[1]) then
    redis.call('SET', KEYS[1], '1', 'PX', ARGV[1])
end
return 1
"""

CONCURRENCY = """
local stamp = redis.call('TIME')
local now = stamp[1] * 1000 + math.floor(stamp[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]) + 1000)
return 1
"""


class Limits:
    """Coordinate account quotas, cooldowns, and user concurrency through Redis."""

    redis: Redis
    namespace: str
    provider_limit: int
    window_seconds: int

    def __init__(
        self, redis: Redis, namespace: str, provider_limit: int = 8, window_seconds: int = 5
    ) -> None:
        """Configure the shared namespace and conservative provider request budget."""
        self.redis = redis
        self.namespace = namespace
        self.provider_limit = provider_limit
        self.window_seconds = window_seconds

    async def reserve(
        self, name: str, limit: int, window_seconds: int, wait_seconds: float = 0
    ) -> None:
        """Reserve one rolling-window request slot, waiting only within the given budget."""
        key = f"{self.namespace}:limit:{name}"
        deadline = time.monotonic() + wait_seconds
        try:
            while True:
                delay_ms = int(
                    await self.redis.eval(
                        WINDOW,
                        2,
                        key,
                        key + ":cooldown",
                        window_seconds * 1000,
                        limit,
                        uuid.uuid4().hex,
                    )
                )
                if delay_ms == 0:
                    return
                if time.monotonic() + delay_ms / 1000 > deadline:
                    raise ResearchError(
                        "rate_limited",
                        "Request budget is currently exhausted.",
                        True,
                        round(delay_ms / 1000, 3),
                    )
                await asyncio.sleep(delay_ms / 1000 + 0.02)
        except RedisError as exc:
            raise unavailable("Rate-limit coordination is unavailable.") from exc

    async def provider(self) -> None:
        """Reserve a Finviz request slot with a bounded wait."""
        await self.reserve("finviz", self.provider_limit, self.window_seconds, wait_seconds=6)

    async def cooldown(self, seconds: float) -> None:
        """Extend the account-wide Finviz cooldown without shortening an existing delay."""
        try:
            await self.redis.eval(
                COOLDOWN,
                1,
                f"{self.namespace}:limit:finviz:cooldown",
                max(1000, int(seconds * 1000)),
            )
        except RedisError as exc:
            raise unavailable() from exc

    @asynccontextmanager
    async def user(self, subject: str, per_minute: int, concurrency: int) -> AsyncIterator[None]:
        """Enforce user request rate and hold a bounded concurrency lease.

        Yields:
            Control while the caller owns a concurrency slot.

        Raises:
            ResearchError: User limits are exhausted or coordination is unavailable.
        """
        identity = hashlib.sha256(subject.encode()).hexdigest()
        await self.reserve(f"user:{identity}", per_minute, 60)
        key = f"{self.namespace}:concurrency:{identity}"
        owner = uuid.uuid4().hex
        try:
            acquired = await self.redis.eval(CONCURRENCY, 1, key, concurrency, 65000, owner)
            if not acquired:
                raise ResearchError(
                    "rate_limited", "Too many concurrent requests for this account.", True, 2
                )
            try:
                yield
            finally:
                await self.redis.zrem(key, owner)
        except RedisError as exc:
            raise unavailable() from exc

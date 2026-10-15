"""Bound retries for explicitly replay-safe asynchronous operations."""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import ParamSpec, TypeVar

Parameters = ParamSpec("Parameters")
Result = TypeVar("Result")


@dataclass(frozen=True)
class RetryPolicy:
    """Define attempt, full-jitter backoff, server-delay, and total-time budgets."""

    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    total_seconds: float

    def __post_init__(self) -> None:
        """Reject unbounded or inconsistent retry budgets at construction time."""
        durations = (self.base_delay_seconds, self.max_delay_seconds, self.total_seconds)
        if (
            self.max_attempts < 1
            or not all(math.isfinite(value) and value > 0 for value in durations)
            or self.base_delay_seconds > self.max_delay_seconds
        ):
            raise ValueError("Retry budgets must be finite, positive, and ordered")


@dataclass
class RetryableFailure(Exception):
    """Opt one transient failure into retry, preserving a safe terminal exception."""

    error: Exception
    retry_after_seconds: float | None = None


def retry_async(
    policy: RetryPolicy,
) -> Callable[[Callable[Parameters, Awaitable[Result]]], Callable[Parameters, Awaitable[Result]]]:
    """Decorate a replay-safe operation with one bounded asynchronous retry policy.

    Only RetryableFailure opts into replay. Other errors and cancellation propagate.
    The operation must release responses and concurrency slots before raising. Server
    delays are honored without jitter; delays beyond the policy are not shortened.
    The total budget includes attempts and sleeps; expiry raises TimeoutError.
    """

    def decorate(
        operation: Callable[Parameters, Awaitable[Result]],
    ) -> Callable[Parameters, Awaitable[Result]]:
        """Wrap an asynchronous operation while preserving its signature metadata."""

        @wraps(operation)
        async def wrapped(*args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
            """Run bounded attempts, sleeping only after the failed operation unwinds."""
            async with asyncio.timeout(policy.total_seconds):
                for attempt in range(policy.max_attempts):
                    try:
                        return await operation(*args, **kwargs)
                    except RetryableFailure as failure:
                        if attempt + 1 == policy.max_attempts:
                            raise failure.error from None
                        delay = failure.retry_after_seconds
                        if delay is None:
                            delay = random.uniform(
                                0,
                                min(
                                    policy.max_delay_seconds, policy.base_delay_seconds * 2**attempt
                                ),
                            )
                        if not math.isfinite(delay) or not 0 <= delay <= policy.max_delay_seconds:
                            raise failure.error from None
                        await asyncio.sleep(delay)
            raise RuntimeError("Retry policy exhausted without a result")

        return wrapped

    return decorate

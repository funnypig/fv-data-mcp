"""Exercise retry policy bounds independently of any provider."""

from __future__ import annotations

import asyncio
import inspect
from typing import NoReturn

import pytest

from fv_data_mcp.retry import RetryableFailure, RetryPolicy, retry_async


async def test_retry_backoff_attempt_bound_and_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve callable contracts and use bounded exponential delays before exhaustion."""
    delays: list[float] = []
    attempts = 0

    async def sleep(delay: float) -> None:
        """Record delays without consuming real wall-clock time."""
        delays.append(delay)

    monkeypatch.setattr("fv_data_mcp.retry.asyncio.sleep", sleep)
    monkeypatch.setattr("fv_data_mcp.retry.random.uniform", lambda lower, upper: upper)

    @retry_async(RetryPolicy(4, 0.1, 0.3, 5))
    async def operation(value: str) -> str:
        """Fail with a safe exception eligible for bounded replay."""
        nonlocal attempts
        attempts += 1
        raise RetryableFailure(ValueError(value))

    with pytest.raises(ValueError, match="safe"):
        await operation("safe")
    assert attempts == 4
    assert delays == [0.1, 0.2, 0.3]
    assert operation.__name__ == "operation"
    assert operation.__doc__ == "Fail with a safe exception eligible for bounded replay."
    assert list(inspect.signature(operation).parameters) == ["value"]


@pytest.mark.parametrize("delay", [2.0, 10.0, float("nan"), float("inf"), -1.0])
async def test_server_delay_is_honored_or_not_retried(
    delay: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Honor supported server delays exactly and never shorten unsupported delays."""
    delays: list[float] = []
    attempts = 0

    async def sleep(seconds: float) -> None:
        """Capture the selected server-directed wait."""
        delays.append(seconds)

    monkeypatch.setattr("fv_data_mcp.retry.asyncio.sleep", sleep)

    @retry_async(RetryPolicy(2, 0.1, 5, 10))
    async def operation() -> str:
        """Return success only if replay is permitted by the delay budget."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryableFailure(ValueError("retry advice"), delay)
        return "ok"

    if delay == 2:
        assert await operation() == "ok"
        assert delays == [2]
        assert attempts == 2
    else:
        with pytest.raises(ValueError, match="retry advice"):
            await operation()
        assert not delays
        assert attempts == 1


@pytest.mark.parametrize("error", [ValueError("invalid"), asyncio.CancelledError()])
async def test_unmarked_errors_and_cancellation_are_not_retried(error: BaseException) -> None:
    """Propagate permanent failures and task cancellation immediately."""
    attempts = 0

    @retry_async(RetryPolicy(2, 0.1, 5, 10))
    async def operation() -> NoReturn:
        """Raise a failure that has not opted into replay."""
        nonlocal attempts
        attempts += 1
        raise error

    with pytest.raises(type(error)):
        await operation()
    assert attempts == 1


async def test_total_budget_bounds_waits() -> None:
    """Cancel a retry sleep when the operation's total deadline expires."""
    attempts = 0

    @retry_async(RetryPolicy(2, 1, 5, 0.01))
    async def operation() -> NoReturn:
        """Request a delay longer than the remaining operation budget."""
        nonlocal attempts
        attempts += 1
        raise RetryableFailure(ValueError("offline"), 1)

    with pytest.raises(TimeoutError):
        await operation()
    assert attempts == 1


@pytest.mark.parametrize(
    ("attempts", "base", "maximum", "total"),
    [(0, 1, 2, 3), (1, 0, 2, 3), (1, 3, 2, 3), (1, 1, 2, float("inf"))],
)
def test_invalid_retry_policy(attempts: int, base: float, maximum: float, total: float) -> None:
    """Reject invalid retry policies before they can reach a network operation."""
    with pytest.raises(ValueError):
        RetryPolicy(attempts, base, maximum, total)

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import fakeredis.aioredis
import pytest
from fakeredis.aioredis import FakeRedis

from fv_data_mcp.config import Settings
from fv_data_mcp.models import Contract


@pytest.fixture
def redis() -> FakeRedis:
    """Provide an isolated in-memory Redis implementation with Lua support."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def settings() -> Settings:
    """Provide safe local-only settings without loading environment files."""
    return Settings(
        _env_file=None,
        environment="test",
        dev_auth_enabled=True,
        dev_token="local-token-for-tests-123456",
        cursor_secret="s" * 32,
    )


@pytest.fixture
def contract() -> Callable[..., Contract]:
    """Provide a factory for validated option contracts with overridable market fields."""

    def make(**overrides: Any) -> Contract:
        """Build a standard option contract, applying caller-supplied field overrides."""
        values = dict(
            symbol="TEST270115C00100000",
            expiration=date(2027, 1, 15),
            side="call",
            strike=100,
            multiplier=100,
            standard=True,
            bid=1.0,
            ask=1.1,
            open_interest=100,
            volume=50,
            iv=0.3,
            delta=0.5,
            gamma=0.02,
        )
        return Contract(**(values | overrides))

    return make

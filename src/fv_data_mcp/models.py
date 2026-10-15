from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from fv_data_mcp.errors import ResearchError


def normalize_ticker(value: Any) -> str:
    """Normalize and validate an untrusted US equity/ETF symbol."""
    if not isinstance(value, str):
        raise ValueError("Ticker must be a string")
    normalized = value.strip().upper().removeprefix("$")
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", normalized):
        raise ValueError("Use a US stock or ETF symbol, for example AAPL or BRK-B")
    return normalized


Ticker = Annotated[str, BeforeValidator(normalize_ticker)]
Side = Literal["call", "put"]


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class Contract(BaseModel):
    """Represent a validated option row with explicit missing market-data fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(allow_inf_nan=False, frozen=True)

    symbol: str
    expiration: date
    side: Side
    strike: float = Field(gt=0)
    multiplier: int | None = Field(default=None, gt=0)
    standard: bool = True
    bid: float | None = Field(default=None, ge=0)
    ask: float | None = Field(default=None, ge=0)
    last: float | None = Field(default=None, ge=0)
    last_trade: str | None = None
    open_interest: int | None = Field(default=None, ge=0)
    volume: int | None = Field(default=None, ge=0)
    iv: float | None = Field(default=None, ge=0)
    delta: float | None = Field(default=None, ge=-1, le=1)
    gamma: float | None = Field(default=None, ge=0)
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None


class Chain(BaseModel):
    """Group contracts for one ticker/expiration with provider-rejection diagnostics."""

    ticker: str
    expiration: date
    contracts: list[Contract]
    rejected_rows: int = 0
    warnings: list[str] = Field(default_factory=list)


class Snapshot(BaseModel):
    """Preserve immutable source data and its fresh and hard-expiration timestamps."""

    snapshot_id: str
    provider: str
    fetched_at: datetime
    fresh_until: datetime
    expires_at: datetime
    source_as_of: datetime | None = None
    source_delay_seconds: int | None = None
    data: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)

    def provenance(self, now: datetime | None = None, cache_hit: bool = False) -> dict[str, Any]:
        """Describe source timing, cache age, and staleness without exposing cached data."""
        current = now or utc_now()
        return {
            "snapshot_id": self.snapshot_id,
            "provider": self.provider,
            "fetched_at": self.fetched_at.isoformat(),
            "source_as_of": self.source_as_of.isoformat() if self.source_as_of else None,
            "source_delay_seconds": self.source_delay_seconds,
            "cache_age_seconds": max(0, round((current - self.fetched_at).total_seconds(), 2)),
            "cache_hit": cache_hit,
            "stale": current >= self.fresh_until,
            "warnings": self.warnings,
        }


class CachedResult(BaseModel):
    """Associate a source snapshot with cache-hit and refresh-failure information."""

    snapshot: Snapshot
    cache_hit: bool
    refresh_error: str | None = None

    def provenance(self) -> dict[str, Any]:
        """Return source provenance augmented with refresh and stale-data warnings."""
        result = self.snapshot.provenance(cache_hit=self.cache_hit)
        if self.refresh_error:
            result["refresh_error"] = self.refresh_error
        if result["stale"]:
            result["warnings"] = [
                *result["warnings"],
                "STALE: this snapshot is beyond its fresh window; check refresh_error or start a new paginated query.",
            ]
        return result


class ToolResponse(BaseModel):
    """Define the versioned structured envelope returned by every research tool."""

    schema_version: str = "1.0"
    request_id: str
    status: Literal["ok", "partial", "error"] = "ok"
    data: dict[str, Any] = Field(default_factory=dict)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None


def require_known(values: list[Any], description: str) -> None:
    """Reject an empty collection or any unknown value needed for a calculation."""
    if not values or any(value is None for value in values):
        raise ResearchError(
            "insufficient_data", f"Missing {description}; a reliable result cannot be calculated."
        )

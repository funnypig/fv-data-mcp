from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas_market_calendars as calendars

from fv_data_mcp.errors import ResearchError

NEW_YORK = ZoneInfo("America/New_York")


@lru_cache(maxsize=512)
def session_close(session: date) -> datetime:
    """Return the NYSE session close in UTC, including early-close sessions."""
    calendar = calendars.get_calendar("NYSE")
    schedule = calendar.schedule(start_date=session, end_date=session)
    if schedule.empty:
        raise ResearchError(
            "unsupported_expiration", "Expiration is not an exchange trading session."
        )
    return schedule.iloc[0]["market_close"].to_pydatetime().astimezone(UTC)


def expiry_time(expiration: date) -> datetime:
    """Model a supported equity/ETF expiration at its NYSE session close."""
    return session_close(expiration)


def year_fraction(expiration: date, now: datetime) -> float:
    """Return nonnegative ACT/365.25 years until the modeled expiration."""
    return max(0.0, (expiry_time(expiration) - now).total_seconds() / (365.25 * 86400))


def expiration_info(expiration: date, now: datetime) -> dict[str, str | int]:
    """Describe calendar DTE, subsequent trading sessions, and modeled expiry time."""
    today = now.astimezone(NEW_YORK).date()
    calendar = calendars.get_calendar("NYSE")
    schedule = (
        calendar.schedule(start_date=today + timedelta(days=1), end_date=expiration)
        if expiration > today
        else []
    )
    return {
        "expiration": expiration.isoformat(),
        "calendar_dte": max(0, (expiration - today).days),
        "trading_dte": len(schedule),
        "modeled_expiry_at": expiry_time(expiration).isoformat(),
    }

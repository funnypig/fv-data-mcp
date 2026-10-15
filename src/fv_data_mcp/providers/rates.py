from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import httpx
import numpy as np
from defusedxml import ElementTree

from fv_data_mcp.cache import Payload, SnapshotCache
from fv_data_mcp.errors import ResearchError
from fv_data_mcp.models import CachedResult, utc_now

TENORS = {
    "BC_1MONTH": 1 / 12,
    "BC_2MONTH": 2 / 12,
    "BC_3MONTH": 0.25,
    "BC_4MONTH": 1 / 3,
    "BC_6MONTH": 0.5,
    "BC_1YEAR": 1,
    "BC_2YEAR": 2,
    "BC_3YEAR": 3,
    "BC_5YEAR": 5,
    "BC_7YEAR": 7,
    "BC_10YEAR": 10,
    "BC_20YEAR": 20,
    "BC_30YEAR": 30,
}


def parse_curve(xml: str, today: date) -> dict[str, Any]:
    """Extract the latest usable Treasury par-yield curve no older than seven days."""
    try:
        root = ElementTree.fromstring(xml)
    except Exception as exc:
        raise ResearchError(
            "reference_data_unavailable", "Treasury returned invalid XML.", True
        ) from exc
    observations = []
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1] != "properties":
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: child.text for child in entry}
        try:
            stamp = date.fromisoformat(fields["NEW_DATE"][:10])
            points = sorted(
                (years, float(fields[name]) / 100)
                for name, years in TENORS.items()
                if fields.get(name)
            )
        except (KeyError, ValueError, TypeError):
            continue
        if stamp <= today and points and all(np.isfinite(rate) and rate > -1 for _, rate in points):
            observations.append((stamp, points))
    if not observations:
        raise ResearchError(
            "reference_data_unavailable", "No usable Treasury rate observations.", True
        )
    stamp, points = max(observations, key=lambda observation: observation[0])
    if (today - stamp).days > 7:
        raise ResearchError(
            "reference_data_unavailable", "Treasury rates are more than seven days old.", True
        )
    return {
        "as_of": stamp.isoformat(),
        "points": [{"years": years, "yield": rate} for years, rate in points],
    }


def interpolate_rate(curve: dict[str, Any], years: float) -> float:
    """Interpolate a par yield by maturity and approximate a continuous annual rate."""
    points = curve["points"]
    annual = np.interp(
        years, [point["years"] for point in points], [point["yield"] for point in points]
    )
    return float(2 * np.log1p(annual / 2))


class TreasuryProvider:
    """Cache bounded public Treasury reference curves for option modeling."""

    client: httpx.AsyncClient
    cache: SnapshotCache

    def __init__(self, client: httpx.AsyncClient, cache: SnapshotCache) -> None:
        """Bind HTTP retrieval and the shared reference-data cache."""
        self.client, self.cache = client, cache

    async def curve(self) -> CachedResult:
        """Return a cached reference curve whose observation date remains usable."""

        async def load() -> Payload:
            """Fetch current-year curves with an early-January prior-year fallback."""
            today = utc_now().date()
            observations = []
            for year in (
                [today.year, today.year - 1]
                if today.month == 1 and today.day <= 7
                else [today.year]
            ):
                try:
                    async with asyncio.timeout(10):
                        async with self.client.stream(
                            "GET",
                            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml",
                            params={
                                "data": "daily_treasury_yield_curve",
                                "field_tdr_date_value": year,
                            },
                        ) as response:
                            response.raise_for_status()
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > 4_000_000:
                                    raise ResearchError(
                                        "reference_data_unavailable",
                                        "Treasury response exceeds size limit.",
                                        True,
                                    )
                            observations.append(parse_curve(body.decode("utf-8"), today))
                except (httpx.HTTPError, TimeoutError, UnicodeDecodeError):
                    continue
                except ResearchError:
                    continue
            if not observations:
                raise ResearchError(
                    "reference_data_unavailable",
                    "Treasury rates are unavailable; supply an explicit rate override.",
                    True,
                )
            curve = max(observations, key=lambda observation: observation["as_of"])
            return Payload(
                curve,
                provider="us_treasury",
                warnings=[
                    "Interpolated Treasury par yield converted to a continuous-rate proxy; not a calibrated zero curve."
                ],
            )

        result = await self.cache.fetch("reference_rates", {}, load, 86400, 7 * 86400)
        if (utc_now().date() - date.fromisoformat(result.snapshot.data["as_of"])).days > 7:
            raise ResearchError(
                "reference_data_unavailable", "Treasury rates are too old for model use.", True
            )
        return result

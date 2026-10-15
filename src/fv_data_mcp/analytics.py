from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from statistics import median
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import brentq

from fv_data_mcp.calendar import year_fraction
from fv_data_mcp.errors import ResearchError
from fv_data_mcp.models import Chain, Contract

METHOD_VERSION = "2026-10-13.1"


def usable(chain: Chain) -> list[Contract]:
    """Select standard contracts with a verified multiplier for analysis."""
    return [
        contract
        for contract in chain.contracts
        if contract.standard and contract.multiplier is not None
    ]


def coverage(chain: Chain, contracts: list[Contract]) -> dict[str, Any]:
    """Describe included contracts, rejected rows, and known-OI coverage."""
    known_oi = sum(contract.open_interest or 0 for contract in chain.contracts)
    included_oi = sum(contract.open_interest or 0 for contract in contracts)
    return {
        "input_contracts": len(chain.contracts),
        "included_contracts": len(contracts),
        "excluded_contracts": len(chain.contracts) - len(contracts),
        "rejected_provider_rows": chain.rejected_rows,
        "unknown_oi_contracts": sum(contract.open_interest is None for contract in chain.contracts),
        "known_oi_coverage": included_oi / known_oi if known_oi else None,
    }


def max_pain(chain: Chain) -> dict[str, Any]:
    """Find all observed-strike minima of full-OI intrinsic payout.

    Returns:
        Tied minimizing strikes, minimum USD payout, and input coverage.

    Raises:
        ResearchError: No usable positive open interest is available.
    """
    contracts = [contract for contract in usable(chain) if contract.open_interest is not None]
    if not contracts or not any(contract.open_interest for contract in contracts):
        raise ResearchError("insufficient_data", "Max pain requires positive known open interest.")
    grouped = defaultdict(lambda: [0.0, 0.0])
    for contract in contracts:
        grouped[contract.strike][0 if contract.side == "call" else 1] += (
            contract.open_interest * contract.multiplier
        )
    strikes = np.array(sorted(grouped), dtype=float)
    call_units = np.array([grouped[strike][0] for strike in strikes])
    put_units = np.array([grouped[strike][1] for strike in strikes])
    call_payout = strikes * np.cumsum(call_units) - np.cumsum(call_units * strikes)
    put_payout = (
        np.cumsum((put_units * strikes)[::-1])[::-1] - strikes * np.cumsum(put_units[::-1])[::-1]
    )
    total = call_payout + put_payout
    minimum = float(total.min())
    minimizing = strikes[np.isclose(total, minimum, rtol=1e-12, atol=1e-7)]
    return {
        "expiration": chain.expiration.isoformat(),
        "max_pain_strikes": minimizing.tolist(),
        "minimum_payout_usd": minimum,
        "coverage": coverage(chain, contracts),
        "method": "Full valid OI; intrinsic payout at observed strikes; all tied minima returned.",
        "method_version": METHOD_VERSION,
    }


def bsm_gamma(
    spot: float | NDArray[np.float64],
    strike: float | NDArray[np.float64],
    volatility: float | NDArray[np.float64],
    years: float | NDArray[np.float64],
    rate: float | NDArray[np.float64],
    dividend: float | NDArray[np.float64],
) -> np.float64 | NDArray[np.float64]:
    """Evaluate Black–Scholes–Merton gamma for scalar or broadcast array inputs.

    Args:
        spot: Positive underlying price in USD.
        strike: Positive strike prices in USD.
        volatility: Positive annualized decimal implied volatility.
        years: Positive ACT/365.25 time to expiration.
        rate: Continuous annual decimal risk-free rate.
        dividend: Continuous annual decimal dividend yield.

    Returns:
        Gamma per USD underlying move, with the broadcast input shape.
    """
    scale = volatility * np.sqrt(years)
    distance = (np.log(spot / strike) + (rate - dividend + volatility**2 / 2) * years) / scale
    return np.exp(-dividend * years - distance**2 / 2) / (np.sqrt(2 * np.pi) * spot * scale)


def gamma_roots(profile: Callable[[float], float], spots: NDArray[np.float64]) -> list[float]:
    """Find detected sign-changing roots on a supplied spot grid using Brent's method."""
    values = [float(profile(float(spot))) for spot in spots]
    roots = []
    for index in range(1, len(spots)):
        previous, current = values[index - 1], values[index]
        if not math.isfinite(previous) or not math.isfinite(current):
            continue
        if previous * current < 0:
            root = brentq(profile, float(spots[index - 1]), float(spots[index]), xtol=1e-8)
            roots.append(float(root))
        elif current == 0 and 0 < index < len(spots) - 1 and previous * values[index + 1] < 0:
            roots.append(float(spots[index]))
    return sorted(set(round(root, 8) for root in roots))


def gamma_exposure(
    chains: list[Chain],
    spot: float,
    now: datetime,
    rates: dict[str, float] | None = None,
    dividend_yield: float | None = None,
) -> dict[str, Any]:
    """Aggregate quoted GEX and estimate fixed-strike-IV gamma-flip crossings.

    Args:
        chains: Standard equity/ETF chains to combine.
        spot: Positive underlying reference price in USD.
        now: Timezone-aware model valuation time.
        rates: Continuous annual decimal rates keyed by ISO expiration date.
        dividend_yield: Continuous annual decimal dividend yield, when known.

    Returns:
        USD delta-notional exposure per 1% move, coverage, and flip diagnostics.
        Calls-positive/puts-negative is a positioning assumption, not inventory.

    Raises:
        ResearchError: Spot is invalid or quoted gamma/OI inputs are insufficient.
    """
    if not math.isfinite(spot) or spot <= 0:
        raise ResearchError("insufficient_data", "A positive quoted underlying price is required.")
    rows = defaultdict(lambda: {"call_gex": 0.0, "put_gex": 0.0})
    diagnostics = []
    model_contracts = []
    all_contracts = [contract for chain in chains for contract in chain.contracts]
    for chain in chains:
        included = [
            contract
            for contract in usable(chain)
            if contract.gamma is not None and contract.open_interest is not None
        ]
        diagnostics.append(
            {"expiration": chain.expiration.isoformat(), **coverage(chain, included)}
        )
        for contract in included:
            amount = contract.gamma * contract.open_interest * contract.multiplier * spot**2 * 0.01
            rows[contract.strike][contract.side + "_gex"] += amount
        years = year_fraction(chain.expiration, now)
        rate = (rates or {}).get(chain.expiration.isoformat())
        if years > 0 and rate is not None:
            for contract in usable(chain):
                if contract.iv and contract.iv > 0 and contract.open_interest is not None:
                    model_contracts.append((contract, years, rate))
    if not rows:
        raise ResearchError("insufficient_data", "GEX requires gamma, OI, and a known multiplier.")
    table = [
        {
            "strike": strike,
            "call_gex": values["call_gex"],
            "put_gex": -values["put_gex"],
            "net_gex": values["call_gex"] - values["put_gex"],
        }
        for strike, values in sorted(rows.items())
    ]
    result = {
        "spot": spot,
        "units": "USD delta-notional change per 1% underlying move",
        "sign_convention": "Calls positive, puts negative: a positioning assumption, not observed dealer inventory.",
        "call_gex": sum(row["call_gex"] for row in table),
        "put_gex": sum(row["put_gex"] for row in table),
        "net_gex": sum(row["net_gex"] for row in table),
        "gross_gex": sum(row["call_gex"] - row["put_gex"] for row in table),
        "by_strike": table,
        "coverage": diagnostics,
        "method_version": METHOD_VERSION,
        "gamma_flip": {
            "status": "unavailable",
            "reason": "Missing model inputs or insufficient coverage.",
        },
    }
    known_oi = sum(contract.open_interest or 0 for contract in all_contracts)
    modeled_oi = sum(contract.open_interest or 0 for contract, _, _ in model_contracts)
    model_coverage = modeled_oi / known_oi if known_oi else 0
    if dividend_yield is None or not model_contracts or model_coverage < 0.9:
        return result
    if any(contract.open_interest is None for contract in all_contracts) or any(
        chain.rejected_rows for chain in chains
    ):
        return result
    strikes = np.array([contract.strike for contract, _, _ in model_contracts])
    volatility = np.array([contract.iv for contract, _, _ in model_contracts])
    times = np.array([years for _, years, _ in model_contracts])
    interest = np.array([rate for _, _, rate in model_contracts])
    weights = np.array(
        [
            contract.open_interest * contract.multiplier * (1 if contract.side == "call" else -1)
            for contract, _, _ in model_contracts
        ]
    )

    def profile(scenario_spot: float) -> float:
        """Reprice signed net GEX at one hypothetical underlying price."""
        gammas = bsm_gamma(scenario_spot, strikes, volatility, times, interest, dividend_yield)
        return float(np.sum(gammas * weights) * scenario_spot**2 * 0.01)

    grid = np.linspace(spot * 0.5, spot * 1.5, 201)
    roots = gamma_roots(profile, grid)
    result["gamma_flip"] = {
        "status": "estimated" if roots else "no_crossing_in_range",
        "crossings": roots,
        "nearest_crossing": min(roots, key=lambda root: abs(root - spot)) if roots else None,
        "search_range": [float(grid[0]), float(grid[-1])],
        "scenarios": [
            {"spot": float(price), "modeled_net_gex": profile(float(price))} for price in grid
        ],
        "modeled_net_gex_at_spot": profile(spot),
        "known_oi_coverage": model_coverage,
        "rates": rates,
        "dividend_yield": dividend_yield,
        "valuation_time": now.isoformat(),
        "method": "Black-Scholes-Merton, fixed strike IV, continuous dividends, ACT/365.25; approximation for American options.",
    }
    return result


def complete_sum(contracts: list[Contract], attribute: str) -> int | None:
    """Sum a contract field only when every contributing value is known."""
    values = [getattr(contract, attribute) for contract in contracts]
    return sum(values) if values and all(value is not None for value in values) else None


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Return a ratio when both values are known and the denominator is nonzero."""
    return (
        numerator / denominator if numerator is not None and denominator not in {None, 0} else None
    )


def weighted_iv(contracts: list[Contract]) -> float | None:
    """Compute OI-weighted decimal IV for contracts with absolute delta 0.3–0.7."""
    sample = [
        contract
        for contract in contracts
        if contract.delta is not None
        and 0.3 <= abs(contract.delta) <= 0.7
        and contract.iv is not None
        and contract.iv > 0
        and contract.open_interest
    ]
    if not sample:
        return None
    return sum(contract.iv * contract.open_interest for contract in sample) / sum(
        contract.open_interest for contract in sample
    )


def logarithmic_score(value: int, low: int, high: int) -> float:
    """Map a value to a clipped zero-to-one score between logarithmic bounds."""
    if value <= low:
        return 0.0
    if value >= high:
        return 1.0
    return (math.log(value) - math.log(low)) / (math.log(high) - math.log(low))


def liquidity(contracts: list[Contract]) -> dict[str, Any]:
    """Summarize a spread/OI/volume heuristic over valid absolute-delta samples."""
    scores = []
    for contract in contracts:
        if contract.delta is None or not 0.2 <= abs(contract.delta) <= 0.8:
            continue
        if (
            contract.bid is None
            or contract.ask is None
            or contract.ask < contract.bid
            or contract.ask <= 0
        ):
            continue
        if contract.open_interest is None or contract.volume is None:
            continue
        spread = (contract.ask - contract.bid) / ((contract.ask + contract.bid) / 2)
        spread_score = max(0.0, min(1.0, 1 - (spread - 0.6) / 1.4))
        oi_score = logarithmic_score(contract.open_interest, 20, 500)
        volume_score = logarithmic_score(contract.volume, 10, 100)
        scores.append(
            {
                "spread_score": spread_score,
                "oi_score": oi_score,
                "volume_score": volume_score,
                "score": 0.66 * spread_score + 0.17 * oi_score + 0.17 * volume_score,
            }
        )
    values = {
        key: median(item[key] for item in scores) if scores else None
        for key in ("score", "spread_score", "oi_score", "volume_score")
    }
    return {
        **values,
        "sample_contracts": len(scores),
        "is_liquid": values["score"] > 0.6 if values["score"] is not None else None,
        "method": "Heuristic median score over absolute delta 0.2–0.8; not an executable-size estimate.",
    }


def option_metrics(chain: Chain, top_n: int = 10) -> dict[str, Any]:
    """Return per-side volume, OI, IV, liquidity, ratios, and strike leaders."""
    contracts = usable(chain)
    sides = {}
    for side in ("call", "put"):
        selected = [contract for contract in contracts if contract.side == side]
        interest = defaultdict(int)
        for contract in selected:
            if contract.open_interest is not None:
                interest[contract.strike] += contract.open_interest
        top = sorted(interest.items(), key=lambda entry: (-entry[1], entry[0]))[:top_n]
        sides[side] = {
            "volume": complete_sum(selected, "volume"),
            "open_interest": complete_sum(selected, "open_interest"),
            "atm_oi_weighted_iv": weighted_iv(selected),
            "top_open_interest": [
                {"strike": strike, "open_interest": amount} for strike, amount in top
            ],
            "liquidity": liquidity(selected),
        }
    return {
        "expiration": chain.expiration.isoformat(),
        **sides,
        "put_call_volume_ratio": ratio(sides["put"]["volume"], sides["call"]["volume"]),
        "put_call_oi_ratio": ratio(sides["put"]["open_interest"], sides["call"]["open_interest"]),
        "atm_oi_weighted_iv": weighted_iv(contracts),
        "iv_units": "annualized decimal",
        "coverage": coverage(chain, contracts),
        "method_version": METHOD_VERSION,
    }

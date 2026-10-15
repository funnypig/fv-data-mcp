from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import numpy as np
import pytest
from scipy.stats import norm

from fv_data_mcp.analytics import (
    bsm_gamma,
    gamma_exposure,
    gamma_roots,
    liquidity,
    max_pain,
    option_metrics,
    weighted_iv,
)
from fv_data_mcp.calendar import expiry_time
from fv_data_mcp.errors import ResearchError
from fv_data_mcp.models import Chain, Contract


def chain(contracts: list[Contract]) -> Chain:
    """Wrap contracts in the fixed test ticker and expiration."""
    return Chain(ticker="TEST", expiration=date(2027, 1, 15), contracts=contracts)


def test_max_pain_matches_brute_force_and_keeps_ties(contract: Callable[..., Contract]) -> None:
    """Check flat payout minima and compare the optimized payout against exhaustive sums."""
    contracts = [
        contract(strike=90, open_interest=3),
        contract(strike=100, open_interest=0),
        contract(strike=110, side="put", open_interest=3),
    ]
    result = max_pain(chain(contracts))
    assert result["max_pain_strikes"] == [90, 100, 110]
    assert result["minimum_payout_usd"] == 6000
    rng = np.random.default_rng(42)
    contracts = [
        contract(strike=float(strike), side=side, open_interest=int(rng.integers(0, 100)))
        for strike in range(70, 131, 5)
        for side in ["call", "put"]
    ]
    payouts = {
        strike: sum(
            max(0, strike - item.strike) * item.open_interest * 100
            if item.side == "call"
            else max(0, item.strike - strike) * item.open_interest * 100
            for item in contracts
        )
        for strike in range(70, 131, 5)
    }
    assert max_pain(chain(contracts))["minimum_payout_usd"] == min(payouts.values())


def test_max_pain_missing_and_adjusted(contract: Callable[..., Contract]) -> None:
    """Require usable positive OI rather than inferring values for unknown contracts."""
    with pytest.raises(ResearchError, match="positive known"):
        max_pain(chain([contract(open_interest=None), contract(standard=False, multiplier=None)]))


def test_bsm_gamma_matches_numerical_price_second_derivative() -> None:
    """Check analytical gamma against finite-difference curvature of a BSM call price."""
    strike, volatility, years, rate, dividend = 100, 0.3, 0.5, 0.04, 0.01

    def price(spot: float) -> np.float64:
        """Evaluate the dividend-adjusted BSM call price at a hypothetical spot."""
        distance = (np.log(spot / strike) + (rate - dividend + volatility**2 / 2) * years) / (
            volatility * np.sqrt(years)
        )
        return spot * np.exp(-dividend * years) * norm.cdf(distance) - strike * np.exp(
            -rate * years
        ) * norm.cdf(distance - volatility * np.sqrt(years))

    step = 0.01
    numerical = (price(100 + step) - 2 * price(100) + price(100 - step)) / step**2
    assert bsm_gamma(100, strike, volatility, years, rate, dividend) == pytest.approx(
        numerical, rel=1e-6
    )


def test_gamma_roots_multiple_and_no_crossings() -> None:
    """Distinguish detected sign changes from constant or identically zero profiles."""
    assert gamma_roots(
        lambda spot: (spot - 91.2) * (spot - 106.3), np.linspace(50, 150, 201)
    ) == pytest.approx([91.2, 106.3])
    assert gamma_roots(lambda spot: 1.0, np.linspace(50, 150, 201)) == []
    assert gamma_roots(lambda spot: 0.0, np.linspace(50, 150, 201)) == []


def test_gex_units_and_flip_missing_inputs(contract: Callable[..., Contract]) -> None:
    """Check USD exposure scaling while unavailable model inputs leave flip unset."""
    data = gamma_exposure(
        [chain([contract(), contract(side="put", open_interest=50)])],
        100,
        datetime(2026, 10, 13, tzinfo=UTC),
    )
    assert data["net_gex"] == 10000
    assert data["gross_gex"] == 30000
    assert data["gamma_flip"]["status"] == "unavailable"


def test_gamma_single_side_has_no_fabricated_flip(contract: Callable[..., Contract]) -> None:
    """Ensure a one-sided exposure profile reports no crossing rather than a fake root."""
    data = gamma_exposure(
        [chain([contract()])], 100, datetime(2026, 10, 13, tzinfo=UTC), {"2027-01-15": 0.04}, 0.0
    )
    assert data["gamma_flip"]["status"] == "no_crossing_in_range"
    assert data["gamma_flip"]["nearest_crossing"] is None


def test_iv_includes_negative_delta_puts_and_volume_score_is_independent(
    contract: Callable[..., Contract],
) -> None:
    """Check put inclusion, volume-sensitive scoring, and null-safe incomplete metrics."""
    contracts = [contract(iv=0.2), contract(side="put", delta=-0.5, iv=0.4)]
    assert weighted_iv(contracts) == pytest.approx(0.3)
    assert liquidity([contract(volume=10)])["volume_score"] == 0
    assert liquidity([contract(volume=100)])["volume_score"] == 1
    assert liquidity([contract(bid=2, ask=1)])["score"] is None
    assert option_metrics(chain([contract(volume=None)]))["put_call_volume_ratio"] is None


def test_expiry_uses_early_exchange_close() -> None:
    """Respect early closes and reject nontrading expiration dates."""
    assert expiry_time(date(2026, 11, 27)).hour == 18
    with pytest.raises(ResearchError):
        expiry_time(date(2026, 12, 25))

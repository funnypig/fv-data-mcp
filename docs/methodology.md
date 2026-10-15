# Calculation methodology

These are descriptive research tools, not execution-quality signals. Method version: `2026-10-13.1`. Source provenance accompanies every result; inspect warnings and coverage before interpreting a number. Tests compare max pain against brute force and BSM gamma against numerical price differentiation.

## Contracts and expiration dates

Only provider-listed dates are discovered; no synthetic Friday calendar. Explicit expiration queries remain possible if Finviz changes its HTML listing. The adapter validates OCC symbol expiry/right/strike against CSV fields, deduplicates identical rows, rejects conflicting duplicates, and records rejected rows. Standard US stock/ETF contracts with verified ordinary roots use multiplier 100. Adjusted/unverified roots are displayed but excluded from calculations, not silently assigned a multiplier.

This inference is not an authoritative OCC deliverables feed. Do not use these results for adjusted contracts, index/AM-settled products or non-US options; obtain a deliverables feed before expanding scope. Expiry timing is modeled at the NYSE session close in America/New_York, including holidays and early closes, ACT/365.25. Some ETF options trade after that modeled close, so final-minutes estimates are approximate. Index AM settlement is unsupported. Expired dates are rejected. Calendar DTE counts local dates; trading DTE counts sessions strictly after today through expiration.

## Max pain

For settlement spot `S`, payout is the sum over contracts:

```text
calls: max(S - strike, 0) × open_interest × multiplier
puts:  max(strike - S, 0) × open_interest × multiplier
```

Minimize at all observed valid strikes, returning every tied minimum. Sort and prefix/suffix sums avoid quadratic loops. No delta-based trimming or arbitrary OI threshold. Unknown OI and unverified multipliers are excluded with coverage, not treated as measured zero. A flat interval between returned minimizing strikes may also minimize the continuous piecewise-linear payout; the output is a list of observed-strike minima, not a prediction of expiry price. All-zero/missing OI is insufficient data.

## Gamma exposure

```text
quoted-gamma GEX = gamma × open_interest × multiplier × spot² × 0.01
```

Units: USD delta-notional change for a 1% underlying move. Calls are positive, puts negative; this **assumes** a positioning convention and does not observe dealer inventory. Return call, put, signed net, gross and strike aggregates. Only known gamma/OI/multiplier rows contribute; per-expiration coverage identifies exclusions. Stock spot is the latest available five-minute bar close, which may be old outside market hours. Option Greeks and stock prices need not have synchronized source timestamps. Quoted-gamma exposure is descriptive at the provider's quoted Greeks; it is not repriced live exposure.

### Modeled gamma flip

Reprice each valid contract's gamma using Black–Scholes–Merton at each hypothetical spot, holding that contract's strike IV fixed. Search 201 evenly spaced points from `0.5 × spot` to `1.5 × spot`; bracket sign changes and solve with Brent's method. Return all **detected** crossings, the nearest crossing, sampled profile and valuation time. A fixed grid can miss tightly spaced crossings; do not call it a proof of all mathematical roots. Tangencies without sign change are not flips. A profile that stays one-sided or identically zero returns `no_crossing_in_range` and a null nearest crossing.

Use explicit caller overrides for continuous annual decimal risk-free rate/dividend yield, or obtain a Treasury curve and the indicated dividend yield from the company snapshot. Treasury par yields are linearly interpolated by maturity with endpoint clamping, then approximated as continuous rates via `2*log(1+yield/2)`; this is **not a bootstrapped zero curve**. Missing dividend is never assumed zero. Provider indicated yield is used as a continuous-dividend approximation; discrete dividends, borrow and early exercise are not modeled.

If required inputs are missing, any OI is unknown, provider rows were rejected, or modeled known-OI coverage is below 90%, flip is `unavailable`; valid quoted GEX is still returned with partial status. American exercise and volatility-surface dynamics limit the estimate. Gamma flip is a **model output**, not a known market threshold.

## Metrics

- Separate call/put volume and OI totals and put/call ratios; a missing contributing value or zero denominator yields null, never an invented zero or infinity.
- OI-weighted IV uses positive IV/OI with **absolute** delta 0.3–0.7, so ordinary negative-delta puts participate. IV units are annualized decimals.
- Top OI levels aggregate by strike and sort descending, deterministic ties by strike.
- Liquidity samples absolute delta 0.2–0.8 with known non-crossed bid/ask, positive ask, known volume and OI. Spread score clips `1-(relative_spread-0.6)/1.4` to [0,1]; OI score is log-scaled 20–500, **volume** score independently log-scaled 10–100. Final score is median of `0.66*spread + 0.17*OI + 0.17*volume`; above 0.6 labels liquid. These inherited heuristic thresholds are permissive, not a validated execution/slippage model. Missing samples yield null.

## Deliberate limits and next useful additions

No historical OI change, unusual-volume baseline, IV percentile/rank or backtest is claimed from a current snapshot. Those require licensed historical storage and a separate retention design. Useful extensions after live calibration: ATM term structure and 25-delta skew with interpolation/coverage rules; expirations-specific liquidity summaries; confidence diagnostics for model roots; deliverables-aware adjusted options. Keep these deterministic and versioned, using the same source snapshots.

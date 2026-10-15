from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Literal

from fv_data_mcp.analytics import METHOD_VERSION, gamma_exposure, max_pain, option_metrics
from fv_data_mcp.cache import SnapshotCache
from fv_data_mcp.calendar import expiration_info, expiry_time, year_fraction
from fv_data_mcp.cursors import Cursors
from fv_data_mcp.errors import ResearchError
from fv_data_mcp.models import CachedResult, Chain, Side, utc_now
from fv_data_mcp.providers.articles import ArticleProvider
from fv_data_mcp.providers.finviz import FinvizProvider
from fv_data_mcp.providers.rates import TreasuryProvider, interpolate_rate


def result(
    data: dict[str, Any], sources: list[CachedResult], warnings: list[str] | None = None
) -> dict[str, Any]:
    """Build a research envelope with deduplicated source provenance and warnings."""
    unique = {source.snapshot.snapshot_id: source for source in sources}
    provenance = [source.provenance() for source in unique.values()]
    notes = list(
        dict.fromkeys(
            [*(warnings or []), *(note for source in provenance for note in source["warnings"])]
        )
    )
    return {
        "data": data,
        "sources": provenance,
        "warnings": notes,
        "status": "partial" if any(source["stale"] for source in provenance) else "ok",
    }


class ResearchService:
    """Compose authenticated tool operations from reusable cached provider datasets."""

    finviz: FinvizProvider
    treasury: TreasuryProvider
    articles: ArticleProvider
    cache: SnapshotCache
    cursors: Cursors

    def __init__(
        self,
        finviz: FinvizProvider,
        treasury: TreasuryProvider,
        articles: ArticleProvider,
        cache: SnapshotCache,
        cursor_secret: str,
    ) -> None:
        """Bind providers, shared cache, and signed snapshot-pagination support."""
        self.finviz, self.treasury, self.articles, self.cache = finviz, treasury, articles, cache
        self.cursors = Cursors(cursor_secret)

    async def quote(self, ticker: str) -> dict[str, Any]:
        """Return the latest available bar-close snapshot with provenance."""
        source = await self.finviz.quote(ticker)
        return result(source.snapshot.data, [source])

    async def overview(self, ticker: str) -> dict[str, Any]:
        """Return company fundamentals and their retrieval provenance."""
        source = await self.finviz.overview(ticker)
        return result(source.snapshot.data, [source])

    async def statements(
        self, ticker: str, kind: Literal["income", "balance", "cash_flow"]
    ) -> dict[str, Any]:
        """Return a selected quarterly statement in provider-native structure."""
        source = await self.finviz.statement(ticker, kind)
        return result(source.snapshot.data, [source])

    async def expirations(self, ticker: str) -> dict[str, Any]:
        """Describe listed, unexpired dates with calendar and trading-session DTE."""
        source = await self.finviz.expirations(ticker)
        now = utc_now()
        listed = [date.fromisoformat(value) for value in source.snapshot.data["expirations"]]
        return result(
            {
                "ticker": ticker,
                "expirations": [
                    expiration_info(expiration, now)
                    for expiration in listed
                    if expiry_time(expiration) > now
                ],
            },
            [source],
        )

    async def chains(
        self, ticker: str, expirations: list[date] | None, default_count: int = 1
    ) -> tuple[list[Chain], list[CachedResult]]:
        """Fetch up to six distinct unexpired chains and retain their source snapshots.

        Args:
            ticker: Normalized equity/ETF ticker.
            expirations: Explicit listed dates, or None to select nearest listed dates.
            default_count: Number of nearest dates to select when none are supplied.

        Returns:
            Validated chains and all snapshots used to select and fetch them.

        Raises:
            ResearchError: No supported dates exist or a required source cannot be read.
        """
        sources = []
        now = utc_now()
        if not expirations:
            listing = await self.finviz.expirations(ticker)
            sources.append(listing)
            expirations = [
                date.fromisoformat(value)
                for value in listing.snapshot.data["expirations"]
                if expiry_time(date.fromisoformat(value)) > now
            ][:default_count]
        expirations = sorted(set(expirations))
        if not expirations or len(expirations) > 6:
            raise ResearchError(
                "invalid_argument", "Choose between one and six listed, unexpired expirations."
            )
        if any(expiry_time(expiration) <= now for expiration in expirations):
            raise ResearchError(
                "invalid_argument", "Historical or expired option chains are not supported."
            )
        fetched = await asyncio.gather(
            *(self.finviz.chain(ticker, expiration) for expiration in expirations)
        )
        chains = [Chain.model_validate(source.snapshot.data) for source in fetched]
        return chains, [*sources, *fetched]

    async def chain(
        self,
        ticker: str,
        expiration: date,
        side: Side | None,
        min_strike: float | None,
        max_strike: float | None,
        min_open_interest: int | None,
        page_size: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        """Filter and paginate one chain while preserving an immutable source snapshot.

        Returns:
            Contract rows, continuation cursor, source expiry, and provenance.

        Raises:
            ResearchError: Filters are inconsistent or pagination state is invalid.
        """
        if min_strike is not None and max_strike is not None and min_strike > max_strike:
            raise ResearchError("invalid_argument", "Minimum strike cannot exceed maximum strike.")
        binding = {
            "ticker": ticker,
            "expiration": expiration.isoformat(),
            "side": side,
            "min_strike": min_strike,
            "max_strike": max_strike,
            "min_open_interest": min_open_interest,
            "page_size": page_size,
        }
        offset = 0
        if cursor:
            decoded = self.cursors.decode(cursor, binding)
            source = CachedResult(
                snapshot=await self.cache.by_id(decoded["snapshot"]), cache_hit=True
            )
            chain = Chain.model_validate(source.snapshot.data)
            if chain.ticker != ticker or chain.expiration != expiration:
                raise ResearchError("invalid_cursor", "Cursor dataset mismatch.")
            offset = decoded["offset"]
            sources = [source]
        else:
            chains, sources = await self.chains(ticker, [expiration])
            chain, source = chains[0], sources[-1]
        contracts = sorted(
            (
                contract
                for contract in chain.contracts
                if (side is None or contract.side == side)
                and (min_strike is None or contract.strike >= min_strike)
                and (max_strike is None or contract.strike <= max_strike)
                and (
                    min_open_interest is None
                    or contract.open_interest is not None
                    and contract.open_interest >= min_open_interest
                )
            ),
            key=lambda contract: (contract.strike, contract.side, contract.symbol),
        )
        next_cursor = None
        if offset + page_size < len(contracts):
            next_cursor = self.cursors.encode(
                {
                    "binding": binding,
                    "offset": offset + page_size,
                    "snapshot": source.snapshot.snapshot_id,
                    "expires": source.snapshot.expires_at.timestamp(),
                }
            )
        return result(
            {
                "ticker": ticker,
                "expiration": expiration.isoformat(),
                "total_contracts": len(contracts),
                "contracts": [
                    contract.model_dump(mode="json")
                    for contract in contracts[offset : offset + page_size]
                ],
                "next_cursor": next_cursor,
                "snapshot_expires_at": source.snapshot.expires_at.isoformat(),
                "rejected_rows": chain.rejected_rows,
            },
            sources,
        )

    async def pain(self, ticker: str, expirations: list[date] | None) -> dict[str, Any]:
        """Compute or reuse full-OI max-pain results for selected expirations."""
        chains, sources = await self.chains(ticker, expirations)
        data = await self.cache.calculated(
            "max_pain:" + METHOD_VERSION,
            [source.snapshot.snapshot_id for source in sources],
            {},
            lambda: {"ticker": ticker, "expirations": [max_pain(chain) for chain in chains]},
        )
        return result(data, sources)

    async def metrics(self, ticker: str, expiration: date) -> dict[str, Any]:
        """Compute or reuse liquidity, IV, volume, and OI statistics for one chain."""
        chains, sources = await self.chains(ticker, [expiration])
        data = await self.cache.calculated(
            "metrics:" + METHOD_VERSION,
            [source.snapshot.snapshot_id for source in sources],
            {},
            lambda: {"ticker": ticker, **option_metrics(chains[0])},
        )
        return result(data, sources)

    async def gamma(
        self,
        ticker: str,
        expirations: list[date] | None,
        risk_free_rate: float | None = None,
        dividend_yield: float | None = None,
        default_count: int = 1,
        loaded: tuple[list[Chain], list[CachedResult]] | None = None,
    ) -> dict[str, Any]:
        """Compose quoted exposure and optional modeled gamma-flip estimates.

        Args:
            ticker: Normalized underlying ticker.
            expirations: Dates to analyze, or None to select nearest listed dates.
            risk_free_rate: Optional continuous annual decimal rate override.
            dividend_yield: Optional continuous annual decimal dividend override.
            default_count: Number of nearest dates selected by default.
            loaded: Previously fetched chains and snapshots, preserving summary consistency.

        Returns:
            GEX, model inputs, and provenance; missing flip inputs produce partial results.
        """
        chains, original_sources = loaded or await self.chains(ticker, expirations, default_count)
        sources = list(original_sources)
        quoted = await self.finviz.quote(ticker)
        sources.append(quoted)
        warnings = []
        now = utc_now().replace(second=0, microsecond=0)
        rates = None
        if risk_free_rate is not None:
            rates = {chain.expiration.isoformat(): risk_free_rate for chain in chains}
            warnings.append("Risk-free continuous rate supplied by the caller.")
        else:
            try:
                curve = await self.treasury.curve()
                sources.append(curve)
                rates = {
                    chain.expiration.isoformat(): interpolate_rate(
                        curve.snapshot.data, year_fraction(chain.expiration, now)
                    )
                    for chain in chains
                }
            except ResearchError as exc:
                warnings.append("Gamma flip lacks rate input: " + exc.code)
        if dividend_yield is None:
            try:
                overview = await self.finviz.overview(ticker)
                sources.append(overview)
                dividend_yield = overview.snapshot.data["indicated_dividend_yield"]
            except ResearchError as exc:
                warnings.append("Gamma flip lacks dividend input: " + exc.code)
        else:
            warnings.append("Continuous dividend yield supplied by the caller.")
        data = await self.cache.calculated(
            "gamma:" + METHOD_VERSION,
            [source.snapshot.snapshot_id for source in sources],
            {"valuation_time": now.isoformat(), "rates": rates, "dividend_yield": dividend_yield},
            lambda: {
                "ticker": ticker,
                **gamma_exposure(chains, quoted.snapshot.data["price"], now, rates, dividend_yield),
            },
        )
        if data["gamma_flip"]["status"] == "unavailable":
            warnings.append("Gamma flip is unavailable; quoted-gamma exposure remains available.")
        output = result(data, sources, warnings)
        if data["gamma_flip"]["status"] == "unavailable":
            output["status"] = "partial"
        return output

    async def summary(
        self,
        ticker: str,
        expirations: list[date] | None,
        risk_free_rate: float | None = None,
        dividend_yield: float | None = None,
    ) -> dict[str, Any]:
        """Combine max pain, metrics, and GEX using the same selected chain snapshots."""
        chains, sources = await self.chains(ticker, expirations, 3)
        selected = [chain.expiration for chain in chains]
        gamma = await self.gamma(
            ticker, selected, risk_free_rate, dividend_yield, loaded=(chains, sources)
        )
        statistics = []
        partial = False
        for chain in chains:
            try:
                pain = max_pain(chain)
            except ResearchError as exc:
                pain = {"error": exc.as_dict()}
                partial = True
            statistics.append(
                {
                    "expiration": chain.expiration.isoformat(),
                    "max_pain": pain,
                    "metrics": option_metrics(chain),
                }
            )
        gamma["data"] = {
            "ticker": ticker,
            "expirations": statistics,
            "gamma_exposure": gamma["data"],
        }
        all_sources = {
            source["snapshot_id"]: source
            for source in [*[source.provenance() for source in sources], *gamma["sources"]]
        }
        gamma["sources"] = list(all_sources.values())
        if partial:
            gamma["status"] = "partial"
        return gamma

    async def news(self, ticker: str | None, limit: int) -> dict[str, Any]:
        """Return bounded headlines with shared article IDs for allowed source URLs."""
        source = await self.finviz.news(ticker)
        items = []
        for item in source.snapshot.data["items"][:limit]:
            try:
                identifier = await self.articles.register(item["source_url"])
                items.append({**item, "article_id": identifier})
            except ResearchError as exc:
                if exc.code != "article_blocked":
                    raise
                items.append({**item, "article_id": None, "article_status": "blocked"})
        return result({"ticker": ticker, "items": items}, [source])

    async def article(self, article_id: str) -> dict[str, Any]:
        """Return cached public article text for a news-provided identifier."""
        source = await self.articles.fetch(article_id)
        return result(source.snapshot.data, [source])

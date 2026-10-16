from __future__ import annotations

import csv
import io
import json
import math
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from pydantic import ValidationError

from fv_data_mcp.errors import ResearchError
from fv_data_mcp.models import Chain, Contract


def number(value: str | int | float | None, integer: bool = False) -> int | float | None:
    """Parse finite numeric or percent text, retaining unknown values as None."""
    if value is None or str(value).strip() in {"", "-", "N/A", "nan", "null"}:
        return None
    text = str(value).strip().replace(",", "")
    percent = text.endswith("%")
    try:
        result = float(text.removesuffix("%")) / (100 if percent else 1)
    except ValueError:
        return None
    if not math.isfinite(result) or (integer and not result.is_integer()):
        return None
    return int(result) if integer else result


def csv_records(text: str) -> list[dict[str, str | None]]:
    """Parse provider CSV rows, rejecting HTML and invalid headers."""
    if not text.strip() or text.lstrip().startswith("<"):
        raise ResearchError("provider_schema_changed", "Expected CSV data from Finviz.")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames is None or len(reader.fieldnames) < 2:
        raise ResearchError("provider_schema_changed", "Invalid Finviz CSV header.")
    return [{key.strip(): value for key, value in row.items() if key is not None} for row in reader]


def parse_chain(text: str, ticker: str, expiration: date) -> Chain:
    """Validate OCC metadata and build a deduplicated option chain.

    Args:
        text: Provider CSV content.
        ticker: Normalized requested equity/ETF ticker.
        expiration: Requested expiration matched against every contract symbol.

    Returns:
        Validated contracts with rejected-row and adjusted-contract diagnostics.

    Raises:
        ResearchError: Headers, duplicates, size, or all contract rows are invalid.
    """
    rows = csv_records(text)
    if len(rows) > 20000:
        raise ResearchError("response_too_large", "Too many contracts in a single expiration.")
    header = next(csv.reader(io.StringIO(text.lstrip("\ufeff"))), [])
    if not {"Strike", "Type", "Open Int."}.issubset(field.strip() for field in header):
        raise ResearchError("provider_schema_changed", "Required option-chain columns are missing.")
    contracts = {}
    rejected = 0
    for row in rows:
        symbol = row.get("Contract Name") or row.get("Contract") or row.get("Name") or ""
        match = re.fullmatch(r"([A-Z0-9. -]+)(\d{6})([CP])(\d{8})", symbol.replace(" ", ""))
        try:
            if match is None:
                raise ValueError("Unrecognized contract symbol")
            root, expiry, right, encoded_strike = match.groups()
            actual_expiry = date(2000 + int(expiry[:2]), int(expiry[2:4]), int(expiry[4:6]))
            side = row["Type"].strip().lower()
            strike = number(row["Strike"])
            if actual_expiry != expiration or side != ("call" if right == "C" else "put"):
                raise ValueError("Contract metadata mismatch")
            if strike is None or not math.isclose(strike, int(encoded_strike) / 1000):
                raise ValueError("Contract strike mismatch")
            standard = not any(character.isdigit() for character in root)
            if root.replace(".", "-") != ticker.replace(".", "-"):
                standard = False
            contract = Contract(
                symbol=symbol,
                expiration=expiration,
                side=side,
                strike=strike,
                standard=standard,
                multiplier=100 if standard else None,
                bid=number(row.get("Bid")),
                ask=number(row.get("Ask")),
                last=number(row.get("Last Close")),
                last_trade=row.get("Last Trade") or None,
                open_interest=number(row.get("Open Int."), True),
                volume=number(row.get("Volume"), True),
                iv=number(row.get("IV")),
                delta=number(row.get("Delta")),
                gamma=number(row.get("Gamma")),
                theta=number(row.get("Theta")),
                vega=number(row.get("Vega")),
                rho=number(row.get("Rho")),
            )
            if symbol in contracts and contracts[symbol] != contract:
                raise ResearchError(
                    "provider_schema_changed", "Conflicting duplicate contracts from provider."
                )
            contracts[symbol] = contract
        except (ValueError, ValidationError):
            rejected += 1
    if rows and not contracts:
        raise ResearchError("provider_schema_changed", "No option rows passed contract validation.")
    warnings = []
    if rejected:
        warnings.append(
            f"Rejected {rejected} invalid contract rows; calculations may be incomplete."
        )
    if any(not contract.standard for contract in contracts.values()):
        warnings.append(
            "Adjusted or unverified contracts are shown but excluded from calculations."
        )
    return Chain(
        ticker=ticker,
        expiration=expiration,
        contracts=list(contracts.values()),
        rejected_rows=rejected,
        warnings=warnings,
    )


def parse_export_expirations(text: str, ticker: str) -> list[date]:
    """Extract listed dates from the all-expiration export, checking OCC dates and roots."""
    rows = csv_records(text)
    header = next(csv.reader(io.StringIO(text.lstrip("\ufeff"))), [])
    if not {"Contract Name", "Expiry"}.issubset(field.strip() for field in header):
        raise ResearchError(
            "provider_schema_changed", "Option export expiration columns are missing."
        )
    expirations = set()
    for row in rows:
        symbol = (row.get("Contract Name") or "").replace(" ", "")
        match = re.fullmatch(r"([A-Z0-9. -]+)(\d{6})[CP]\d{8}", symbol)
        try:
            if match is None:
                raise ValueError
            root, encoded = match.groups()
            if root.replace(".", "-") != ticker.replace(".", "-"):
                continue
            expiration = datetime.strptime(row.get("Expiry") or "", "%m/%d/%Y").date()
            contract_expiration = date(2000 + int(encoded[:2]), int(encoded[2:4]), int(encoded[4:]))
            if expiration != contract_expiration:
                raise ValueError
            expirations.add(expiration)
        except ValueError:
            raise ResearchError(
                "provider_schema_changed",
                "Option export contains inconsistent expiration metadata.",
            ) from None
    if rows and not expirations:
        raise ResearchError(
            "provider_schema_changed", "Option export has no matching ticker contracts."
        )
    return sorted(expirations)


def parse_overview_export(overview: str, financial: str, ticker: str) -> dict[str, Any]:
    """Merge single-ticker exports and distinguish unavailable profile fields from zero values."""
    metrics = {}
    for text in (overview, financial):
        rows = csv_records(text)
        if not rows:
            raise ResearchError(
                "not_found", "Finviz did not return this ticker in the screener export."
            )
        if len(rows) != 1 or rows[0].get("Ticker") != ticker:
            raise ResearchError(
                "provider_schema_changed", "Finviz screener returned unexpected ticker rows."
            )
        metrics.update(
            {key: value for key, value in rows[0].items() if key not in {"No.", "Ticker"}}
        )
    dividend = metrics.get("Dividend Yield")
    dividend_yield = number(dividend) if dividend and dividend.endswith("%") else None
    if dividend_yield is not None and not 0 <= dividend_yield <= 1:
        dividend_yield = None
    return {
        "ticker": ticker,
        "description": None,
        "metrics": metrics,
        "institutional_ownership": [],
        "indicated_dividend_yield": dividend_yield,
    }


def parse_expirations(html: str) -> list[date]:
    """Extract explicit provider-listed dates without inventing expiration schedules."""
    soup = BeautifulSoup(html, "html.parser")
    values = set()
    for element in soup.select(
        "select[name='e'] option, select[id*='expir'] option, [data-expiration]"
    ):
        value = (
            element.get("value") or element.get("data-expiration") or element.get_text(strip=True)
        )
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            values.add(date.fromisoformat(value))
    for link in soup.select("a[href]"):
        query = parse_qs(urlsplit(link["href"]).query)
        if query.get("ty") == ["oc"] and "e" in query:
            candidate = query["e"][0]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
                values.add(date.fromisoformat(candidate))
    for match in re.finditer(r'"(?:expirations|expirationDates)"\s*:\s*(\[[^\]]*\])', html):
        try:
            candidates = json.loads(match.group(1))
            for candidate in candidates:
                if isinstance(candidate, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
                    values.add(date.fromisoformat(candidate))
        except (ValueError, TypeError):
            continue
    if not values:
        raise ResearchError(
            "provider_capability_unavailable",
            "Finviz did not expose a recognizable expiration list. Explicit expirations remain supported.",
        )
    return sorted(values)


def parse_overview(html: str, ticker: str) -> dict[str, Any]:
    """Extract company metrics, description, ownership, and a known dividend yield."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.snapshot-table2")
    if table is None:
        raise ResearchError("provider_schema_changed", "Finviz company snapshot was not found.")
    metrics = {}
    for row in table.select("tr"):
        cells = row.find_all("td", recursive=False)
        for index in range(0, len(cells) - 1, 2):
            label = cells[index].get_text(" ", strip=True)
            metrics[label] = cells[index + 1].get_text(" ", strip=True)
    profile = soup.select_one(".quote_profile-bio, td.fullview-profile.quote_profile")
    dividend = metrics.get("Dividend Est.", metrics.get("Dividend", ""))
    matched = re.search(r"\(([\d.]+)%\)", dividend)
    dividend_yield = (
        float(matched.group(1)) / 100
        if matched
        else (0.0 if dividend in {"0", "0.00", "0.00 (0.00%)"} else None)
    )
    owners = []
    for row in soup.select(".managers-and-funds table tbody tr"):
        cells = row.find_all("td")
        if len(cells) >= 2:
            owners.append(
                {
                    "name": cells[0].get_text(" ", strip=True),
                    "ownership": cells[1].get_text(" ", strip=True),
                }
            )
    return {
        "ticker": ticker,
        "description": profile.get_text(" ", strip=True) if profile else None,
        "metrics": metrics,
        "institutional_ownership": owners,
        "indicated_dividend_yield": dividend_yield,
    }

"""Server-side client for the equities-data Postman collection.

The HTTP gateway complements the Tequity websocket: it supplies company and
macro records from financialdatasets.ai and FMP. No internal token is sent to
the browser or included in a tool result. User watchlist writes require a
separate, explicitly supplied user JWT and are never registered as chat tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
import json
import re
from typing import Any

import httpx

from app.provider_router import NoData, ProviderRouter, ProviderTool
from app.routing.instruments import equity_instruments
from app.settings import settings
from app.tool_results import compact_tool_result


_ROOT = "/api/v1/internal"
_TICKER = re.compile(r"(?<![A-Za-z0-9])\$?([A-Z][A-Z0-9.]{0,6})(?![A-Za-z0-9])")
_TICKER_STOP = {"AI", "API", "CIK", "CPI", "ETF", "FED", "FMP", "GDP", "IPO", "NYSE", "SEC", "US", "USD", "UTC"}
_CIK = re.compile(r"\b(?:CIK|filer[_ -]?cik)\s*[:#]?\s*(\d{6,10})\b", re.I)
_LIMIT = re.compile(r"\b(?:top|first|latest|last|show|list)\s+(\d{1,2})\b", re.I)
_RANGE = re.compile(r"\b(1h|4h|24h|7d|30d|1y)\b", re.I)
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_EQUITY = re.compile(r"\b(?:stock|stocks|shares?|equities|equity|company|companies|nasdaq|nyse|s&p|dow)\b", re.I)


@dataclass(frozen=True)
class Endpoint:
    name: str
    path: str
    capabilities: tuple[str, ...]
    pattern: str
    ticker: bool = False
    source: str = "financialdatasets.ai / FMP via equities-data"
    note: str = ""
    priority: float = 12


# One definition per unique read endpoint in the collection. Variants such as
# single/multi-ticker and date/range filters are parameters, not more tools.
ENDPOINTS = (
    Endpoint("equities_market_news", "/news/market", ("news", "web_research", "equity_research"), r"\b(?:stock|equity|equities|market|wall street)\b.{0,45}\b(?:news|headlines|stories)\b|\b(?:news|headlines)\b.{0,45}\b(?:stocks?|equities|market)\b", source="financialdatasets.ai"),
    Endpoint("equities_company_news", "/news/company", ("news", "equity_research", "web_research"), r"\b(?:news|headlines|stories|catalysts?|announcements?)\b", ticker=True, source="financialdatasets.ai"),
    Endpoint("equities_earnings_history", "/earnings", ("finance_data", "equity_research"), r"\b(?:earnings|eps|reported results|quarterly results)\b", ticker=True, source="financialdatasets.ai", note="Historical reported results, not the forward earnings calendar."),
    Endpoint("equities_earnings_feed", "/earnings/feed", ("finance_data", "equity_research"), r"\b(?:earnings|quarterly results)\b.{0,35}\b(?:feed|latest|recent|market|today)\b|\b(?:latest|recent)\b.{0,25}\bearnings\b", source="financialdatasets.ai", note="Market-wide feed may be restricted to the gateway's stock allowlist."),
    Endpoint("equities_earnings_upcoming", "/earnings/upcoming", ("finance_data", "equity_research", "listing_events"), r"\b(?:upcoming|next|future|when|calendar|scheduled)\b.{0,35}\b(?:earnings|reports?|results?)\b|\bearnings\b.{0,35}\b(?:upcoming|next|calendar|date)\b", source="FMP", note="Forward calendar; a scheduled date can change."),
    Endpoint("equities_insider_trades", "/insider-trades", ("finance_data", "equity_research"), r"\b(?:insider|form 4|executive|director)\b.{0,45}\b(?:trad(?:e|es|ing)|buy|buys|bought|sell|sells|sold|sales?|transactions?)\b|\binsider\s+(?:activity|transactions?)\b", source="FMP", note="Cached, allowlist-filtered recent Form 4 feed; an insider sale alone does not establish motive."),
    Endpoint("equities_price_history", "/prices/historical", ("market_data", "finance_data", "equity_research"), r"\b(?:historical|history|daily|chart|ohlcv|price over|price since|past prices?)\b", ticker=True, source="financialdatasets.ai", note="Daily bars only; this endpoint does not support intraday intervals."),
    Endpoint("equities_price_snapshot", "/prices/snapshot", ("market_data", "finance_data", "equity_research"), r"\b(?:price|quote|snapshot|trading at|share price)\b", ticker=True, source="financialdatasets.ai", note="Check the response's own timestamp; this is not an executable venue quote."),
    Endpoint("equities_ratings", "/ratings", ("finance_data", "equity_research"), r"\b(?:analysts?|ratings?|upgrades?|downgrades?|price targets?)\b", source="FMP", note="Ratings are analyst opinions, not realized returns."),
    Endpoint("equities_ratings_consensus", "/ratings/consensus", ("finance_data", "equity_research"), r"\b(?:consensus|buy.*/hold.*/sell|analyst breakdown)\b", ticker=True, source="FMP", note="Buy/hold/sell counts are analyst opinions."),
    Endpoint("equities_most_active", "/unusual-volume", ("market_data", "finance_data", "equity_research"), r"\b(?:most active|unusual volume|volume leaders|highest volume)\b", source="FMP", note="This is FMP's most-active list filtered to the gateway allowlist, not volume divided by historical average."),
    Endpoint("equities_interest_rates", "/macro/interest-rates", ("finance_data", "market_data"), r"\b(?:interest rates?|policy rates?|fed funds|federal funds)\b", source="financialdatasets.ai", note="Historical/current rates, not a calendar of future decisions."),
    Endpoint("equities_inflation", "/macro/inflation", ("finance_data", "market_data"), r"\b(?:inflation|consumer price index|\bcpi\b)\b", source="financialdatasets.ai", note="CPI series, not a future release calendar."),
    Endpoint("equities_yield_curve", "/macro/yield-curve", ("finance_data", "market_data"), r"\b(?:yield curve|treasury curve|term structure)\b", source="financialdatasets.ai", note="Current yield-curve snapshot, not a forward event calendar."),
    Endpoint("equities_filings", "/filings", ("finance_data", "equity_research"), r"\b(?:sec filings?|10-k|10-q|8-k|annual report|quarterly filing)\b", source="financialdatasets.ai", note="Company filings; inspect the filing date and link for material claims."),
    Endpoint("equities_filings_feed", "/filings/feed", ("finance_data", "equity_research"), r"\b(?:sec filings?|10-k|10-q|8-k|filings?)\b.{0,35}\b(?:latest|recent|feed|market|today)\b|\b(?:latest|recent)\b.{0,25}\bfilings?\b", source="FMP", note="Cached, allowlist-filtered market feed."),
    Endpoint("equities_institutional_holdings", "/institutional-holdings", ("finance_data", "equity_research"), r"\b(?:institutional|13f|fund holdings?|investor holdings?|berkshire|vanguard|blackrock)\b", source="financialdatasets.ai", note="13F position snapshots, not computed buy/sell flows; filings lag the holdings date."),
    Endpoint("equities_popular_investors", "/institutional-holdings/popular-investors", ("finance_data", "equity_research"), r"\b(?:popular|notable|top|famous)\b.{0,25}\b(?:institutional )?investors?\b", source="financialdatasets.ai", note="A curated investor list, not a ranking by returns."),
    Endpoint("equities_ticker_details", "/ticker-details", ("finance_data", "equity_research"), r"\b(?:company profile|ticker details|stock details|company overview|fundamentals|valuation|market cap|business overview)\b", ticker=True, source="financialdatasets.ai / FMP", note="Combined best-effort view; absent metrics are unknown, not zero."),
    Endpoint("equities_venue_movers", "/markets/movers", ("market_data", "token_discovery"), r"\b(?:aster|hyperliquid)\b.{0,50}\b(?:gainers|losers|movers|trending|most active)\b|\b(?:gainers|losers|movers|trending)\b.{0,50}\b(?:aster|hyperliquid)\b", source="equities-data market gateway", note="Venue pairs, including tokenized-stock perps; not underlying equity shares.", priority=5),
)
BY_NAME = {item.name: item for item in ENDPOINTS}


def enabled() -> bool:
    return bool(settings.equities_data_base_url and settings.equities_data_internal_token)


def _request(method: str, path: str, *, params: dict | None = None, body: dict | None = None,
             user_jwt: str | None = None, authenticated: bool = True) -> dict:
    if not settings.equities_data_base_url:
        raise RuntimeError("Equities-data base URL is not configured")
    if authenticated and not settings.equities_data_internal_token:
        raise RuntimeError("Equities-data internal token is not configured")
    headers = {"Accept": "application/json", "User-Agent": "AnvayaBackend/1.0"}
    if authenticated:
        headers["X-Internal-Token"] = settings.equities_data_internal_token
    if user_jwt:
        headers["Authorization"] = f"Bearer {user_jwt}"
    # All paths are fixed here or from the private endpoint catalog. Never
    # allow a user query to become an arbitrary path or URL.
    url = settings.equities_data_base_url.rstrip("/") + path
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        response = client.request(method, url, params=params, json=body, headers=headers)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        if path == "/health":
            return {"status": response.text.strip() or "ok"}
        raise RuntimeError("Equities-data returned non-JSON data") from None
    if not isinstance(data, dict):
        raise RuntimeError("Equities-data returned an unexpected response")
    return data


def health() -> dict:
    return _request("GET", "/health", authenticated=False)


def watchlist_add(ticker: str, user_jwt: str, source: str | None = None) -> dict:
    return _request("POST", "/api/v1/watchlist", body={"ticker": ticker, **({"source": source} if source else {})}, user_jwt=user_jwt)


def watchlist_remove(ticker: str, user_jwt: str) -> dict:
    return _request("DELETE", "/api/v1/watchlist", params={"ticker": ticker}, user_jwt=user_jwt)


def watchlist_list(user_jwt: str, source: str | None = None) -> dict:
    return _request("GET", "/api/v1/watchlist", params={"source": source} if source else None, user_jwt=user_jwt)


def cache_delete(key: str) -> dict:
    """Administrative operation for an operator; intentionally not a chat tool."""
    return _request("DELETE", _ROOT + "/cache", params={"key": key})


def tickers_in(request: str) -> list[str]:
    found = equity_instruments(request)
    for match in _TICKER.finditer(request):
        ticker = match.group(1)
        if ticker not in _TICKER_STOP and len(ticker) >= 2 and ticker not in found:
            found.append(ticker)
    return found[:5]


def _limit(request: str) -> int:
    match = _LIMIT.search(request)
    return min(25, max(1, int(match.group(1)))) if match else 10


def _params(endpoint: Endpoint, request: str) -> dict[str, Any]:
    names = tickers_in(request)
    ticker = names[0] if names else None
    if endpoint.ticker and not ticker:
        raise ValueError("Name a stock ticker for this lookup")
    key = endpoint.name
    p: dict[str, Any] = {}
    if key in {"equities_company_news", "equities_earnings_history", "equities_ratings"} and names:
        p["tickers"] = ",".join(names)
    elif key in {"equities_price_history", "equities_price_snapshot"} and names:
        p["symbols"] = ",".join(names)
    elif key in {"equities_insider_trades", "equities_filings", "equities_filings_feed", "equities_ratings_consensus", "equities_ticker_details"} and ticker:
        p["ticker"] = ticker
    elif key == "equities_institutional_holdings":
        cik = _CIK.search(request)
        if cik:
            p["filer_cik"] = cik.group(1)
        elif ticker:
            p["ticker"] = ticker
        else:
            raise ValueError("Name a stock ticker or a filer CIK for institutional holdings")
    if key == "equities_price_history":
        if re.search(r"\b(?:intraday|minute|hourly|1m|5m|15m|30m|1h)\b", request, re.I):
            raise ValueError("The equities-data historical-prices endpoint supports daily bars only")
        p["interval"] = "day"  # the documented upstream does not support intraday
    if key == "equities_filings":
        cik = _CIK.search(request)
        if cik:
            p["cik"] = cik.group(1)
        filing_type = re.search(r"\b(10-K|10-Q|8-K)\b", request, re.I)
        if filing_type:
            p["filing_type"] = filing_type.group(1).upper()
        if not (p.get("ticker") or p.get("cik")):
            raise ValueError("Name a ticker or CIK for company filings")
    if key == "equities_interest_rates":
        p["bank"] = "FED"
    if key == "equities_inflation":
        p["series"] = "cpi_all_sa"
    if key == "equities_venue_movers":
        venue = re.search(r"\b(aster|hyperliquid)\b", request, re.I)
        if not venue:
            raise ValueError("Name Aster or Hyperliquid for a venue ranking")
        p["dex"] = venue.group(1).lower()
        p["tab"] = "losers" if re.search(r"\blosers?\b", request, re.I) else "trending" if re.search(r"\btrending\b", request, re.I) else "gainers"
        if re.search(r"\b(?:stocks?|equities|tokenized shares?)\b", request, re.I):
            p["category"] = "stock"
        elif re.search(r"\b(?:crypto only|coins? only|exclude stocks?)\b", request, re.I):
            p["category"] = "crypto"
    if key not in {"equities_price_snapshot", "equities_price_history", "equities_ratings_consensus", "equities_ticker_details", "equities_yield_curve", "equities_popular_investors", "equities_interest_rates", "equities_inflation"}:
        p["limit" if key not in {"equities_market_news", "equities_company_news"} else "pageSize"] = _limit(request)
    if key in {"equities_market_news", "equities_company_news", "equities_earnings_history", "equities_earnings_feed", "equities_ratings", "equities_price_history", "equities_venue_movers"}:
        period = _RANGE.search(request)
        if period:
            p["range"] = period.group(1).lower()
    dates = _DATE.findall(request)
    if len(dates) >= 2:
        if key in {"equities_market_news", "equities_company_news"}:
            p.update(dateFrom=dates[0], dateTo=dates[1])
        elif key in {"equities_earnings_history", "equities_earnings_feed", "equities_earnings_upcoming", "equities_interest_rates", "equities_inflation"}:
            p.update(date_from=dates[0], date_to=dates[1])
        elif key == "equities_price_history":
            p.update({"from": dates[0], "to": dates[1]})
    return p


def _has_records(data: dict) -> bool:
    if data.get("status") == "not_ready" or (isinstance(data.get("data"), dict) and data["data"].get("status") == "not_ready"):
        return False
    value = data.get("data", data)
    if value is None or value == []:
        return False
    if isinstance(value, dict) and not value:
        return False
    return True


def _bounded(value: Any, rows: int) -> Any:
    """Keep provider evidence valid JSON within the chat card's size budget."""
    if isinstance(value, list):
        return [_bounded(item, rows) for item in value[:rows]]
    if isinstance(value, dict):
        return {key: _bounded(item, rows) for key, item in list(value.items())[:35]}
    if isinstance(value, str) and len(value) > 700:
        return value[:700] + "…"
    return value


def run(name: str, request: str) -> str:
    endpoint = BY_NAME[name]
    params = _params(endpoint, request)
    data = _request("GET", _ROOT + endpoint.path, params=params)
    if not _has_records(data):
        raise NoData(f"No {name.replace('_', ' ')} data is available for this request")
    if name in {"equities_interest_rates", "equities_inflation"} and isinstance(data.get("data"), dict):
        series_key = "interest_rates" if name == "equities_interest_rates" else "inflation"
        rows = data["data"].get(series_key)
        if isinstance(rows, list):
            # The rates gateway returns its full history oldest-first (1954
            # first in a live 2026 call). A bounded card must show the newest
            # observations, never the first five legacy records as "current".
            data = {**data, "data": {**data["data"], series_key: sorted(rows, key=lambda row: str(row.get("date") or "") if isinstance(row, dict) else "", reverse=True)}}
    # Preserve the gateway's own timestamps, pagination, links and units. The
    # Postman collection has no response schemas, so guessing field names here
    # would quietly drop evidence. Bound the card rather than rewriting it.
    payload = ""
    shown_rows = 0
    for row_count in (5, 3, 1):
        candidate = json.dumps(_bounded(data, row_count), ensure_ascii=False, default=str)
        if len(candidate) <= 8500:
            payload, shown_rows = candidate, row_count
            break
    if not payload:
        payload = json.dumps({"data_keys": list((data.get("data") or {}).keys()) if isinstance(data.get("data"), dict) else list(data.keys()),
                              "notice": "Response too large for a chat card; narrow the ticker or limit."})
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"### {name.removeprefix('equities_').replace('_', ' ').title()}",
             f"Gateway: equities-data · Upstream: {endpoint.source} · Retrieved: {stamp}",
             f"Endpoint: `{endpoint.path}` · Parameters: `{json.dumps(params, sort_keys=True)}`"]
    if name in {"equities_interest_rates", "equities_inflation", "equities_yield_curve"}:
        kind = {"equities_interest_rates": "interest_rates", "equities_inflation": "inflation", "equities_yield_curve": "yield_curve"}[name]
        body = data.get("data") or {}
        value = body.get(kind) if isinstance(body, dict) else None
        latest = value[0].get("date") if isinstance(value, list) and value and isinstance(value[0], dict) else value.get("date") if isinstance(value, dict) else None
        if latest:
            lines.append(f"Latest observation in this response: **{latest}**. Retrieval time is not the observation date.")
    if endpoint.note:
        lines.append(endpoint.note)
    if shown_rows < 5:
        lines.append(f"Showing at most {shown_rows} rows from each list; ask for a narrower result for more detail.")
    if key := ("direction-derived bullish/bearish/neutral is not sentiment analysis" if name in {
        "equities_market_news", "equities_earnings_feed", "equities_filings_feed", "equities_ratings",
        "equities_insider_trades", "equities_institutional_holdings", "equities_most_active"} else ""):
        lines.append("Data-label caveat: " + key + ".")
    if name in {"equities_market_news", "equities_company_news", "equities_earnings_feed", "equities_filings_feed"}:
        lines.append("Price-at-release enrichment may use a tokenized-stock venue pair; verify its instrument before comparing it with a cash share price.")
    lines.extend(["", "```json", payload, "```"])
    return compact_tool_result("\n".join(lines))


def _matches(endpoint: Endpoint, request: str) -> bool:
    if not re.search(endpoint.pattern, request, re.I):
        return False
    if endpoint.name != "equities_venue_movers" and re.search(r"\b(?:aster|hyperliquid)\b", request, re.I):
        # The named object is a venue pair/perp unless the user explicitly
        # asks about its underlying listed company or cash shares.
        if not re.search(r"\b(?:underlying|listed company|cash shares?|company fundamentals|company earnings)\b", request, re.I):
            return False
    if endpoint.name in {"equities_company_news", "equities_price_history", "equities_price_snapshot", "equities_ticker_details"}:
        # A bare crypto ticker also looks like an uppercase stock symbol.
        # Require either a known equity alias or explicit equity context for
        # these otherwise generic news/price/profile words.
        if not (equity_instruments(request) or _EQUITY.search(request)):
            return False
    if endpoint.ticker and not tickers_in(request):
        return False
    if endpoint.name == "equities_earnings_history" and re.search(r"\b(?:upcoming|next|when|calendar|scheduled)\b", request, re.I):
        return False
    if endpoint.name == "equities_ratings" and re.search(r"\bconsensus\b", request, re.I) and tickers_in(request):
        return False
    if endpoint.name == "equities_filings" and not (tickers_in(request) or _CIK.search(request)):
        return False
    if endpoint.name == "equities_filings" and re.search(r"\b(?:feed|market.wide|latest filings|recent filings)\b", request, re.I):
        return False
    if endpoint.name == "equities_institutional_holdings" and re.search(r"\b(?:popular|notable|famous)\s+investors?\b", request, re.I):
        return False
    if endpoint.name == "equities_venue_movers" and re.search(r"\b(?:funding|open interest|positions?|liquidations?)\b", request, re.I):
        return False
    if endpoint.name in {"equities_market_news", "equities_earnings_feed", "equities_filings_feed"} and not tickers_in(request):
        return True
    if endpoint.name == "equities_market_news" and tickers_in(request):
        return False
    return True


def matching_tools(request: str) -> list[str]:
    """Focused equity evidence for the equity-research node, in fit order."""
    return [e.name for e in ENDPOINTS if e.name != "equities_venue_movers" and _matches(e, request)]


class EquitiesDataProvider:
    name = "equities_data"

    def register(self, router: ProviderRouter) -> None:
        for endpoint in ENDPOINTS:
            router.register(ProviderTool(
                endpoint.name, self.name, endpoint.capabilities, partial(run, endpoint.name),
                enabled=enabled, matches=partial(_matches, endpoint),
                keywords=tuple(endpoint.name.removeprefix("equities_").split("_")),
                cache_ttl_seconds=60 if endpoint.name in {"equities_price_snapshot", "equities_venue_movers"} else 300,
                priority=endpoint.priority,
                description=f"{endpoint.source}: {endpoint.path}. {endpoint.note}",
            ))

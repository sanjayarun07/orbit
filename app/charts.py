"""Chart cards: which TradingView symbol to draw for what an answer is about.

An answer that resolved a token (a price, a security dossier, a deep dive)
or researched an equity gets a chart card, rendered in the browser by
TradingView's embeddable Advanced Chart widget (free, no account, attributed
per TradingView's widget terms). The only work here is naming the symbol the
widget should show: an exchange-qualified spot pair for a coin, the listing
for a stock. TradingView's public symbol search answers that; when it cannot
be reached the widget gets TradingView's own aggregate index for the coin,
or the bare ticker for a stock, both of which it resolves itself.
"""
from __future__ import annotations

import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

_SEARCH = "https://symbol-search.tradingview.com/symbol_search/v3/"
_HEADERS = {"Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"}
_TTL_SECONDS = 6 * 3600
_cache: dict[tuple[str, str], tuple[float, dict]] = {}

# Where a coin's chart is best drawn, most liquid first. Solana DEX venues are
# last: TradingView lists them, but their history is short and gappy.
_EXCHANGE_ORDER = ("BINANCE", "COINBASE", "BYBIT", "OKX", "KRAKEN", "KUCOIN", "GATEIO", "MEXC", "BITGET", "HTX", "BITSTAMP",
                   "RAYDIUM", "ORCA", "METEORA", "JUPITER", "UNISWAP3ETH", "UNISWAP")
_QUOTES = ("USDT", "USD", "USDC")
_STOCK_EXCHANGES = ("NASDAQ", "NYSE", "AMEX", "NSE", "BSE", "LSE", "TSX", "HKEX", "TSE", "XETR", "EURONEXT")
_TICKER = re.compile(r"\$([A-Za-z]{1,6})\b|\b([A-Z]{2,6})\b")
_TICKER_STOP = {"AI", "ETF", "ETFS", "USD", "USDT", "USDC", "IPO", "CEO", "CFO", "SEC", "FED", "GDP", "CPI", "PE", "EPS", "YTD", "ATH", "OK", "US", "UK", "EU",
                "NYSE", "NASDAQ", "BUY", "SELL", "HOLD", "LONG", "SHORT", "NFT", "DEFI", "DEX", "CEX", "TVL", "APY", "APR", "RSI", "MACD", "EMA", "SMA", "ATR", "VWAP"}


def _search(text: str, search_type: str) -> list[dict]:
    response = httpx.get(_SEARCH, params={"text": text, "hl": 0, "exchange": "", "lang": "en", "search_type": search_type, "domain": "production"},
                         headers=_HEADERS, timeout=4.0)
    response.raise_for_status()
    return list(response.json().get("symbols") or [])


def _cached(key: tuple[str, str], compute) -> dict | None:
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = compute()
    if value is not None:
        _cache[key] = (now + _TTL_SECONDS, value)
    return value


def _exchange_rank(exchange: str) -> int:
    try:
        return _EXCHANGE_ORDER.index(exchange.upper())
    except ValueError:
        return len(_EXCHANGE_ORDER)


def crypto_symbol(symbol: str) -> dict:
    """{symbol, label} for a coin: its most liquid USD-quoted spot pair."""
    sym = (symbol or "").strip().lstrip("$").upper()
    fallback = {"symbol": f"CRYPTO:{sym}USD", "label": f"{sym} / USD"}
    if not sym:
        return fallback

    def compute():
        try:
            found = _search(sym, "crypto")
        except Exception:
            logger.debug("tradingview symbol search failed for %s", sym, exc_info=True)
            return None
        wanted = {f"{sym}{quote}" for quote in _QUOTES}
        spots = [s for s in found if s.get("type") == "spot" and str(s.get("symbol", "")).upper() in wanted]
        if not spots:
            return fallback
        best = min(spots, key=lambda s: (_exchange_rank(str(s.get("exchange", ""))), _QUOTES.index(str(s["symbol"]).upper().replace(sym, "", 1)) if str(s["symbol"]).upper().replace(sym, "", 1) in _QUOTES else 9))
        exchange = str(best.get("exchange", "")).upper()
        quote = str(best["symbol"]).upper().replace(sym, "", 1)
        return {"symbol": f"{exchange}:{best['symbol']}", "label": f"{sym} / {quote} · {exchange.title()}"}

    return _cached(("crypto", sym), compute) or fallback


def equity_symbol(ticker: str) -> dict:
    """{symbol, label} for a stock: its primary listing when the search knows it."""
    tick = (ticker or "").strip().lstrip("$").upper()
    fallback = {"symbol": tick, "label": tick}
    if not tick:
        return fallback

    def compute():
        try:
            found = _search(tick, "stock")
        except Exception:
            logger.debug("tradingview symbol search failed for %s", tick, exc_info=True)
            return None
        stocks = [s for s in found if s.get("type") == "stock" and str(s.get("symbol", "")).upper() == tick]
        if not stocks:
            return fallback
        best = min(stocks, key=lambda s: (_STOCK_EXCHANGES.index(str(s.get("exchange", "")).upper()) if str(s.get("exchange", "")).upper() in _STOCK_EXCHANGES else len(_STOCK_EXCHANGES)))
        exchange = str(best.get("exchange", "")).upper()
        description = re.sub(r"<[^>]+>", "", str(best.get("description") or tick))
        return {"symbol": f"{exchange}:{tick}", "label": f"{description} · {exchange}"}

    return _cached(("stock", tick), compute) or fallback


def equity_ticker(request: str) -> str | None:
    """The stock ticker an equity ask names, or None: `$AAPL`, or a bare
    upper-case word that is not market jargon. Company names are left to the
    research path; a chart needs a ticker it can trust."""
    for dollar, bare in _TICKER.findall(request or ""):
        tick = (dollar or bare).upper()
        if tick and tick not in _TICKER_STOP:
            return tick
    return None


def chart_for(run, request: str) -> dict | None:
    """The chart card for a finished research turn, or None."""
    if getattr(run, "intent", None) != "research":
        return None
    resolved = getattr(run, "resolved_token", None) or {}
    symbol = (resolved.get("symbol") or "").strip() if isinstance(resolved, dict) else ""
    if symbol:
        return {**crypto_symbol(symbol), "interval": "60", "kind": "crypto"}
    if "equity_research" in set(getattr(run, "capabilities", None) or ()):
        tick = equity_ticker(request)
        if tick:
            return {**equity_symbol(tick), "interval": "D", "kind": "equity"}
    return None

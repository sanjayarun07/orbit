"""TradingView, on the user's own account.

TradingView's MCP server (https://www.tradingview.com/mcp/docs) answers live
quotes, technicals, fundamentals, forecasts, news, filings and calendars for
crypto and equities -- for a signed-in TradingView user, over OAuth 2.1.
There is no service credential, so Orbit never holds one login for everyone:
each Orbit user links their own TradingView account here, and the tools in
this module act with that user's token for the duration of their turn.

Three layers:

- OAuth client. Orbit registers itself once with TradingView's authorization
  server (dynamic client registration, RFC 7591), then runs the authorization
  code flow with PKCE and the MCP resource indicator per user, refreshing the
  access token when it is about to expire and revoking it on disconnect.
  Tokens live in `user_integrations`, keyed by user and provider.
- MCP calls. One short-lived Streamable HTTP session per call, carrying the
  user's bearer token. The tool names and arguments are TradingView's
  documented ones.
- Turn context. The chat turn binds the user's token to a ContextVar; the
  provider-router tools below match only while a token is bound, so a user
  without a TradingView connection never sees them chosen and their turn is
  exactly what it was before this module existed.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from app.db import get_pg_pool, get_redis
from app.settings import settings

logger = logging.getLogger(__name__)

PROVIDER = "tradingview"
SCOPE = "mcp:read mcp:tools"
_PENDING_TTL = 600

# The token bound to the turn being served (None: no TradingView connection).
current_token: ContextVar[str | None] = ContextVar("tradingview_token", default=None)

# In-memory fallbacks when Postgres / Redis are absent (development, tests).
_clients: dict[str, dict] = {}
_tokens: dict[str, dict] = {}
_pending: dict[str, tuple[float, dict]] = {}
_metadata: dict | None = None


class TradingViewError(Exception):
    """A connector step failed in a way the user can act on."""


def enabled() -> bool:
    return bool(settings.tradingview_enabled)


def redirect_uri() -> str:
    return settings.public_base_url.rstrip("/") + "/integrations/tradingview/callback"


# ----------------------------------------------------------------------------
# HTTP (patched in tests)
# ----------------------------------------------------------------------------

async def _get_json(url: str) -> dict:
    """One discovery document. TradingView's metadata endpoint answered a
    click with a read timeout once (2026-09-18) and fine a minute later, so
    a slow first answer gets a second try before the user sees an error."""
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
                response = await client.get(url, headers={"Accept": "application/json"})
                response.raise_for_status()
                return response.json()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            logger.info("TradingView discovery %s attempt %d failed: %s", url, attempt, type(exc).__name__)
    raise TradingViewError(f"TradingView's discovery document at {url} did not answer in time ({type(last).__name__}).") from last


async def _post_json(url: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, json=payload, headers={"Accept": "application/json"})
        if response.status_code >= 400:
            raise TradingViewError(f"{url} answered {response.status_code}: {response.text[:200]}")
        return response.json()


async def _post_form(url: str, data: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, data=data, headers={"Accept": "application/json"})
        if response.status_code >= 400:
            raise TradingViewError(f"{url} answered {response.status_code}: {response.text[:200]}")
        return response.json()


# ----------------------------------------------------------------------------
# Discovery and client registration
# ----------------------------------------------------------------------------

async def metadata() -> dict:
    """The authorization server's metadata, found from the MCP server's
    protected-resource document (RFC 9728) and cached for the process."""
    global _metadata
    if _metadata is not None:
        return _metadata
    parts = urlsplit(settings.tradingview_mcp_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    # RFC 9728 path-based metadata for the /mcp resource (the server names
    # this location in its WWW-Authenticate challenge). Two turns discovering
    # at once both fetch it; the result is the same document.
    resource = await _get_json(f"{origin}/.well-known/oauth-protected-resource{parts.path.rstrip('/') or '/mcp'}")
    issuer = (resource.get("authorization_servers") or [origin])[0].rstrip("/")
    meta = await _get_json(f"{issuer}/.well-known/oauth-authorization-server")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if not meta.get(key):
            raise TradingViewError(f"TradingView's authorization server does not advertise {key}")
    _metadata = meta
    return meta


async def warm() -> None:
    """Fetch the authorization-server metadata ahead of the first click."""
    if not enabled():
        return
    try:
        await metadata()
    except Exception:
        logger.info("TradingView metadata warm-up skipped", exc_info=True)


async def _load_client() -> dict | None:
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow("SELECT client_id, client_secret, redirect_uri FROM oauth_clients WHERE provider = $1", PROVIDER)
        return dict(row) if row else None
    return _clients.get(PROVIDER)


async def _save_client(record: dict) -> None:
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute(
            "INSERT INTO oauth_clients (provider, client_id, client_secret, redirect_uri) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (provider) DO UPDATE SET client_id = EXCLUDED.client_id, client_secret = EXCLUDED.client_secret, redirect_uri = EXCLUDED.redirect_uri",
            PROVIDER, record["client_id"], record.get("client_secret"), record["redirect_uri"],
        )
    else:
        _clients[PROVIDER] = dict(record)


async def client() -> dict:
    """Orbit's OAuth client at TradingView: registered on first use, and
    re-registered when the public base URL (so the redirect URI) changed."""
    record = await _load_client()
    if record and record.get("redirect_uri") == redirect_uri():
        return record
    meta = await metadata()
    registered = await _post_json(meta["registration_endpoint"], {
        "client_name": "Orbit",
        "client_uri": settings.public_base_url,
        "redirect_uris": [redirect_uri()],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    })
    if not registered.get("client_id"):
        raise TradingViewError("TradingView did not return a client id for Orbit")
    record = {"client_id": registered["client_id"], "client_secret": registered.get("client_secret"), "redirect_uri": redirect_uri()}
    await _save_client(record)
    return record


# ----------------------------------------------------------------------------
# The authorization code flow, per user
# ----------------------------------------------------------------------------

def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


async def _remember_pending(state: str, payload: dict) -> None:
    redis = await get_redis()
    if redis is not None:
        await redis.set(f"tv:oauth:{state}", json.dumps(payload), ex=_PENDING_TTL)
        return
    now = time.time()
    for key, (expires, _) in list(_pending.items()):
        if expires < now:
            _pending.pop(key, None)
    _pending[state] = (now + _PENDING_TTL, payload)


async def _take_pending(state: str) -> dict | None:
    redis = await get_redis()
    if redis is not None:
        raw = await redis.getdel(f"tv:oauth:{state}")
        return json.loads(raw) if raw else None
    item = _pending.pop(state, None)
    if item is None or item[0] < time.time():
        return None
    return item[1]


async def begin(user_id: str) -> str:
    """The URL to send the user to. State and PKCE verifier are kept for ten
    minutes, bound to the user who started."""
    if not enabled():
        raise TradingViewError("The TradingView connection is turned off on this deployment")
    meta = await metadata()
    app_client = await client()
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    await _remember_pending(state, {"user_id": user_id, "verifier": verifier})
    params = {
        "response_type": "code",
        "client_id": app_client["client_id"],
        "redirect_uri": redirect_uri(),
        "scope": SCOPE,
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "resource": settings.tradingview_mcp_url,
    }
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


def _token_record(user_id: str, tokens: dict, previous: dict | None = None) -> dict:
    expires_in = tokens.get("expires_in")
    return {
        "user_id": user_id,
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token") or (previous or {}).get("refresh_token"),
        "expires_at": time.time() + float(expires_in) if expires_in else None,
        "scope": tokens.get("scope") or SCOPE,
        "connected_at": (previous or {}).get("connected_at") or datetime.now(timezone.utc).isoformat(),
    }


async def _save_tokens(record: dict) -> None:
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute(
            "INSERT INTO user_integrations (user_id, provider, access_token, refresh_token, expires_at, scope, connected_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (user_id, provider) DO UPDATE SET access_token = EXCLUDED.access_token, "
            "refresh_token = EXCLUDED.refresh_token, expires_at = EXCLUDED.expires_at, scope = EXCLUDED.scope",
            record["user_id"], PROVIDER, record["access_token"], record.get("refresh_token"), record.get("expires_at"), record.get("scope"),
            datetime.fromisoformat(record["connected_at"]),
        )
    else:
        _tokens[record["user_id"]] = dict(record)


async def _load_tokens(user_id: str) -> dict | None:
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow("SELECT * FROM user_integrations WHERE user_id = $1 AND provider = $2", user_id, PROVIDER)
        if not row:
            return None
        record = dict(row)
        record["user_id"] = str(record["user_id"])
        record["connected_at"] = record["connected_at"].isoformat() if record.get("connected_at") else None
        return record
    return _tokens.get(user_id)


async def _delete_tokens(user_id: str) -> None:
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute("DELETE FROM user_integrations WHERE user_id = $1 AND provider = $2", user_id, PROVIDER)
    else:
        _tokens.pop(user_id, None)


async def complete(state: str, code: str) -> str:
    """Exchange the code for tokens; returns the user id the flow belongs to."""
    pending = await _take_pending(state)
    if pending is None:
        raise TradingViewError("This sign-in link has expired or was already used. Start the connection again.")
    meta = await metadata()
    app_client = await client()
    tokens = await _post_form(meta["token_endpoint"], {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "client_id": app_client["client_id"],
        "code_verifier": pending["verifier"],
        "resource": settings.tradingview_mcp_url,
    })
    if not tokens.get("access_token"):
        raise TradingViewError("TradingView did not return an access token")
    await _save_tokens(_token_record(pending["user_id"], tokens))
    return pending["user_id"]


async def access_token(user_id: str) -> str | None:
    """The user's access token, refreshed when it is within a minute of
    expiry; None when there is no connection or the refresh failed (then the
    connection is dropped, so the user is asked to connect again)."""
    record = await _load_tokens(user_id)
    if record is None:
        return None
    expires_at = record.get("expires_at")
    if expires_at and expires_at - 60 > time.time():
        return record["access_token"]
    if not expires_at:
        return record["access_token"]
    if not record.get("refresh_token"):
        await _delete_tokens(user_id)
        return None
    try:
        meta = await metadata()
        app_client = await client()
        tokens = await _post_form(meta["token_endpoint"], {
            "grant_type": "refresh_token",
            "refresh_token": record["refresh_token"],
            "client_id": app_client["client_id"],
            "resource": settings.tradingview_mcp_url,
        })
        fresh = _token_record(user_id, tokens, previous=record)
    except Exception:
        logger.warning("TradingView token refresh failed for user %s; dropping the connection", user_id, exc_info=True)
        await _delete_tokens(user_id)
        return None
    await _save_tokens(fresh)
    return fresh["access_token"]


async def disconnect(user_id: str) -> bool:
    record = await _load_tokens(user_id)
    if record is None:
        return False
    try:
        meta = await metadata()
        endpoint = meta.get("revocation_endpoint")
        if endpoint:
            app_client = await client()
            for kind in ("refresh_token", "access_token"):
                token = record.get(kind)
                if token:
                    await _post_form(endpoint, {"token": token, "token_type_hint": kind, "client_id": app_client["client_id"]})
    except Exception:
        logger.info("TradingView revocation skipped for user %s", user_id, exc_info=True)
    await _delete_tokens(user_id)
    return True


async def status(user_id: str) -> dict:
    record = await _load_tokens(user_id)
    return {"connected": record is not None, "connected_at": (record or {}).get("connected_at")}


async def bind_turn(user_id: str | None):
    """Bind the user's token to the turn being served. Returns the ContextVar
    token to reset with; binds None when the user has no connection."""
    token = None
    if user_id and enabled():
        try:
            token = await access_token(user_id)
        except Exception:
            logger.warning("TradingView token lookup failed for user %s", user_id, exc_info=True)
    return current_token.set(token)


def available() -> bool:
    """Whether the turn being served may use TradingView."""
    return enabled() and bool(current_token.get())


# ----------------------------------------------------------------------------
# MCP calls
# ----------------------------------------------------------------------------

async def _call_with(token: str, name: str, arguments: dict) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    timeout = httpx.Timeout(connect=10, read=settings.mcp_call_timeout_seconds, write=10, pool=10)
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=timeout) as http:
        async with streamable_http_client(settings.tradingview_mcp_url, http_client=http) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    if getattr(result, "isError", False):
        raise TradingViewError(f"TradingView {name} failed: {' '.join(parts)[:300]}")
    return "\n".join(parts)


async def call_tool(name: str, arguments: dict, *, token: str | None = None) -> str:
    """Run one TradingView MCP tool with the turn's token (or the given one)."""
    token = token or current_token.get()
    if not token:
        raise TradingViewError("No TradingView connection for this turn")
    return await _call_with(token, name, arguments)


def call_tool_sync(name: str, arguments: dict) -> str:
    """For provider-router handlers, which run in worker threads with no loop."""
    token = current_token.get()
    if not token:
        raise TradingViewError("No TradingView connection for this turn")
    return asyncio.run(_call_with(token, name, arguments))


# ----------------------------------------------------------------------------
# Research helpers: symbol resolution and evidence cards
# ----------------------------------------------------------------------------

_TICKER = re.compile(r"\$([A-Za-z]{1,10})\b|\b([A-Z]{2,10})\b")
_STOP = {"AI", "ETF", "ETFS", "USD", "USDT", "USDC", "IPO", "CEO", "CFO", "SEC", "FED", "GDP", "CPI", "PE", "EPS", "YTD", "ATH", "OK", "US", "UK", "EU",
         "NYSE", "NASDAQ", "BUY", "SELL", "HOLD", "LONG", "SHORT", "NFT", "DEFI", "DEX", "CEX", "TVL", "APY", "APR", "RSI", "MACD", "EMA", "SMA", "ATR", "VWAP", "TV"}
_MARKET_WORDS = re.compile(r"\b(?:price|quote|technicals?|rsi|macd|moving\s+average|chart|volume|market\s*cap|fundamentals?|p/e|valuation|earnings|revenue|analyst|forecast|target|news|dividend|filings?|10-k|10-q)\b", re.I)


def ticker_in(request: str) -> str | None:
    for dollar, bare in _TICKER.findall(request or ""):
        tick = (dollar or bare).upper()
        if tick not in _STOP:
            return tick
    return None


def _first_symbol(raw: str, hint: str) -> str | None:
    """The routable symbol out of search_symbols' answer: exchange-qualified
    when the payload gives one, else the first `EXCHANGE:SYMBOL` in the text."""
    try:
        data = json.loads(raw)
    except Exception:
        data = None
    items = data.get("symbols") if isinstance(data, dict) else (data if isinstance(data, list) else None)
    if items:
        for item in items:
            if not isinstance(item, dict):
                continue
            sym = item.get("symbol") or item.get("ticker")
            exch = item.get("exchange") or item.get("source")
            if sym and ":" in str(sym):
                return str(sym)
            if sym and exch:
                return f"{exch}:{sym}"
    match = re.search(r"\b([A-Z0-9_]{2,16}:[A-Z0-9._-]{1,20})\b", raw or "")
    if match:
        return match.group(1)
    return None


def resolve_symbol(query: str, kind: str = "all") -> str | None:
    """TradingView's routable symbol for a ticker or name, on the user's token."""
    try:
        raw = call_tool_sync("search_symbols", {"query": query, "type_filter": kind})
    except Exception:
        logger.debug("TradingView search_symbols failed for %r", query, exc_info=True)
        return None
    return _first_symbol(raw, query)


def _card(title: str, symbol: str, sections: list[tuple[str, str]]) -> str:
    lines = [f"# {title}", f"**Provider**: TradingView (your account) · **Symbol**: {symbol} · **As of**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]
    for heading, body in sections:
        if body and body.strip():
            lines += [f"## {heading}", body.strip()[: settings.mcp_result_max_chars // max(1, len(sections))], ""]
    lines.append("Source: TradingView · https://www.tradingview.com/")
    return "\n".join(lines)


def _subject(request: str, kind: str) -> str | None:
    tick = ticker_in(request)
    return resolve_symbol(tick or request, kind)


def snapshot(request: str) -> str:
    """Quote, key stats and the technical-indicator rating for the symbol named."""
    symbol = _subject(request, "all")
    if not symbol:
        raise TradingViewError("TradingView could not resolve a symbol from the request")
    quote = call_tool_sync("get_symbol_data", {"symbol": symbol, "columns": [
        "close", "change", "change_abs", "volume", "market_cap_basic", "high", "low", "open", "price_52_week_high", "price_52_week_low",
        "Recommend.All", "RSI", "average_volume_10d_calc", "price_earnings_ttm", "earnings_per_share_basic_ttm", "sector", "industry", "description"]})
    technicals = ""
    try:
        technicals = call_tool_sync("get_technicals_rating", {"symbol": symbol, "interval": "1D"})
    except Exception:
        logger.debug("TradingView technicals failed for %s", symbol, exc_info=True)
    return _card("TradingView snapshot", symbol, [("Quote and key stats", quote), ("Technical rating (1D)", technicals)])


def financials(request: str) -> str:
    """Fundamentals and analyst forecasts for the equity named."""
    symbol = _subject(request, "stock")
    if not symbol:
        raise TradingViewError("TradingView could not resolve an equity from the request")
    fundamentals = call_tool_sync("get_financials", {"symbol": symbol, "period": "ttm"})
    forecasts = ""
    try:
        forecasts = call_tool_sync("get_forecasts", {"symbol": symbol})
    except Exception:
        logger.debug("TradingView forecasts failed for %s", symbol, exc_info=True)
    return _card("TradingView fundamentals", symbol, [("Financial snapshot (TTM)", fundamentals), ("Analyst consensus and targets", forecasts)])


def news(request: str) -> str:
    symbol = _subject(request, "all")
    if not symbol:
        raise TradingViewError("TradingView could not resolve a symbol from the request")
    headlines = call_tool_sync("get_news", {"symbol": symbol, "lang": "en", "limit": 12})
    return _card("TradingView news", symbol, [("Latest headlines", headlines)])


def earnings(request: str) -> str:
    symbol = _subject(request, "stock")
    if not symbol:
        raise TradingViewError("TradingView could not resolve an equity from the request")
    calendar = call_tool_sync("get_earnings_calendar", {"symbols": [symbol]})
    return _card("TradingView earnings calendar", symbol, [("Upcoming and recent earnings", calendar)])


def matches_market(request: str) -> bool:
    return available() and bool(ticker_in(request)) and bool(_MARKET_WORDS.search(request or ""))


def matches_equity(request: str) -> bool:
    return available() and bool(ticker_in(request)) and bool(re.search(r"\b(?:fundamentals?|p/e|valuation|earnings|revenue|margins?|analysts?|forecast|price\s+target|research|stock|shares?|equity)\b", request or "", re.I))


def matches_news(request: str) -> bool:
    return available() and bool(ticker_in(request)) and bool(re.search(r"\b(?:news|headlines?|why\s+is|what\s+happened|latest)\b", request or "", re.I))


def matches_earnings(request: str) -> bool:
    return available() and bool(ticker_in(request)) and bool(re.search(r"\bearnings?\b", request or "", re.I))


def equity_evidence(request: str) -> str | None:
    """For the equity research brief: fundamentals, forecasts and the quote
    together, or None when TradingView cannot serve this turn."""
    if not available():
        return None
    try:
        return financials(request) + "\n\n" + snapshot(request)
    except Exception:
        logger.info("TradingView equity evidence unavailable", exc_info=True)
        return None


def technicals_for(symbol: str) -> str | None:
    """The 1D technical-indicator rating for a coin, for the deep-dive lens."""
    if not available():
        return None
    try:
        resolved = resolve_symbol(symbol, "crypto")
        if not resolved:
            return None
        return _card("TradingView technicals", resolved, [("Technical rating (1D)", call_tool_sync("get_technicals_rating", {"symbol": resolved, "interval": "1D"}))])
    except Exception:
        logger.info("TradingView technicals unavailable for %s", symbol, exc_info=True)
        return None

"""A pasted link to a token page is the token, not a page to fetch.

2026-09-18: a CoinMarketCap link to OpenLedger was answered with "I couldn't
access the CoinMarketCap page" because the site blocks fetches. The link
already says which token the user means. This module reads the well-known
token-page URLs -- CoinMarketCap, CoinGecko, DEX Screener, Birdeye, the
block explorers -- into a (symbol, name, chain, address) the resolver and
the market tools can use, and the turn answers from our own data.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.settings import settings

logger = logging.getLogger(__name__)

_URL = re.compile(r"https?://[^\s<>]+", re.I)
_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# CoinGecko platform ids -> the chain names the resolver uses.
_PLATFORMS = {
    "ethereum": "ethereum", "binance-smart-chain": "bsc", "base": "base", "arbitrum-one": "arbitrum", "polygon-pos": "polygon",
    "solana": "solana", "avalanche": "avalanche", "optimistic-ethereum": "optimism", "sui": "sui",
}
_CHAIN_ORDER = ("solana", "ethereum", "bsc", "base", "arbitrum", "polygon", "avalanche", "optimism", "sui")
_EXPLORERS = {
    "etherscan.io": "ethereum", "bscscan.com": "bsc", "basescan.org": "base", "arbiscan.io": "arbitrum",
    "polygonscan.com": "polygon", "snowtrace.io": "avalanche", "optimistic.etherscan.io": "optimism",
    "solscan.io": "solana", "explorer.solana.com": "solana", "solana.fm": "solana",
}


@dataclass(frozen=True)
class TokenPage:
    source: str
    symbol: str | None = None
    name: str | None = None
    chain: str | None = None
    address: str | None = None
    slug: str | None = None

    def rewrite(self, request: str) -> str:
        """The request with the token spelled the way the resolver reads it."""
        rest = _URL.sub("", request).strip(" :-,.") or "details and current price"
        subject = f"{self.symbol} token" if self.symbol else (self.name or self.slug or "the token")
        where = f" {self.address} on {self.chain}" if self.address and self.chain else ""
        return f"{subject} {rest}{where}".strip()

    def note(self) -> str:
        who = " ".join(p for p in (self.name, f"({self.symbol})" if self.symbol else "") if p) or self.slug or "the token"
        where = f" on {self.chain}" if self.chain else ""
        return f"_Read the {self.source} link as {who}{where}._"


def parse(request: str) -> TokenPage | None:
    """The token a pasted link points at, or None when the link is not a
    token page (a news article, docs) -- those still get fetched."""
    match = _URL.search(request or "")
    if not match:
        return None
    parts = urlsplit(match.group(0))
    host = parts.netloc.lower().removeprefix("www.")
    segments = [s for s in parts.path.split("/") if s]
    query = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p)
    try:
        if host == "coinmarketcap.com" and len(segments) >= 2 and segments[0] == "currencies":
            return _from_coingecko(segments[1], "CoinMarketCap")
        if host == "coingecko.com" and "coins" in segments:
            return _from_coingecko(segments[segments.index("coins") + 1], "CoinGecko")
        if host == "dexscreener.com" and len(segments) >= 2:
            chain, ref = segments[0].lower(), segments[1]
            return TokenPage("DEX Screener", chain=chain, address=ref if (_EVM.match(ref) or _SOL.match(ref)) else None)
        if host == "birdeye.so" and "token" in segments:
            ref = segments[segments.index("token") + 1]
            return TokenPage("Birdeye", chain=(query.get("chain") or "solana").lower(), address=ref)
        if host in _EXPLORERS and segments and segments[0] in ("token", "address"):
            ref = segments[1] if len(segments) > 1 else ""
            if _EVM.match(ref) or _SOL.match(ref):
                return TokenPage(host, chain=_EXPLORERS[host], address=ref)
    except Exception:
        logger.info("token page link not read: %s", match.group(0)[:120], exc_info=True)
    return None


def _cg(path: str, params: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if settings.coingecko_api_key:
        headers["x-cg-demo-api-key"] = settings.coingecko_api_key
    with httpx.Client(timeout=12) as client:
        response = client.get(f"{settings.coingecko_base_url.rstrip('/')}{path}", params=params or {}, headers=headers)
        response.raise_for_status()
        return response.json()


def _liquid_chain(symbol: str, platforms: dict[str, str]) -> str | None:
    """Among the chains a token is deployed on, the one where it actually
    trades (OpenLedger: the same contract on Ethereum and BNB Chain, the
    volume on BNB Chain). DEX Screener liquidity by chain, same contract only."""
    if len(platforms) < 2 or not symbol:
        return None
    try:
        from app.token_resolve import token_candidates

        wanted = {c: a.lower() for c, a in platforms.items()}
        best: tuple[float, str] | None = None
        for candidate in token_candidates(symbol.upper()):
            chain = str(candidate.get("chain") or "").lower().replace("bnb", "bsc")
            if wanted.get(chain) == str(candidate.get("address") or "").lower():
                liq = float(candidate.get("liquidity_usd") or 0)
                if best is None or liq > best[0]:
                    best = (liq, chain)
        return best[1] if best else None
    except Exception:
        logger.info("liquidity by chain unavailable for %s", symbol, exc_info=True)
        return None


def _from_coingecko(slug: str, source: str) -> TokenPage | None:
    """CoinMarketCap and CoinGecko slugs usually agree; when they do not,
    CoinGecko's search by the slug's words finds the coin."""
    slug = slug.strip().lower()
    coin = None
    for candidate in (slug, None):
        try:
            if candidate:
                coin = _cg(f"/coins/{candidate}", {"localization": "false", "tickers": "false", "community_data": "false", "developer_data": "false", "sparkline": "false"})
                if coin.get("id"):
                    break
                coin = None
            else:
                hits = (_cg("/search", {"query": slug.replace("-", " ")}).get("coins") or [])
                exact = [h for h in hits if str(h.get("id") or "").lower() in (slug, f"{slug}-2") or str(h.get("name") or "").lower().replace(" ", "-") == slug]
                pick = (exact or hits)[:1]
                if pick:
                    coin = _cg(f"/coins/{pick[0]['id']}", {"localization": "false", "tickers": "false", "community_data": "false", "developer_data": "false", "sparkline": "false"})
        except Exception:
            logger.info("coingecko lookup for %s failed", slug, exc_info=True)
            coin = None
    if not coin or not coin.get("id"):
        return TokenPage(source, slug=slug, name=slug.replace("-", " ").title())
    platforms = {_PLATFORMS[k]: v for k, v in (coin.get("platforms") or {}).items() if k in _PLATFORMS and v}
    chain = _liquid_chain(str(coin.get("symbol") or ""), platforms) or next((c for c in _CHAIN_ORDER if c in platforms), None)
    return TokenPage(source, symbol=str(coin.get("symbol") or "").upper() or None, name=coin.get("name"), chain=chain,
                     address=platforms.get(chain) if chain else None, slug=slug)

"""CoinGecko coin metadata for protocols that have a token: the description,
categories, canonical links, genesis date and, most usefully, the token's
contract address on every chain it is deployed to. Those addresses become
token entities, so a contract pasted into chat resolves at confidence 1.0
to the protocol that issued it, on Base or Solana as well as Ethereum.

Free API, no key, ~30 requests a minute: calls are throttled process-wide
and a 429 is an error for this pass (retried next time it is due). Weekly
refresh; the text rarely changes."""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.knowledge.connectors.base import SourceRef
from app.knowledge.models import NormalizedDocument, Protocol
from app.knowledge.normalize import clean_markdown, content_hash, html_to_markdown

COIN_URL = "https://api.coingecko.com/api/v3/coins/{id}?localization=false&tickers=false&market_data=false&community_data=false&developer_data=false&sparkline=false"

# CoinGecko platform ids -> the KB's chain slugs (DefiLlama-style).
PLATFORMS = {
    "ethereum": "ethereum", "solana": "solana", "base": "base", "arbitrum-one": "arbitrum", "optimistic-ethereum": "optimism",
    "polygon-pos": "polygon", "binance-smart-chain": "bsc", "avalanche": "avalanche", "hyperliquid": "hyperliquid", "sui": "sui",
    "aptos": "aptos", "tron": "tron", "linea": "linea", "scroll": "scroll", "zksync": "zksync-era", "mantle": "mantle", "blast": "blast",
    "sonic": "sonic", "berachain": "berachain", "gnosis": "xdai", "fantom": "fantom", "celo": "celo", "sei-v2": "sei", "near-protocol": "near",
    "the-open-network": "ton", "unichain": "unichain", "monad": "monad", "plasma": "plasma", "world-chain": "world-chain", "ink": "ink",
    "soneium": "soneium", "mode": "mode", "manta-pacific": "manta", "metis-andromeda": "metis", "kava": "kava", "cronos": "cronos", "moonbeam": "moonbeam",
}

# EVM (20-byte hex), Solana/base58, or Move-style 0x<32 bytes>::module::Type.
_ADDRESS = re.compile(r"^(?:0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}|0x[0-9a-fA-F]{64}(?:::[\w:]+)?)$")

_throttle_lock = threading.Lock()
_last_call = 0.0
MIN_INTERVAL = 6.5  # seconds between calls, process-wide (anonymous tier tolerates ~10/min sustained)


def _throttled_get(client: httpx.Client, url: str) -> httpx.Response:
    global _last_call
    with _throttle_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
    return client.get(url)


class CoinGeckoConnector:
    name = "coingecko"
    source_type = "token"
    refresh_every = timedelta(days=7)

    def applies(self, protocol: Protocol) -> bool:
        return bool(protocol.coingecko_id)

    def discover(self, protocol: Protocol):
        return [SourceRef(url=COIN_URL.format(id=protocol.coingecko_id), title=f"{protocol.name} token", kind="bundle", metadata={"coingecko_id": protocol.coingecko_id})]

    def fetch(self, protocol: Protocol, ref: SourceRef) -> NormalizedDocument | None:
        return None

    def documents(self, protocol: Protocol, ref: SourceRef) -> list[NormalizedDocument]:
        with httpx.Client(timeout=25, headers={"User-Agent": "Dopamint-Knowledge/0.1", "Accept": "application/json"}) as client:
            resp = _throttled_get(client, ref.url)
            if resp.status_code == 404:
                return []
            if resp.status_code == 429:
                raise RuntimeError("coingecko rate limited (429); retried next pass")
            resp.raise_for_status()
            data = resp.json()
        doc = self.document(protocol, data)
        return [doc] if doc else []

    @staticmethod
    def document(protocol: Protocol, data: dict) -> NormalizedDocument | None:
        name = data.get("name") or protocol.name
        symbol = (data.get("symbol") or protocol.symbol or "").upper()
        coingecko_id = data.get("id") or protocol.coingecko_id
        if not coingecko_id:
            return None
        description = clean_markdown(html_to_markdown(((data.get("description") or {}).get("en")) or "")) or "No description provided."
        links = data.get("links") or {}
        homepage = next((u for u in (links.get("homepage") or []) if u), None)
        repos = ((links.get("repos_url") or {}).get("github")) or []
        platforms = [(PLATFORMS.get(pid, pid), addr) for pid, addr in (data.get("platforms") or {}).items() if pid and isinstance(addr, str) and _ADDRESS.match(addr)]
        categories = [c for c in (data.get("categories") or []) if isinstance(c, str)]
        lines = [
            f"# {name} token ({symbol})" if symbol else f"# {name} token", "",
            f"**Protocol**: {protocol.name} · **CoinGecko**: {coingecko_id}" + (f" · **Genesis**: {data.get('genesis_date')}" if data.get("genesis_date") else ""),
        ]
        if homepage or links.get("whitepaper") or repos or links.get("twitter_screen_name"):
            lines.append("**Links**: " + " · ".join(x for x in [
                f"site {homepage}" if homepage else "", f"whitepaper {links['whitepaper']}" if links.get("whitepaper") else "",
                f"github {repos[0]}" if repos else "", f"twitter @{links['twitter_screen_name']}" if links.get("twitter_screen_name") else "",
            ] if x))
        if categories:
            lines.append(f"**Categories**: {', '.join(categories[:8])}")
        lines += ["", "## About", description]
        if platforms:
            lines += ["", "## Contract addresses", *[f"- {chain}: `{addr}`" for chain, addr in platforms]]
        content = "\n".join(lines)
        facts = [{
            "relation": "TOKEN_OF", "direction": "in", "confidence": 0.98,
            "target": {"id": f"token:{chain}:{addr.lower() if addr.startswith('0x') else addr}", "type": "token", "name": f"{protocol.name} token", "symbol": symbol or None,
                       "chain": chain, "address": addr, "aliases": [name] if name.lower() != protocol.name.lower() else [], "metadata": {"coingecko_id": coingecko_id}},
            "metadata": {"source": "coingecko"},
        } for chain, addr in platforms]
        return NormalizedDocument(
            source="coingecko", source_type="token", url=f"https://www.coingecko.com/en/coins/{coingecko_id}", protocol_id=protocol.id, title=f"{name} token ({symbol})" if symbol else f"{name} token",
            content=content, content_hash=content_hash(content), retrieved_at=datetime.now(timezone.utc),
            metadata={"coingecko_id": coingecko_id, "symbol": symbol, "categories": categories[:8], "platforms": dict(platforms), "facts": facts},
        )

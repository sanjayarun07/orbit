"""Memecoin forensics: who holds it, who is trading it, and who shipped it.

User direction (2026-09-18): memecoins on Solana, Base and BNB Chain are the
focus, and Orbit should answer what GMGN answers -- holders, top holders,
concentration, snipers and insiders, the developer's track record, and the
latest transactions.

Three tools over Mobula's indexed data, each verified live against a real
Solana memecoin before it was written:

  mobula_token_holders   /api/2/token/holder-positions
  mobula_token_trades    /api/2/token/trades
  mobula_wallet_deployer /api/2/wallet/deployer

Holder positions carry far more than a balance: each wallet's share of
supply, its buy and sell counts, its realized and unrealized PnL, when it
was funded and last traded, and Mobula's labels for it. That is what lets a
holder table answer "is this bundled" and "are insiders still in" rather
than just listing addresses.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx

from app.mobula_security import _CHAIN_NAMES, _subject
from app.provider_router import ProviderRouter, ProviderTool
from app.settings import settings
from app.tool_catalog import TOOL_SPECS

logger = logging.getLogger(__name__)

_BASE = "https://production-api.mobula.io/api/2"
_WALLET = re.compile(r"\b(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})\b")

HOLDERS_ASK = re.compile(
    r"\b(?:holders?|holding|concentration|whales?|bundle[ds]?|bundling|snip(?:e|ed|er|ers|ing)|insiders?|"
    r"top\s*\d*\s*(?:holders?|wallets?)|distribution|who\s+(?:holds|owns))\b", re.IGNORECASE)
TRADES_ASK = re.compile(
    r"\b(?:trades?|trading|swaps?|transactions?|buys?|sells?|activity|flow|volume\s+recent|latest|recent)\b", re.IGNORECASE)
DEPLOYER_ASK = re.compile(
    r"\b(?:deployer|developer|dev\b|creator|deployed|launched|team\s+wallet|who\s+(?:made|created|launched|deployed))\b", re.IGNORECASE)
# Labels Mobula attaches that a memecoin buyer should see spelled out.
_RISK_LABELS = {"sniper", "bundler", "insider", "rug", "scam", "bot", "mev", "team", "dev", "deployer"}


def enabled() -> bool:
    return bool(settings.mobula_api_key)


def _get(path: str, params: dict) -> dict | list:
    with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
        response = client.get(f"{_BASE}{path}", params=params, headers={"Authorization": settings.mobula_api_key or ""})
        response.raise_for_status()
        payload = response.json()
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _usd(value) -> str:
    amount = _num(value, float("nan"))
    if amount != amount:                      # NaN: not reported
        return "—"
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount >= 1_000_000:
        return f"{sign}${amount/1_000_000:,.2f}M"
    if amount >= 1_000:
        return f"{sign}${amount/1_000:,.1f}K"
    return f"{sign}${amount:,.2f}"


def _when(value) -> str:
    if not value:
        return "—"
    try:
        text = str(value)
        if text.isdigit():
            return datetime.fromtimestamp(int(text) / (1000 if len(text) > 10 else 1), tz=timezone.utc).strftime("%Y-%m-%d")
        return text[:10]
    except Exception:
        return "—"


def _short(address: str) -> str:
    return f"{address[:4]}…{address[-4:]}" if address and len(address) > 10 else (address or "—")


def _labels_of(row: dict) -> list[str]:
    raw = row.get("labels") or []
    out = []
    for label in raw:
        text = label.get("name") if isinstance(label, dict) else label
        if text:
            out.append(str(text))
    return out


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def token_holders(request: str) -> str:
    """Who holds the token: share of supply, trading, PnL and labels."""
    subject = _subject(request)
    if subject is None:
        raise ValueError("No token address found in the request")
    address, chain = subject
    rows = _get("/token/holder-positions", {"address": address, "blockchain": chain, "limit": 50})
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        raise RuntimeError("Mobula returned no holder positions for this token")
    rows.sort(key=lambda r: _num(r.get("percentageOfTotalSupply")), reverse=True)
    top10 = sum(_num(r.get("percentageOfTotalSupply")) for r in rows[:10])
    flagged: dict[str, int] = {}
    for row in rows:
        for label in _labels_of(row):
            if any(word in label.lower() for word in _RISK_LABELS):
                flagged[label] = flagged.get(label, 0) + 1

    lines = [
        "# Token holders — Mobula",
        f"**Contract**: `{address}` · **Chain**: {chain} · **Checked**: {_stamp()}",
        "",
        f"**Top 10 wallets hold {top10:.2f}% of supply** (of the {len(rows)} largest positions indexed). "
        "Wallets are not necessarily distinct owners: one person can hold through many.",
        "",
        "| # | Wallet | Share | Value | Buys/Sells | Unrealized PnL | First trade | Labels |",
        "|---:|---|---:|---:|---:|---:|---|---|",
    ]
    for index, row in enumerate(rows[:15], start=1):
        labels = ", ".join(_labels_of(row)[:3]) or "—"
        lines.append(
            f"| {index} | `{_short(row.get('walletAddress'))}` | {_num(row.get('percentageOfTotalSupply')):.2f}% | "
            f"{_usd(row.get('tokenAmountUSD'))} | {row.get('buys', 0)}/{row.get('sells', 0)} | "
            f"{_usd(row.get('unrealizedPnlUSD'))} | {_when(row.get('firstTradeAt') or row.get('walletFundAt'))} | {labels} |")
    if flagged:
        lines += ["", "## Flagged wallets", "| Label | Wallets |", "|---|---:|"]
        lines += [f"| {label} | {count} |" for label, count in sorted(flagged.items(), key=lambda kv: -kv[1])[:8]]
        lines += ["", "A label is a classification from on-chain behaviour. It is evidence, not proof of coordination or intent."]
    lines += ["", "Source: [Mobula holder positions](https://docs.mobula.io/rest-api-reference/endpoint/token-holder-positions)"]
    return "\n".join(lines)


def token_trades(request: str) -> str:
    """The latest indexed swaps: who traded, how much, at what price."""
    subject = _subject(request)
    if subject is None:
        raise ValueError("No token address found in the request")
    address, chain = subject
    rows = _get("/token/trades", {"address": address, "blockchain": chain, "limit": 25})
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        raise RuntimeError("Mobula returned no trades for this token")
    buys = sum(1 for r in rows if str(r.get("type")).lower() == "buy")
    buy_usd = sum(_num(r.get("baseTokenAmountUSD")) for r in rows if str(r.get("type")).lower() == "buy")
    sell_usd = sum(_num(r.get("baseTokenAmountUSD")) for r in rows if str(r.get("type")).lower() == "sell")
    lines = [
        "# Latest trades — Mobula",
        f"**Contract**: `{address}` · **Chain**: {chain} · **Checked**: {_stamp()}",
        "",
        f"Of the last {len(rows)} indexed swaps: **{buys} buys** ({_usd(buy_usd)}) and **{len(rows) - buys} sells** ({_usd(sell_usd)}).",
        "",
        "| When | Side | Size | Price | Wallet | Venue |",
        "|---|---|---:|---:|---|---|",
    ]
    for row in rows[:15]:
        when = str(row.get("date") or "")[:19].replace("T", " ")
        platform = row.get("platform")
        venue = (platform.get("name") if isinstance(platform, dict) else platform) or row.get("blockchain") or "—"
        wallet = row.get("swapSenderAddress") or row.get("transactionSenderAddress")
        lines.append(f"| {when or '—'} | {row.get('type') or '—'} | {_usd(row.get('baseTokenAmountUSD'))} | "
                     f"{_usd(row.get('baseTokenPriceUSD'))} | `{_short(wallet)}` | {venue} |")
    lines += ["", "Source: [Mobula token trades](https://docs.mobula.io/rest-api-reference/endpoint/token-trades)",
              "Indexed swaps only: a transfer that is not a swap, or a venue Mobula does not index, is absent."]
    return "\n".join(lines)


def wallet_deployer(request: str) -> str:
    """A developer's track record: the other tokens this wallet deployed."""
    match = _WALLET.search(request or "")
    if not match:
        raise ValueError("No wallet address found in the request")
    wallet = match.group(1)
    named = re.search(r"\bon\s+([A-Za-z][A-Za-z ]{2,20}?)\b(?:\s|$|,|\.)", request or "", re.I)
    chain = _CHAIN_NAMES.get((named.group(1) if named else "").strip().lower(), "solana" if not wallet.startswith("0x") else "ethereum")
    payload = _get("/wallet/deployer", {"wallet": wallet, "blockchain": chain})
    rows = payload if isinstance(payload, list) else (payload.get("data") or [])
    rows = [r for r in rows if isinstance(r, dict)]
    lines = [
        "# Deployer track record — Mobula",
        f"**Wallet**: `{wallet}` · **Chain**: {chain} · **Checked**: {_stamp()}",
        "",
    ]
    if rows:
        lines += [f"This wallet has deployed **{len(rows)} token(s)** Mobula indexed.", "",
                  "| Token | Symbol | Deployed | Market cap |", "|---|---|---|---:|"]
        for row in rows[:15]:
            token = row.get("token") if isinstance(row.get("token"), dict) else row
            lines.append(f"| {token.get('name') or '—'} | {token.get('symbol') or '—'} | "
                         f"{_when(row.get('deployedAt') or row.get('createdAt'))} | {_usd(token.get('marketCapUSD'))} |")
        lines += ["", "A developer who has shipped many short-lived tokens is the pattern worth checking before buying the next one."]
    else:
        lines.append("Mobula indexed no other token deployments for this wallet. That is not proof it deployed none: "
                     "a deployment on a chain or venue it does not index would not appear.")
    lines += ["", "Source: [Mobula deployer tokens](https://docs.mobula.io/rest-api-reference/endpoint/wallet-deployer)"]
    return "\n".join(lines)


class MobulaMemeProvider:
    name = "mobula"

    _CHAINS = ("solana", "ethereum", "base", "arbitrum", "bsc", "bnb", "polygon", "avalanche", "optimism", "hyperevm")

    def enabled(self) -> bool:
        return enabled()

    def register(self, router: ProviderRouter) -> None:
        common = dict(enabled=self.enabled, chains=self._CHAINS, cost_usd=settings.mobula_request_cost_usd,
                      quota_per_minute=settings.mobula_requests_per_minute)
        router.register(ProviderTool(
            "mobula_token_holders", self.name, ("token_holdings", "token_security"), token_holders,
            matches=lambda request: _subject(request) is not None and bool(HOLDERS_ASK.search(request)),
            keywords=("top holders", "concentration", "bundled", "snipers", "insiders", "distribution"),
            cache_ttl_seconds=60, priority=12, spec=TOOL_SPECS.get("mobula_token_holders"),
            description="Who holds a memecoin: each wallet's share of supply, buys and sells, realized and unrealized PnL, when it first traded, and Mobula's labels for it (sniper, bundler, insider)",
            **common,
        ))
        router.register(ProviderTool(
            "mobula_token_trades", self.name, ("market_data", "token_discovery"), token_trades,
            matches=lambda request: _subject(request) is not None and bool(TRADES_ASK.search(request)) and not HOLDERS_ASK.search(request),
            keywords=("latest trades", "recent swaps", "buy sell flow"),
            cache_ttl_seconds=30, priority=6, spec=TOOL_SPECS.get("mobula_token_trades"),
            description="The latest indexed swaps for a token: time, side, size in USD, price, the wallet and the venue",
            **common,
        ))
        router.register(ProviderTool(
            "mobula_wallet_deployer", self.name, ("wallet_intelligence", "token_security"), wallet_deployer,
            matches=lambda request: bool(_WALLET.search(request or "")) and bool(DEPLOYER_ASK.search(request)),
            keywords=("deployer", "developer", "creator", "deployed tokens", "dev wallet"),
            cache_ttl_seconds=300, priority=12, spec=TOOL_SPECS.get("mobula_wallet_deployer"),
            description="The other tokens a wallet has deployed, so a memecoin developer's track record can be checked before buying",
            **common,
        ))

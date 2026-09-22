"""Wallet tracking and portfolio analysis, from Mobula.

User decision (2026-09-18): "I want to use this explicitly for wallet
tracking and portfolio analysis related queries at high priority order."
Mobula covers 40+ chains in one call, prices the long-tail tokens our
balance providers leave blank (the HETF row that showed no USD value), and
carries what we had nothing for: a wallet's transaction history, its
portfolio value over time, and realized PnL with win rates.

Three tools, each ranked above the per-chain balance providers:
  mobula_wallet_portfolio  - what it holds now, across every chain, priced
  mobula_wallet_history    - what it did: transfers and trades, dated
  mobula_wallet_analysis   - how it has done: PnL, win rate, funding

The per-chain tools stay registered and reachable; Hyperliquid perps are
still GoldRush's (Mobula's perps cover Lighter and Gains Network only, and
only on their demo gateway).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx

from app import evidence as _evidence
from app.provider_router import NoData, ProviderRouter, ProviderTool
from app.settings import settings
from app.tool_catalog import TOOL_SPECS

logger = logging.getLogger(__name__)

from app import mobula_client
_ADDRESS = re.compile(r"\b(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})\b")
# A wallet question: the words that mean "this address's own holdings or
# activity", as opposed to a token's market data.
WALLET_ASK = re.compile(
    r"\b(?:wallet|portfolio|holdings?|holds?|balances?|positions?|net\s*worth|pnl|p&l|profit\w*|loss(?:es)?|realized|unrealized|"
    r"track(?:ing|er|\s+record)?|activity|transactions?|transfers?|trades?|traded|trading|history|buy|buys|buying|bought|sell|sells|selling|sold|"
    r"allocation|exposure|address|account)\b",
    re.IGNORECASE,
)
# Spam airdrop tokens name themselves after a claim page; they carry no
# price and no liquidity, and they crowd out the real rows.
_SPAM = re.compile(r"https?://|www\.|\.(?:cfd|xyz|top|club|site|online|sbs|rest|live|shop|icu|fun)\b"
                  r"|claim|airdrop|voucher|reward[s]?\s*:|visit\b|\bfree\s|\bgift\b", re.I)


def enabled() -> bool:
    return bool(settings.mobula_api_key)


def address_in(request: str) -> str | None:
    match = _ADDRESS.search(request or "")
    return match.group(1) if match else None


# An address can be a wallet or a token contract; only the wording separates
# them. "recent trades of 0x940181… on base" is that token's trades, not a
# wallet's activity (2026-09-18), so a token-shaped phrasing with no explicit
# wallet word is left to the token tools.
_WALLET_WORD = re.compile(r"\b(?:wallet|portfolio|net\s*worth|pnl|p&l|allocation|exposure|account|address|my\b|this\s+wallet)\b", re.I)
_TOKEN_SHAPED = re.compile(r"\b(?:trades?|holders?|volume|price|liquidity|pairs?|chart|market\s*cap|supply)\s+(?:of|for)\b"
                           r"|\b(?:holders?|insiders?|snip\w+|bundl\w+|whales?|concentration|distribution|top\s*\d+|liquidity|lp|rug\w*|honeypot|deployer|dev)\b", re.I)


# "the largest wallets holding ANSEM <mint>" names wallets, but the address
# is the token's: the question is about its holders (live, 2026-09-18, the
# portfolio tool ran on the mint). These phrasings veto even a wallet word.
_HOLDERS_OF_TOKEN = re.compile(r"\bwallets?\s+(?:holding|that\s+hold|which\s+hold|hold)\b|\bholders?\s+of\b|\btop\s*\d*\s*holders?\b|\bholding\s+\$?[A-Z]{2,10}\b", re.I)


def matches(request: str) -> bool:
    """A wallet question about a concrete address. "insiders holding <mint>"
    is about the token's holders, not a wallet: token words veto unless an
    explicit wallet word is present, and a holders-of-a-token phrasing vetoes
    regardless."""
    text = request or ""
    if not address_in(text) or not WALLET_ASK.search(text):
        return False
    if _HOLDERS_OF_TOKEN.search(text):
        return False
    return bool(_WALLET_WORD.search(text)) or not _TOKEN_SHAPED.search(text)


def _get(url: str, params: dict) -> dict | list:
    """`url` is "/api/<v><path>" relative to the configured Mobula origin (kept
    as a string so tests can stub by suffix); the shared client applies the
    budget, concurrency cap and metrics."""
    version = 1 if "/api/1/" in url else 2
    path = url.split(f"/api/{version}", 1)[1]
    return mobula_client.get(version, path, params)


def _usd(value) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    if amount == 0:
        return "$0.00"
    if abs(amount) >= 1_000_000:
        return f"${amount/1_000_000:,.2f}M"
    if abs(amount) >= 1_000:
        return f"${amount/1_000:,.2f}K"
    return f"${amount:,.2f}" if abs(amount) >= 0.01 else f"${amount:.8f}".rstrip("0")


def _amount(value) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{amount:,.4f}".rstrip("0").rstrip(".") if abs(amount) < 1e12 else f"{amount:,.0f}"


def _when(ms) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return "—"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if address and len(address) > 12 else (address or "")


def _is_spam(asset: dict) -> bool:
    name = f"{asset.get('name') or ''} {asset.get('symbol') or ''}"
    return bool(_SPAM.search(name))


# No single wallet holds a trillion dollars; a "value" above this is a price
# Mobula could not have verified (live, 2026-09-22: an airdropped token on
# vitalik.eth "worth" $136 quadrillion at $4.5 trillion a token).
_IMPLAUSIBLE_VALUE_USD = 1e12


_VERIFY_ABOVE_USD = 1_000_000.0   # positions worth this much are checked against the token's own market
_VERIFY_LOOKUPS = 5


def _market_of(asset: dict, chains: dict | None) -> dict:
    """The token's market cap and liquidity from Mobula's market data, for the
    contract the wallet holds it under; {} when unknown or the call fails."""
    contracts, blockchains = list(asset.get("contracts") or []), list(asset.get("blockchains") or [])
    pairs = list(zip(contracts, blockchains))
    if chains:
        pairs = [pair for pair in pairs if pair[1] in chains] or pairs
    if not pairs:
        return {}
    try:
        data = _get("/api/1/market/data", {"asset": pairs[0][0], "blockchain": pairs[0][1]})
    except Exception:
        return {}
    market = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    return market if isinstance(market, dict) else {}


def _unverified(value: float, market: dict) -> str | None:
    """A position worth more than the whole token, or more than ten times the
    liquidity that prices it, carries a price nobody could realise (live,
    2026-09-22: a listed token at $63K each, market cap $2M, "worth" $137M)."""
    cap = float(market.get("market_cap") or 0)
    liquidity = float(market.get("liquidity") or 0)
    if cap > 0 and value > cap:
        return "exceeds the token's market cap"
    if liquidity > 0 and value > 10 * liquidity:
        return "over ten times the token's liquidity"
    return None


def _unpriceable(asset: dict, value: float) -> str | None:
    """Why a holding's USD value must not be shown or summed: an asset Mobula
    has not listed (id 0) carries a price nobody verified, and any value past
    the plausible bound is wrong whatever its source. None when it may be priced."""
    if asset.get("id") == 0:            # Mobula's explicit "not listed"; an absent key says nothing
        return "unlisted"
    if value >= _IMPLAUSIBLE_VALUE_USD:
        return "implausible"
    return None


def portfolio(request: str) -> str:
    """What the wallet holds now, across every chain Mobula indexes, priced."""
    wallet = address_in(request)
    if not wallet:
        raise ValueError("No wallet address found in the request")
    data = _get("/api/1/wallet/portfolio", {"wallet": wallet, "unlistedAssets": "true"})
    if not isinstance(data, dict):
        raise RuntimeError("Mobula returned no portfolio")
    rows, spam, dust, unpriced = [], 0, 0, []
    priced_total = 0.0
    for holding in data.get("assets") or []:
        asset = holding.get("asset") or {}
        if _is_spam(asset):
            spam += 1
            continue
        value = holding.get("estimated_balance")
        if not value and not holding.get("token_balance"):
            continue
        why = _unpriceable(asset, float(value or 0))
        if why:
            unpriced.append((asset.get("symbol") or asset.get("name") or "?", holding.get("token_balance"), why))
            continue
        if value is not None and 0 < float(value or 0) < 1:
            priced_total += float(value or 0)
            dust += 1
            continue
        chains = ", ".join((holding.get("cross_chain_balances") or {}).keys()) or ", ".join(asset.get("blockchains") or [])
        rows.append((float(value or 0), asset.get("symbol") or "—", chains, holding.get("token_balance"),
                     holding.get("price"), value, holding.get("price_change_24h"), holding.get("allocation"), asset, holding))
    rows.sort(key=lambda r: r[0], reverse=True)
    # The largest positions are checked against the token's own market: a
    # listed token can still carry a price its pools could never pay.
    checked = 0
    kept = []
    for row in rows:
        value, symbol, chains, balance, price, raw, change, allocation, asset, holding = row
        if value >= _VERIFY_ABOVE_USD and checked < _VERIFY_LOOKUPS:
            checked += 1
            why = _unverified(value, _market_of(asset, holding.get("cross_chain_balances") or {}))
            if why:
                unpriced.append((symbol, balance, why))
                continue
        priced_total += value
        kept.append(row[:8])
    rows = kept
    # The total is always the sum of what was priced and shown: Mobula's own
    # total counts spam and quarantined holdings (a hidden "airdrop" worth a
    # made-up $1e20 inflated it, 2026-09-22). Shares are recomputed likewise.
    excluded = bool(unpriced or spam)
    total = priced_total if (rows or dust or excluded) else data.get("total_wallet_balance")
    if unpriced and priced_total > 0:
        rows = [(v, sym, ch, bal, price, value, change, v / priced_total * 100) for v, sym, ch, bal, price, value, change, _ in rows]
    holdings_seen = len(data.get("assets") or [])
    env_data = {"total_usd": float(total or 0), "priced_holdings": len(rows) + dust, "unpriced_holdings": len(unpriced), "spam_hidden": spam}
    env_sources = [_evidence.source("mobula", "wallet/portfolio")]
    if unpriced:
        reasons = sorted({w for _, _, w in unpriced})
        _evidence.partial("mobula_wallet_portfolio", {"kind": "wallet", "id": wallet}, env_data, env_sources, attempted=holdings_seen,
                          successful=holdings_seen - len(unpriced), missing=[f"{len(unpriced)} holding(s) not valued: {', '.join(reasons)}"],
                          errors=[{"operation": "price verification", "reason": why, "count": sum(1 for _, _, w in unpriced if w == why)} for why in reasons])
    else:
        _evidence.complete("mobula_wallet_portfolio", {"kind": "wallet", "id": wallet}, env_data, env_sources, attempted=max(1, holdings_seen))
    lines = [
        f"# Wallet portfolio — {_short(wallet)}",
        f"**Provider**: Mobula (40+ chains) · **Checked**: {_stamp()} · **Total{' (priced holdings)' if unpriced else ''}**: {_usd(total)}",
        "",
    ]
    if rows:
        lines += ["| Token | Chains | Balance | Price | Value | 24h | Share |", "|---|---|---:|---:|---:|---:|---:|"]
        for _, symbol, chains, balance, price, value, change, allocation in rows[:20]:
            pct = f"{float(change):+.1f}%" if isinstance(change, (int, float)) else "—"
            share = f"{float(allocation):.1f}%" if isinstance(allocation, (int, float)) else "—"
            lines.append(f"| {symbol} | {chains or '—'} | {_amount(balance)} | {_usd(price)} | {_usd(value)} | {pct} | {share} |")
        if len(rows) > 20:
            lines.append(f"| … | {len(rows) - 20} more holdings | | | | | |")
    else:
        lines.append("No priced holdings were returned for this wallet.")
    skipped = [text for text, count in ((f"{dust} holding(s) under $1", dust), (f"{spam} spam/airdrop token(s)", spam)) if count]
    if skipped:
        lines += ["", f"*Not shown: {', '.join(skipped)}.*"]
    if unpriced:
        shown = ", ".join(f"{sym} ({_amount(bal)} tokens, {'unlisted' if why == 'unlisted' else 'implausible price' if why == 'implausible' else why})" for sym, bal, why in unpriced[:6])
        more = f" and {len(unpriced) - 6} more" if len(unpriced) > 6 else ""
        lines += ["", f"*Not valued: {len(unpriced)} holding(s) whose price Mobula cannot verify, left out of the total: {shown}{more}. "
                      "Unlisted tokens are usually airdrops priced by their own thin pools.*"]
    lines += ["", "Source: [Mobula wallet portfolio](https://docs.mobula.io/rest-api-reference/endpoint/wallet-portfolio)"]
    return "\n".join(lines)


def history(request: str) -> str:
    """What the wallet did: dated transfers and trades, plus how its total
    value has moved. This is what answers "when did this wallet buy X"."""
    wallet = address_in(request)
    if not wallet:
        raise ValueError("No wallet address found in the request")
    transactions = _get("/api/1/wallet/transactions", {"wallet": wallet})
    entries = (transactions or {}).get("transactions") if isinstance(transactions, dict) else transactions
    entries = [e for e in (entries or []) if isinstance(e, dict)]
    indexed = len(entries)
    # Airdrop spam arrives as an inbound transfer and Mobula types it "buy",
    # so an unfiltered history for a real wallet is a wall of claim-page
    # tokens (live, 2026-09-18: 129 transactions, all of them spam).
    entries = [e for e in entries if not _is_spam(e.get("asset") or {})]
    spam = indexed - len(entries)
    entries.sort(key=lambda e: e.get("timestamp") or 0, reverse=True)
    lines = [
        f"# Wallet history — {_short(wallet)}",
        f"**Provider**: Mobula · **Checked**: {_stamp()} · **Transactions**: {len(entries)}"
        + (f" (plus {spam} spam/airdrop transfers, not shown)" if spam else ""),
        "",
    ]
    if entries:
        lines += ["## Recent activity", "| When | Type | Asset | Amount | Value | Chain |", "|---|---|---|---:|---:|---|"]
        for entry in entries[:15]:
            asset = entry.get("asset") or {}
            symbol = (asset.get("symbol") if isinstance(asset, dict) else None) or "—"
            lines.append(f"| {_when(entry.get('timestamp'))} | {entry.get('type') or '—'} | {symbol} | "
                         f"{_amount(entry.get('amount'))} | {_usd(entry.get('amount_usd'))} | {entry.get('blockchain') or '—'} |")
        if len(entries) > 15:
            lines.append(f"| … | {len(entries) - 15} older transactions | | | | |")
        oldest = _when(entries[-1].get("timestamp"))
        lines += ["", f"Indexed activity runs back to {oldest}."]
    elif spam:
        lines.append(f"Every one of the {spam} transactions Mobula indexed for this wallet is an airdrop-spam transfer; there is no real activity to show.")
    else:
        lines.append("Mobula has no indexed transactions for this wallet.")
    try:
        series = _get("/api/1/wallet/history", {"wallet": wallet})
        points = (series or {}).get("balance_history") if isinstance(series, dict) else None
        if points:
            first, last = points[0], points[-1]
            lines += ["", "## Portfolio value",
                      f"- **Now**: {_usd((series or {}).get('balance_usd'))}",
                      f"- **Tracked from**: {_when(first[0])} at {_usd(first[1])}",
                      f"- **Latest point**: {_when(last[0])} at {_usd(last[1])} ({len(points)} points)"]
    except Exception:
        logger.info("mobula: balance history unavailable for %s", _short(wallet), exc_info=True)
    lines += ["", "Source: [Mobula wallet history](https://docs.mobula.io/rest-api-reference/endpoint/wallet-history)",
              "Transfers and trades as Mobula indexed them; a transaction it has not indexed is absent, not proof of inactivity."]
    return "\n".join(lines)


def analysis(request: str) -> str:
    """How the wallet has done: realized PnL, win rate, what it trades."""
    wallet = address_in(request)
    if not wallet:
        raise ValueError("No wallet address found in the request")
    data = _get("/api/2/wallet/analysis", {"wallet": wallet})
    if not isinstance(data, dict) or not data:
        raise NoData("Mobula has no analysis for this wallet")
    stat = data.get("stat") or {}
    lines = [
        f"# Wallet analysis — {_short(wallet)}",
        f"**Provider**: Mobula · **Checked**: {_stamp()}",
        "",
        "## Performance",
        f"- **Portfolio value**: {_usd(stat.get('totalValue'))}",
        f"- **PnL over the period**: {_usd(stat.get('periodTotalPnlUSD'))} total, {_usd(stat.get('periodRealizedPnlUSD'))} realized",
        f"- **Tokens traded**: {stat.get('periodActiveTokensCount', 0)} · **Winners**: {stat.get('periodWinCount', 0)}",
    ]
    rate = stat.get("periodRealizedRate")
    if isinstance(rate, (int, float)) and rate:
        lines.append(f"- **Realized rate**: {float(rate)*100:.1f}%")
    wins = {k: v for k, v in (data.get("winRateDistribution") or {}).items() if v}
    if wins:
        lines += ["", "## Where its trades landed", "| Return band | Trades |", "|---|---:|"]
        lines += [f"| {band} | {count} |" for band, count in wins.items()]
    caps = {k: v for k, v in (data.get("marketCapDistribution") or {}).items() if v}
    if caps:
        lines += ["", "## What it trades, by market cap", "| Band | Trades |", "|---|---:|"]
        lines += [f"| {band} | {count} |" for band, count in caps.items()]
    funding = stat.get("fundingInfo") or {}
    if funding.get("date"):
        currency = (funding.get("currency") or {}).get("symbol") or ""
        lines += ["", f"**First funded**: {str(funding['date'])[:10]}"
                  + (f" with {_amount(funding.get('formattedAmount'))} {currency}".rstrip() if funding.get("formattedAmount") else "")
                  + (f" on `{funding.get('chainId')}`" if funding.get("chainId") else "")]
    labels = [str(label) for label in (data.get("labels") or []) if label]
    if labels:
        lines += ["", f"**Labels**: {', '.join(labels[:8])}"]
    lines += ["", "Source: [Mobula wallet analysis](https://docs.mobula.io/rest-api-reference/endpoint/wallet-analysis)",
              "PnL is computed from indexed transfers; it is not tax accounting and it excludes activity Mobula has not indexed."]
    return "\n".join(lines)


_HISTORY_WORDS = re.compile(r"\b(?:history|activity|transactions?|transfers?|trades?|traded|trading|bought|sold|buy|buys|buying|sell|sells|selling|when|since|first|recent(?:ly)?|moved?|ago)\b", re.I)
_ANALYSIS_WORDS = re.compile(r"\b(?:pnl|p&l|profit\w*|loss(?:es)?|realized|unrealized|performance|win\s*rate|analysis|analyz\w+|how (?:well|has)|track\s+record)\b", re.I)


class MobulaWalletProvider:
    """Registered ahead of the per-chain balance providers: the user asked for
    Mobula first on wallet tracking and portfolio analysis (2026-09-18)."""

    name = "mobula"

    def enabled(self) -> bool:
        return enabled()

    def register(self, router: ProviderRouter) -> None:
        common = dict(
            enabled=self.enabled, chains=(), cost_usd=settings.mobula_request_cost_usd,
            quota_per_minute=settings.mobula_requests_per_minute,
        )
        router.register(ProviderTool(
            "mobula_wallet_portfolio", self.name, ("wallet_intelligence", "portfolio"), portfolio,
            matches=lambda request: matches(request) and not _HISTORY_WORDS.search(request) and not _ANALYSIS_WORDS.search(request),
            keywords=("portfolio", "wallet", "holdings", "balances", "net worth", "allocation", "exposure"),
            cache_ttl_seconds=60, priority=14, spec=TOOL_SPECS.get("mobula_wallet_portfolio"),
            description="Everything a wallet holds right now across 40+ chains, priced, with each holding's share of the portfolio",
            **common,
        ))
        router.register(ProviderTool(
            "mobula_wallet_history", self.name, ("wallet_intelligence",), history,
            matches=lambda request: matches(request) and bool(_HISTORY_WORDS.search(request)),
            keywords=("history", "activity", "transactions", "transfers", "trades", "when", "bought", "sold"),
            cache_ttl_seconds=60, priority=14, spec=TOOL_SPECS.get("mobula_wallet_history"),
            description="A wallet's dated transfers and trades across chains, and how its total value has moved; answers when it bought or sold something",
            **common,
        ))
        router.register(ProviderTool(
            "mobula_wallet_analysis", self.name, ("wallet_intelligence", "portfolio"), analysis,
            matches=lambda request: matches(request) and bool(_ANALYSIS_WORDS.search(request)),
            keywords=("pnl", "profit", "performance", "win rate", "realized", "analysis", "track record"),
            cache_ttl_seconds=120, priority=14, spec=TOOL_SPECS.get("mobula_wallet_analysis"),
            description="How a wallet has performed: realized PnL, win-rate and market-cap distribution of its trades, first funding, labels",
            **common,
        ))

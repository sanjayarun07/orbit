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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from app import evidence
from app.mobula_security import _CHAIN_NAMES, _subject
from app.provider_router import NoData, ProviderRouter, ProviderTool
from app.settings import settings
from app.tool_catalog import TOOL_SPECS

logger = logging.getLogger(__name__)

from app import mobula_client

if TYPE_CHECKING:  # pragma: no cover
    from app.signals import Signal, Subject
_WALLET = re.compile(r"\b(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})\b")

HOLDERS_ASK = re.compile(
    r"\b(?:holders?|holding|concentration|whales?|bundle[ds]?|bundling|snip(?:e|ed|er|ers|ing)|insiders?|"
    r"top\s*\d*\s*(?:holders?|wallets?)|distribution|who\s+(?:holds|owns))\b", re.IGNORECASE)
TRADES_ASK = re.compile(
    r"\b(?:trades?|trading|swaps?|transactions?|buys?|sells?|activity|flow|volume\s+recent|latest|recent)\b", re.IGNORECASE)
DEPLOYER_ASK = re.compile(
    r"\b(?:deployer|developer|dev\b|creator|deploy\w*|launch(?:ed)?|team\s+wallet|who\s+(?:made|created|launched|deployed))\b", re.IGNORECASE)
# Labels Mobula attaches that a memecoin buyer should see spelled out.
_RISK_LABELS = {"sniper", "bundler", "insider", "rug", "scam", "bot", "mev", "team", "dev", "deployer"}


def enabled() -> bool:
    return bool(settings.mobula_api_key)


def _get(path: str, params: dict) -> dict | list:
    return mobula_client.get(2, path, params)


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
    if amount >= 1_000_000_000:
        return f"{sign}${amount/1_000_000_000:,.2f}B"
    if amount >= 1_000_000:
        return f"{sign}${amount/1_000_000:,.2f}M"
    if amount >= 1_000:
        return f"{sign}${amount/1_000:,.1f}K"
    return f"{sign}${amount:,.2f}"


def _moment(value) -> datetime | None:
    """Mobula dates arrive as epoch milliseconds (trades, transfers), epoch
    seconds, or ISO strings (first buyers, funding); one reader for all."""
    if value in (None, "", 0):
        return None
    try:
        text = str(value).strip()
        if re.fullmatch(r"\d{9,14}", text):
            number = int(text)
            return datetime.fromtimestamp(number / (1000 if number > 10_000_000_000 else 1), tz=timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _when(value) -> str:
    moment = _moment(value)
    return moment.strftime("%Y-%m-%d") if moment else "—"


def _when_time(value) -> str:
    moment = _moment(value)
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else "—"


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
    subject_env = {"kind": "token", "id": address, "chain": chain}
    rows = _get("/token/holder-positions", {"address": address, "blockchain": chain, "limit": 50})
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        evidence.unavailable("mobula_token_holders", subject_env, "no holder positions indexed", [evidence.source("mobula", "token/holder-positions")])
        raise NoData("Mobula has no holder positions for this token")
    rows.sort(key=lambda r: _num(r.get("percentageOfTotalSupply")), reverse=True)
    top10 = sum(_num(r.get("percentageOfTotalSupply")) for r in rows[:10])
    env = evidence.complete("mobula_token_holders", subject_env,
                            {"positions_indexed": len(rows), "top10_pct_of_supply": round(top10, 2),
                             "top10_definition": "sum of the ten largest indexed positions; pools, exchanges and burn addresses included",
                             "largest": [{"wallet": r.get("walletAddress"), "pct": round(_num(r.get("percentageOfTotalSupply")), 4), "labels": _labels_of(r)[:3]} for r in rows[:10]]},
                            [evidence.source("mobula", "token/holder-positions")])
    for r in rows[:15]:
        env.add_anchor(evidence.record(str(r.get("walletAddress") or ""), "mobula", chain, at=r.get("lastTradeAt") or r.get("firstTradeAt"),
                                       note=f"{_num(r.get('percentageOfTotalSupply')):.2f}% of supply, indexed position"))
    flagged: dict[str, int] = {}
    for row in rows:
        for label in _labels_of(row):
            if any(word in label.lower() for word in _RISK_LABELS):
                flagged[label] = flagged.get(label, 0) + 1

    lines = [
        "# Token holders",
        f"**Provider**: Mobula · **Contract**: `{address}` · **Chain**: {chain} · **Checked**: {_stamp()}",
        "",
        f"**Top 10 wallets hold {top10:.2f}% of supply** (of the {len(rows)} largest positions indexed, pools, exchanges and burn addresses included; "
        "the security profile's top-10 figure applies Mobula's own exclusions and can differ). "
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
    trade_rows = [r for r in (rows or []) if isinstance(r, dict)]
    if trade_rows:
        env = evidence.complete("mobula_token_trades", {"kind": "token", "id": address, "chain": chain},
                                {"trades": len(trade_rows), "buys": sum(1 for r in trade_rows if str(r.get("type") or "").lower() == "buy"),
                                 "sells": sum(1 for r in trade_rows if str(r.get("type") or "").lower() == "sell")},
                                [evidence.source("mobula", "token/trades")])
        for r in trade_rows[:25]:
            if r.get("transactionHash"):
                env.add_anchor(evidence.tx(r["transactionHash"], "mobula", chain, at=r.get("date"),
                                           note=f"{str(r.get('type') or '').lower()} {_usd(r.get('baseTokenAmountUSD'))} on {r.get('platform') or '?'}"))
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        raise NoData("Mobula has no indexed trades for this token")
    buys = sum(1 for r in rows if str(r.get("type")).lower() == "buy")
    buy_usd = sum(_num(r.get("baseTokenAmountUSD")) for r in rows if str(r.get("type")).lower() == "buy")
    sell_usd = sum(_num(r.get("baseTokenAmountUSD")) for r in rows if str(r.get("type")).lower() == "sell")
    lines = [
        "# Latest trades",
        f"**Provider**: Mobula · **Contract**: `{address}` · **Chain**: {chain} · **Checked**: {_stamp()}",
        "",
        f"Of the last {len(rows)} indexed swaps: **{buys} buys** ({_usd(buy_usd)}) and **{len(rows) - buys} sells** ({_usd(sell_usd)}).",
        "",
        "| When | Side | Size | Price | Wallet | Venue |",
        "|---|---|---:|---:|---|---|",
    ]
    for row in rows[:15]:
        when = _when_time(row.get("date"))
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
        "# Deployer track record",
        f"**Provider**: Mobula · **Wallet**: `{wallet}` · **Chain**: {chain} · **Checked**: {_stamp()}",
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
    funded = _funding_line(wallet, chain)
    if funded:
        lines += ["", funded]
    lines += ["", "Source: [Mobula deployer tokens](https://docs.mobula.io/rest-api-reference/endpoint/wallet-deployer)"]
    return "\n".join(lines)


PULSE_CHAINS = {"solana": "solana:solana", "base": "evm:8453", "bsc": "evm:56", "bnb": "evm:56", "ethereum": "evm:1", "hyperevm": "evm:999", "robinhood": "evm:4663"}
LAUNCH_ASK = re.compile(
    r"\b(?:new\s+(?:launch\w*|tokens?|memes?|coins?|pairs?|listings?)|(?:just|recently|freshly)\s+launch\w*|fresh\s+launch\w*|launchpad|pump\.?fun|"
    r"bonding|bonded|graduat\w+|migrat\w+\s+to\s+raydium|latest\s+(?:launch\w*|memes?)|what'?s\s+launching|new\s+on\s+(?:solana|base|bsc|bnb))\b", re.I)


def _get_v1(path: str, params: dict) -> dict | list:
    return mobula_client.get(1, path, params)


def token_first_buyers(request: str) -> str:
    """The first wallets in: what they bought, what they still hold, who is
    tagged. Amounts are raw units, so retention is reported as a share."""
    subject = _subject(request)
    if subject is None:
        raise ValueError("No token address found in the request")
    address, chain = subject
    rows = _get_v1("/token/first-buyers", {"asset": address, "blockchain": chain, "limit": 100})
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        raise NoData("Mobula has no first buyers for this token")
    rows.sort(key=lambda r: str(r.get("firstHoldingDate") or ""))
    initial = sum(_num(r.get("initialAmount")) for r in rows)
    current = sum(min(_num(r.get("currentBalance")), _num(r.get("initialAmount"))) for r in rows)
    still_in = sum(1 for r in rows if _num(r.get("currentBalance")) > 0)
    added = sum(1 for r in rows if _num(r.get("currentBalance")) > _num(r.get("initialAmount")) > 0)
    tagged: dict[str, int] = {}
    for row in rows:
        for tag in row.get("tags") or []:
            text = tag.get("name") if isinstance(tag, dict) else str(tag)
            if text:
                tagged[text] = tagged.get(text, 0) + 1
    retention = (current / initial * 100) if initial else 0.0
    lines = [
        "# First buyers",
        f"**Provider**: Mobula · **Contract**: `{address}` · **Chain**: {chain} · **Checked**: {_stamp()}",
        "",
        f"Of the first **{len(rows)}** buyers, **{still_in} still hold** something, **{added} added** to their position, and "
        f"**{len(rows) - still_in} exited**. Early buyers retain about **{retention:.0f}%** of what they first bought.",
        "",
        "| # | Wallet | First held | Still holding | Change | Tags |",
        "|---:|---|---|---|---|---|",
    ]
    for index, row in enumerate(rows[:20], start=1):
        first, now = _num(row.get("initialAmount")), _num(row.get("currentBalance"))
        status = "yes" if now > 0 else "no"
        change = "added" if now > first > 0 else ("all" if now == 0 else (f"kept {now / first * 100:.0f}%" if first else "—"))
        tags = ", ".join((t.get("name") if isinstance(t, dict) else str(t)) for t in (row.get("tags") or [])[:3]) or "—"
        lines.append(f"| {index} | `{_short(row.get('address'))}` | {_when(row.get('firstHoldingDate'))} | {status} | {change} | {tags} |")
    if tagged:
        lines += ["", "**Tagged among them**: " + ", ".join(f"{k} ×{v}" for k, v in sorted(tagged.items(), key=lambda kv: -kv[1])[:6])]
    lines += ["", "Source: [Mobula first buyers](https://docs.mobula.io/rest-api-reference/endpoint/wallet-first-buyers)",
              "Sniping is a label from timing and behaviour; buying early is not by itself evidence of coordination."]
    return "\n".join(lines)


def _funding_line(wallet: str, chain: str) -> str | None:
    """Where a wallet's first funds came from: the first edge of a relationship graph."""
    try:
        data = _get("/wallet/funding", {"wallet": wallet, "blockchain": chain})
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("from"):
        return None
    tag = data.get("fromWalletTag") or (data.get("fromWalletMetadata") or {}).get("name") if isinstance(data.get("fromWalletMetadata"), dict) else data.get("fromWalletTag")
    return (f"**First funded** on {_when(data.get('date'))} from `{_short(str(data['from']))}`"
            + (f" ({tag})" if tag else " (no known entity)") + ".")


_QUOTES = {"SOL", "WSOL", "USDC", "USDT", "WETH", "ETH", "BNB", "WBNB"}


def _launch_identity(item: dict) -> tuple[str, str]:
    """(symbol, name) for a Pulse row. A brand-new token often has no
    tokenSymbol yet; the pair still names both sides, and the side that is
    not the quote asset is the token."""
    symbol = item.get("tokenSymbol") or item.get("symbol")
    name = item.get("tokenName") or item.get("name")
    if not symbol:
        pair = item.get("pair") or {}
        for side in ("token1", "token0"):
            token = pair.get(side) if isinstance(pair, dict) else None
            if isinstance(token, dict) and str(token.get("symbol") or "").upper() not in _QUOTES and token.get("symbol"):
                symbol, name = token.get("symbol"), token.get("name") or name
                break
    return (str(symbol) if symbol else "—", str(name or "")[:32])


def _pct_or_dash(value) -> str:
    return f"{_num(value):.1f}%" if value is not None and value != "" else "—"


def new_launches(request: str) -> str:
    """What is launching right now on a chain: new, bonding and graduated
    tokens, each with the GMGN-style risk columns Pulse carries -- dev,
    sniper, bundler and insider holdings and top-10 concentration."""
    text = (request or "").lower()
    chain = next((name for name in ("solana", "base", "bsc", "bnb", "ethereum", "hyperevm", "robinhood") if re.search(rf"\b{name}\b", text)), "solana")
    data = _get("/pulse", {"chainId": PULSE_CHAINS[chain], "limit": 10})
    if not isinstance(data, dict):
        raise RuntimeError("Mobula returned no launch feed")
    lines = [f"# New launches — {chain}", f"**Provider**: Mobula Pulse · **Checked**: {_stamp()}", ""]
    for key, label in (("new", "Just launched"), ("bonding", "Bonding (on the curve)"), ("bonded", "Graduated (bonded)")):
        items = ((data.get(key) or {}).get("data") if isinstance(data.get(key), dict) else data.get(key)) or []
        items = [i for i in items if isinstance(i, dict)][:8]
        if not items:
            continue
        lines += [f"## {label}", "| Token | Launchpad | Mcap | Holders | Vol 24h | Dev | Snipers | Bundlers | Top 10 | Age |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
        for item in items:
            symbol, name = _launch_identity(item)
            if symbol == "—":
                continue                  # nothing identifies it yet: not worth a row
            bonding = item.get("bondingPercentage")
            launchpad = str(item.get("source") or item.get("launchpad") or "—") + (f" {_num(bonding):.0f}%" if key == "bonding" and bonding is not None else "")
            holders = item.get("holders_count") or item.get("holdersCount") or 0
            trades = _num(item.get("trades_24h"))
            volume = item.get("volume_24h") if item.get("volume_24h") is not None else item.get("volume24h")
            # On a pair minutes old the market cap is derived from a few
            # lamports of quote and the "volume" is a trade count: blank
            # them rather than print $3.6B for a token with one holder.
            settled = _num(holders) >= 5 or trades >= 5
            mcap = _usd(item.get("market_cap") or item.get("marketCap")) if settled else "—"
            vol = _usd(volume) if settled and _num(volume) != trades else "—"
            lines.append(f"| {symbol} ({name}) | {launchpad} | {mcap} | {holders or '—'} | {vol} | "
                         f"{_pct_or_dash(item.get('devHoldingsPercentage'))} | {_pct_or_dash(item.get('snipersHoldingsPercentage'))} | "
                         f"{_pct_or_dash(item.get('bundlersHoldingsPercentage'))} | {_pct_or_dash(item.get('top10HoldingsPercentage'))} | "
                         f"{_when(item.get('created_at') or item.get('createdAt'))} |")
        lines.append("")
    lines += ["Dev, sniper, bundler and top-10 columns are the share of supply those wallets hold, by Mobula's labels.",
              "Source: [Mobula Pulse](https://docs.mobula.io/rest-api-reference/endpoint/pulse-get)",
              "A launch feed lists what exists, not what is safe: run the security and holders checks before touching any of these."]
    return "\n".join(lines)


BUNDLE_ASK = re.compile(r"\b(?:bundl\w+|same[\s-]*block|coordinat\w+|cluster\w*|linked\s+wallets?|connected\s+wallets?|wash|sybil)\b", re.I)
_FUNDING_LOOKUPS = 25          # first buyers whose funding source is traced (one call each)
_SAME_SECOND_MIN = 3           # wallets buying in the same second before it is called a group


# Funders that mean "no real funder": the Solana system program and burn
# addresses (Mobula reports them when a wallet's first lamports arrived by a
# system transfer), and exchange hot wallets, which fund thousands of
# strangers. They are listed for the record but never count as a cluster.
_NOT_A_FUNDER = re.compile(r"^1{31,}\d?$|^0x0{40}$|^0x0{38}dead$|dead$", re.I)
_IMPERSONAL_TAG = re.compile(r"\b(?:burn|system|program|exchange|hot\s*wallet|cex|binance|coinbase|okx|bybit|kraken|bitget|kucoin|gate|mexc|htx|faucet)\b", re.I)


def _impersonal(funder: str, tag: str | None) -> bool:
    return bool(_NOT_A_FUNDER.search(funder or "")) or bool(tag and _IMPERSONAL_TAG.search(tag))


_LOOKUP_FAILED = object()      # the call itself failed (rate limit, outage): not "no funder"


def _funding_of(wallet: str, chain: str):
    """The wallet's funding record, None when Mobula has none for it, and
    _LOOKUP_FAILED when the call did not succeed. The two were one value
    until a live BONK check under HTTP 429 reported "None found" with all 25
    lookups failed (review, 2026-09-22): missing evidence is not evidence."""
    try:
        data = _get("/wallet/funding", {"wallet": wallet, "blockchain": chain})
    except Exception:
        return _LOOKUP_FAILED
    return data if isinstance(data, dict) and data.get("from") else None


@dataclass
class BundleAnalysis:
    """The bundle read, as numbers first: the text card and the typed Signal
    are two renderings of this one analysis, so they can never disagree."""

    address: str
    chain: str
    level: str                      # "strong" | "some" | "none" | "inconclusive"
    verdict: str                    # the one-line evidence statement
    buyers: int                     # first buyers in the sample
    traced: int                     # of which a funding lookup was attempted
    grouped: int                    # buyers in a same-second group of >= _SAME_SECOND_MIN
    personal_funded: int            # buyers sharing a personal (non-exchange) funder
    strongest_group: int
    overlap: int                    # largest same-funder AND same-second overlap
    groups: list = field(default_factory=list)
    clusters: list = field(default_factory=list)
    personal: list = field(default_factory=list)
    tags: dict = field(default_factory=dict)
    second_of: dict = field(default_factory=dict)
    lookups_failed: int = 0         # attempted lookups whose call failed (rate limit, outage)
    funding: dict = field(default_factory=dict)   # wallet -> {"from", "txHash", "date"}: the transaction each funding claim rests on


def _bundle_analysis(address: str, chain: str) -> BundleAnalysis:
    """Bundle reconstruction from two independent signals: first buyers that
    entered in the same second (same block, near enough, on Solana) and first
    buyers whose wallets were first funded by the same address. Wallets that
    match on both are the strongest evidence a launch was bundled. Evidence,
    not proof: a launchpad's own router can fill a block, and one exchange hot
    wallet funds thousands of strangers. Raises NoData when Mobula has no
    first buyers for the token."""
    from concurrent.futures import ThreadPoolExecutor

    buyers = [r for r in (_get_v1("/token/first-buyers", {"asset": address, "blockchain": chain, "limit": 100}) or []) if isinstance(r, dict)]
    if not buyers:
        raise NoData("Mobula has no first buyers for this token")
    buyers.sort(key=lambda r: str(r.get("firstHoldingDate") or ""))

    # Signal 1: same second.
    by_second: dict[str, list[dict]] = {}
    for row in buyers:
        by_second.setdefault(_when_time(row.get("firstHoldingDate")), []).append(row)
    groups = sorted((rows for rows in by_second.values() if len(rows) >= _SAME_SECOND_MIN), key=len, reverse=True)

    # Signal 2: shared funder, for the earliest buyers.
    traced = buyers[:_FUNDING_LOOKUPS]
    with ThreadPoolExecutor(max_workers=6) as pool:
        funding = list(pool.map(lambda r: _funding_of(str(r.get("address") or ""), chain), traced))
    by_funder: dict[str, list[dict]] = {}
    tags: dict[str, str] = {}
    lookups_failed = sum(1 for info in funding if info is _LOOKUP_FAILED)
    funding_of: dict[str, dict] = {}
    for row, info in zip(traced, funding):
        if info and info is not _LOOKUP_FAILED:
            funder = str(info["from"])
            by_funder.setdefault(funder, []).append(row)
            funding_of[str(row.get("address") or "")] = {"from": funder, "txHash": info.get("txHash"), "date": info.get("date")}
            if info.get("fromWalletTag"):
                tags[funder] = str(info["fromWalletTag"])
    clusters = sorted(((f, rows) for f, rows in by_funder.items() if len(rows) >= 2), key=lambda kv: -len(kv[1]))
    personal = [(f, rows) for f, rows in clusters if not _impersonal(f, tags.get(f))]

    # Overlap: same (personal) funder AND same second.
    second_of = {str(r.get("address")): _when_time(r.get("firstHoldingDate")) for r in buyers}
    overlap = 0
    for _, rows in personal:
        seconds = [second_of.get(str(r.get("address"))) for r in rows]
        overlap = max(overlap, max((seconds.count(sec) for sec in set(seconds)), default=0))

    strongest = max((len(g) for g in groups), default=0)
    failed_note = f" ({lookups_failed} of {len(traced)} funding lookups failed, so the shared-funder count is a floor.)" if lookups_failed else ""
    if overlap >= 3:
        level, verdict = "strong", "**Strong** in the sample: wallets funded by the same address first held in the same second." + failed_note
    elif strongest >= 5 or (personal and len(personal[0][1]) >= 3):
        level, verdict = "some", "**Some** in the sample: a same-second group or a shared funder, but not both together." + failed_note
    elif lookups_failed:
        # No evidence, but not all the evidence was seen: a lookup that failed
        # says nothing about the wallet. This is not "none found".
        level, verdict = "inconclusive", (f"**Inconclusive**: no same-second group among the first {len(buyers)} buyers, but the funding source of only "
                                          f"{len(traced) - lookups_failed} of the first {len(traced)} could be looked up ({lookups_failed} lookups failed), "
                                          "so a shared funder cannot be ruled out. Ask again in a minute.")
    else:
        level, verdict = "none", f"**None found in the sample**: the first {len(buyers)} buyers by time, with funding traced for the first {len(traced)}."
    return BundleAnalysis(
        address=address, chain=chain, level=level, verdict=verdict, buyers=len(buyers), traced=len(traced), lookups_failed=lookups_failed, funding=funding_of,
        grouped=sum(len(g) for g in groups), personal_funded=sum(len(r) for _, r in personal),
        strongest_group=strongest, overlap=overlap, groups=groups, clusters=clusters, personal=personal, tags=tags, second_of=second_of,
    )


def _render_bundle(a: BundleAnalysis) -> str:
    def retained(rows: list[dict]) -> str:
        first = sum(_num(r.get("initialAmount")) for r in rows)
        now = sum(min(_num(r.get("currentBalance")), _num(r.get("initialAmount"))) for r in rows)
        return f"{now / first * 100:.0f}%" if first else "—"

    lines = [
        "# Bundle check",
        f"**Provider**: Mobula · **Contract**: `{a.address}` · **Chain**: {a.chain} · **Checked**: {_stamp()}",
        "",
        f"Bundle evidence: {a.verdict}",
        "",
        f"Sample: the first **{a.buyers}** buyers Mobula indexed; **{a.grouped}** of them first held in a second shared by at least "
        f"{_SAME_SECOND_MIN} wallets; the funding source of the first **{a.traced}** was looked up"
        + (f" (**{a.lookups_failed}** lookups failed)" if a.lookups_failed else "")
        + f" and **{a.personal_funded}** share a personal funder with another early buyer. Buyers after the first {a.buyers}, "
        f"and funders of buyers after the first {a.traced}, were not checked.",
    ]
    if a.groups:
        lines += ["", "## Same-second groups", "| Second (UTC) | Wallets | Still holding | Retained | Tagged |", "|---|---:|---:|---:|---:|"]
        for rows in a.groups[:6]:
            tagged = sum(1 for r in rows if r.get("tags"))
            holding = sum(1 for r in rows if _num(r.get("currentBalance")) > 0)
            lines.append(f"| {_when_time(rows[0].get('firstHoldingDate'))} | {len(rows)} | {holding} | {retained(rows)} | {tagged} |")
    if a.clusters:
        lines += ["", "## Shared funding sources", "| Funder | Known as | Wallets funded | Same second | Funding transactions |", "|---|---|---:|---:|---|"]
        for funder, rows in a.clusters[:6]:
            seconds = [a.second_of.get(str(r.get("address"))) for r in rows]
            same = max((seconds.count(sec) for sec in set(seconds)), default=0)
            known = a.tags.get(funder) or ("system / burn" if _NOT_A_FUNDER.search(funder) else "—")
            counted = "" if not _impersonal(funder, a.tags.get(funder)) else " (not counted)"
            hashes = [(a.funding.get(str(r.get("address")) or "") or {}).get("txHash") for r in rows]
            hashes = [h for h in hashes if h]
            txs = ", ".join(f"[{h[:4]}…{h[-4:]}](https://solscan.io/tx/{h})" if a.chain == "solana" else f"`{h[:4]}…{h[-4:]}`" for h in hashes[:3])
            txs += f" +{len(hashes) - 3}" if len(hashes) > 3 else ""
            lines.append(f"| `{_short(funder)}` | {known}{counted} | {len(rows)} | {same} | {txs or '—'} |")
        lines += ["", "An exchange, the system program or a burn address funds strangers and is not counted; an untagged funder feeding several first buyers is the pattern to weigh."]
    lines += ["", "Source: [Mobula first buyers](https://docs.mobula.io/rest-api-reference/endpoint/wallet-first-buyers) and "
              "[wallet funding](https://docs.mobula.io/rest-api-reference/endpoint/wallet-funding)",
              "A shared second is timing evidence, not a shared block: Mobula's first-holding times have one-second resolution and Solana "
              "produces two to three slots a second, so block-level bundling needs slot and transaction data this check does not have. "
              "Indexed data only; evidence, not proof of intent."]
    return "\n".join(lines)


def token_bundle_check(request: str) -> str:
    """The bundle check as a text card (router tool)."""
    subject = _subject(request)
    if subject is None:
        raise ValueError("No token address found in the request")
    address, chain = subject
    subject_env = {"kind": "token", "id": address, "chain": chain}
    try:
        analysis = _bundle_analysis(address, chain)
    except NoData as exc:
        evidence.unavailable("mobula_token_bundle", subject_env, str(exc), [evidence.source("mobula", "token/first-buyers")])
        raise
    bundle_evidence(analysis)
    return _render_bundle(analysis)


def bundle_evidence(a: BundleAnalysis) -> "evidence.Evidence":
    """The bundle read as an envelope: 25 attempted, 7 successful, 18
    unavailable is the record -- never 25 traced."""
    subject_env = {"kind": "token", "id": a.address, "chain": a.chain}
    data = {"level": a.level, "buyers_sampled": a.buyers, "funding_lookups": a.traced, "funding_lookups_failed": a.lookups_failed,
            "same_second_grouped": a.grouped, "personal_funded": a.personal_funded, "strongest_group": a.strongest_group, "overlap": a.overlap}
    sources = [evidence.source("mobula", "token/first-buyers"), evidence.source("mobula", "wallet/funding")]
    attempted = 1 + a.traced
    if a.lookups_failed:
        env = evidence.partial("mobula_token_bundle", subject_env, data, sources, attempted=attempted, successful=attempted - a.lookups_failed,
                               missing=[f"funding source of {a.lookups_failed} of {a.traced} early buyers"],
                               errors=[{"operation": "wallet/funding", "reason": "lookup failed (rate limit or outage)", "count": a.lookups_failed}])
    else:
        env = evidence.complete("mobula_token_bundle", subject_env, data, sources, attempted=attempted)
    # The anchors: the funding transaction behind every funding claim (a
    # cluster that is not counted still rests on real transactions), and
    # the first-holding time behind every same-second claim. Transactions
    # first, so the cap never crowds them out.
    for funder, rows in a.clusters:
        counted = "" if not _impersonal(funder, a.tags.get(funder)) else ", not counted"
        for r in rows:
            wallet = str(r.get("address") or "")
            info = a.funding.get(wallet) or {}
            if info.get("txHash"):
                env.add_anchor(evidence.tx(info["txHash"], "mobula", a.chain, at=info.get("date"), note=f"{wallet[:6]}… funded by {funder[:6]}…{counted}"))
            else:
                env.add_anchor(evidence.record(wallet, "mobula", a.chain, at=info.get("date"), note=f"funded by {funder[:6]}… (no transaction returned){counted}"))
    for group in a.groups:
        for r in group[:10]:
            env.add_anchor(evidence.record(str(r.get("address") or ""), "mobula", a.chain, at=r.get("firstHoldingDate"), note="first held in a shared second"))
    return env


# Conviction a bundle read carries on its own. Bundling is a bearish fact
# about the launch, never a bullish one: a clean sample is a real neutral vote
# (it dilutes the blend), not evidence for the token.
_BUNDLE_CONVICTION = {"strong": -0.8, "some": -0.4, "none": 0.0, "inconclusive": 0.0}
BUNDLE_MODEL = "bundle_check"


def bundle_signal(subject: "Subject", as_of: str, analysis: BundleAnalysis | None = None) -> "Signal":
    """The bundle check as an analyst vote. Runs the analysis unless one is
    passed in (the deep-dive renders the card from the same object). No first
    buyers -> abstain, with the reason; never a neutral that hides a gap."""
    from app.signals import Signal

    if analysis is None:
        try:
            analysis = _bundle_analysis(subject.id, subject.chain or "")
        except NoData as exc:
            return Signal.abstain(BUNDLE_MODEL, subject, as_of, str(exc))
        except Exception as exc:  # noqa: BLE001 - a broken call is not a view either
            return Signal.abstain(BUNDLE_MODEL, subject, as_of, f"bundle check failed: {type(exc).__name__}")
    if analysis.level == "inconclusive":
        # Not a neutral vote: the evidence was not seen (review, 2026-09-22).
        return Signal.abstain(BUNDLE_MODEL, subject, as_of, re.sub(r"\*\*", "", analysis.verdict))
    return Signal(
        model_name=BUNDLE_MODEL, subject=subject, as_of=as_of, value=_BUNDLE_CONVICTION[analysis.level],
        reasoning=re.sub(r"\*\*", "", analysis.verdict),
        components={"same_second_grouped": float(analysis.grouped), "personal_funded": float(analysis.personal_funded),
                    "strongest_group": float(analysis.strongest_group), "funder_and_second_overlap": float(analysis.overlap),
                    "sample_buyers": float(analysis.buyers), "sample_traced": float(analysis.traced), "funding_lookups_failed": float(analysis.lookups_failed)},
        metadata={"abstained": False, "level": analysis.level, "source": "mobula_token_bundle"},
    )


class MobulaMemeProvider:
    name = "mobula"

    _CHAINS = ("solana", "ethereum", "base", "arbitrum", "bsc", "bnb", "polygon", "avalanche", "optimism", "hyperevm", "robinhood")

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
            "mobula_token_first_buyers", self.name, ("token_holdings", "token_security"), token_first_buyers,
            matches=lambda request: _subject(request) is not None and bool(re.search(r"\b(?:first|earliest|early)\s+(?:buyers?|wallets?|holders?)|\bsnip\w+\b", request, re.I)),
            keywords=("first buyers", "early buyers", "snipers", "still holding"),
            cache_ttl_seconds=120, priority=13, spec=TOOL_SPECS.get("mobula_token_first_buyers"),
            description="The first wallets into a token: when they bought, whether they still hold, whether they added or exited, and which are tagged as snipers",
            **common,
        ))
        router.register(ProviderTool(
            "mobula_token_bundle", self.name, ("token_security", "token_holdings"), token_bundle_check,
            matches=lambda request: _subject(request) is not None and bool(BUNDLE_ASK.search(request)),
            keywords=("bundled", "bundle check", "same block", "coordinated buys", "linked wallets"),
            cache_ttl_seconds=300, priority=13, spec=TOOL_SPECS.get("mobula_token_bundle"),
            description="Whether a launch was bundled: first buyers that entered in the same second, first buyers funded by the same address, and the overlap of the two, with what those groups still hold",
            **common,
        ))
        router.register(ProviderTool(
            "mobula_new_launches", self.name, ("token_discovery",), new_launches,
            matches=lambda request: bool(LAUNCH_ASK.search(request or "")),
            keywords=("new launches", "pump.fun", "bonding", "graduated", "just launched"),
            cache_ttl_seconds=30, priority=12, spec=TOOL_SPECS.get("mobula_new_launches"),
            description="What is launching right now on Solana, Base or BNB Chain: new, bonding and graduated tokens with price, market cap, liquidity, holders and age",
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

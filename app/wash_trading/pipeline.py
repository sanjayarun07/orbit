"""Wash-trading detection pipeline: concentration stats, round-trip
detection, and single-hop funding-wallet fan-out.

Pure orchestration functions, not FastAPI-aware -- called from the
/admin/wash-trading/* route handlers in app/main.py via asyncio.to_thread
(Dune polling, Bitquery calls, and ClickHouse writes are all synchronous).

Phase 1 scope only -- see the implementation plan for what's deferred:
  - No full weighted clustering scorer (funding-wallet fan-out only).
  - No Bitquery Trading-cube MEV filtering (unverified entitlement).
  - No multi-hop funding-tree walk (one hop only).
  - No automated verdicts -- every output here is a lead for a human
    analyst to review, never a final judgment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import Any
from uuid import uuid4

import httpx

from app import dune_tools
from app.settings import settings
from app.wash_trading import schema


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TraderRow:
    trader_id: str
    transactions: int
    volume_usd: float
    avg_trade_usd: float
    first_trade: datetime | None = None
    last_trade: datetime | None = None


@dataclass
class TradeRow:
    block_time: datetime
    trader_id: str
    token_mint: str
    side: str  # 'buy' | 'sell' | 'unknown'
    amount_usd: float
    project: str | None
    tx_id: str


@dataclass
class ConcentrationStats:
    total_volume_usd: float
    unique_traders: int
    top1_share_pct: float
    top10_share_pct: float
    top25_share_pct: float
    top100_share_pct: float
    median_volume_usd: float
    median_transactions: float


@dataclass
class RoundTrip:
    trader_id: str
    buy_tx_id: str
    sell_tx_id: str
    buy_time: datetime
    sell_time: datetime
    gap_seconds: float
    usd_in: float
    usd_out: float
    size_diff_pct: float


@dataclass
class FundingInfo:
    wallet: str
    funding_wallet: str | None
    funding_token: str | None
    funding_amount_usd: float | None
    first_tx_signature: str | None
    first_tx_time: datetime | None
    source: str  # e.g. 'bitquery_realtime' -- records data provenance/limits


@dataclass
class DetectionRun:
    run_id: str
    token_mint: str
    pool_filter: str | None
    window_start: datetime
    window_end: datetime
    top_n: int
    concentration: ConcentrationStats
    round_trip_count: int
    fan_out_clusters: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Bitquery-backed extraction (top traders + per-wallet trade legs)
#
# Live-verified 2026-09-13 against a real token (StonkFun's STONK mint) and
# real BITQUERY_API_KEY: DEXTradeByTokens supports grouping by Trade.Account
# .Owner with sum(of: Trade_Side_AmountInUSD)/count aggregation, a
# Block.Time.since/till range filter, and MintAddress: {in: [...]} for
# multiple mints in one call. This is the PRIMARY extraction path -- Dune's
# build_top_traders_sql/build_wallet_trades_sql below are UNVERIFIED and
# blocked on a real account billing/datapoint limit (see dune_tools.py's
# module docstring); Bitquery has no such blocker on this account.
#
# IMPORTANT caveat also found live: dataset:realtime's retention is NOT the
# ~7-day window Bitquery's docs describe -- empirically it was ~43 hours on
# 2026-09-13 (44h back returned zero rows, 43h back still had data). Treat
# window_start further back than ~40h as likely to silently return nothing,
# not as a guarantee of data. combined/archive (which would cover further
# back) 403 on this account's plan -- see market_providers.py's
# BitqueryProvider docstring.
# ---------------------------------------------------------------------------

_TOP_TRADERS_QUERY = """query TopTraders($tokens: [String!], $since: DateTime!, $till: DateTime!, $limit: Int!) {
  Solana(dataset: realtime) {
    DEXTradeByTokens(
      where: {Trade: {Currency: {MintAddress: {in: $tokens}}}, Transaction: {Result: {Success: true}}, Block: {Time: {since: $since, till: $till}}}
      orderBy: {descendingByField: "volume"}
      limit: {count: $limit}
    ) {
      Trade { Account { Owner } }
      volume: sum(of: Trade_Side_AmountInUSD)
      trades: count
    }
  }
}"""

_WALLET_TRADES_QUERY = """query WalletTrades($token: String!, $wallet: String!, $since: DateTime!, $till: DateTime!) {
  Solana(dataset: realtime) {
    DEXTradeByTokens(
      limit: {count: 20000}
      orderBy: {descending: Block_Time}
      where: {Trade: {Currency: {MintAddress: {is: $token}}, Account: {Owner: {is: $wallet}}}, Transaction: {Result: {Success: true}}, Block: {Time: {since: $since, till: $till}}}
    ) {
      Block { Time }
      Trade { Side { Type } AmountInUSD }
      Transaction { Signature }
    }
  }
}"""


def _bitquery_post_with_retry(query: str, variables: dict[str, Any]) -> dict[str, Any] | None:
    """One short-backoff retry, matching the pattern already established
    elsewhere this session (app/nodes/research.py's _bitquery_balances_
    supplement) for Bitquery's own rate limiting, which was verified live to
    reject even a single sequential request some of the time. Returns None
    (rather than raising) so a single wallet's transient failure doesn't
    abort a whole batch in fetch_wallet_trades/_bitquery_top_traders.
    """
    for attempt in range(2):
        try:
            return _bitquery_post(query, variables)
        except httpx.HTTPStatusError:
            if attempt == 0:
                import time
                time.sleep(0.75)
    return None


def _bitquery_top_traders(
    token_mints: list[str], window_start: datetime, window_end: datetime, top_n: int,
) -> list[TraderRow]:
    """One call, aggregated across every mint in token_mints (a single-mint
    list for one token; a longer list -- e.g. a launchpad's most-active
    tokens -- to approximate platform-wide ranking without a per-mint
    fan-out). "trades" is a leg count, not a distinct-transaction count: a
    single routed swap can post multiple same-side legs against one mint
    (verified live), so this is a reasonable volume-ranking proxy but not an
    exact transaction tally -- fetch_wallet_trades below dedupes by tx_id
    for round-trip detection specifically, where that distinction matters.
    """
    payload = _bitquery_post_with_retry(_TOP_TRADERS_QUERY, {
        "tokens": token_mints,
        "since": window_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "till": window_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": min(top_n, 500),
    })
    if payload is None:
        raise RuntimeError("Bitquery top-traders query failed after retry")
    if payload.get("errors"):
        raise RuntimeError(f"Bitquery top-traders query failed: {payload['errors']}")
    rows = (((payload.get("data") or {}).get("Solana") or {}).get("DEXTradeByTokens")) or []
    traders = []
    for row in rows:
        owner = ((row.get("Trade") or {}).get("Account") or {}).get("Owner")
        if not owner:
            continue
        volume = float(row.get("volume") or 0)
        trades = int(row.get("trades") or 0)
        traders.append(TraderRow(
            trader_id=owner, transactions=trades, volume_usd=volume,
            avg_trade_usd=(volume / trades) if trades else 0.0,
        ))
    return traders


def _bitquery_wallet_trades_for_token(
    wallet: str, token_mint: str, window_start: datetime, window_end: datetime,
) -> list[TradeRow]:
    """Per-trade legs for one wallet/mint pair, merged by (tx_id, side) so a
    multi-hop routed swap (verified live: one signature, several same-side
    legs with different USD amounts) becomes one logical trade -- otherwise
    detect_round_trips would treat routing hops as separate buys/sells and
    both over-count trades and mis-pair round trips.
    """
    payload = _bitquery_post_with_retry(_WALLET_TRADES_QUERY, {
        "token": token_mint, "wallet": wallet,
        "since": window_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "till": window_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    if payload is None or payload.get("errors"):
        return []
    rows = (((payload.get("data") or {}).get("Solana") or {}).get("DEXTradeByTokens")) or []
    merged: dict[tuple[str, str], TradeRow] = {}
    for row in rows:
        trade = row.get("Trade") or {}
        side = ((trade.get("Side") or {}).get("Type") or "unknown").lower()
        signature = (row.get("Transaction") or {}).get("Signature")
        block_time = _parse_time((row.get("Block") or {}).get("Time"))
        amount_usd = trade.get("AmountInUSD")
        if not signature or block_time is None or amount_usd is None:
            continue
        try:
            amount_usd = float(amount_usd)
        except (TypeError, ValueError):
            continue
        key = (signature, side)
        if key in merged:
            merged[key].amount_usd += amount_usd
        else:
            merged[key] = TradeRow(
                block_time=block_time, trader_id=wallet, token_mint=token_mint,
                side=side if side in ("buy", "sell") else "unknown",
                amount_usd=amount_usd, project="bitquery_realtime", tx_id=signature,
            )
    return list(merged.values())


def fetch_top_traders(
    token_mint: str, pool_filter: str | None, window_start: datetime, window_end: datetime, top_n: int,
) -> list[TraderRow]:
    """Bitquery-backed (see module note above) -- pool_filter is accepted for
    signature compatibility with the Dune path but not applied here: Bitquery's
    DEXTradeByTokens filters by mint, not by a specific pool/market address,
    and every mint used through this pipeline so far has had exactly one
    active pool, making the distinction moot in practice. Revisit if a mint
    with multiple pools needs pool-level scoping.
    """
    return _bitquery_top_traders([token_mint], window_start, window_end, top_n)


def fetch_wallet_trades(
    wallets: list[str], token_mint: str, window_start: datetime, window_end: datetime,
) -> list[TradeRow]:
    """Bitquery-backed (see module note above): one call per wallet,
    sequential -- concurrent Bitquery calls were verified live (elsewhere
    this session) to trip rate limiting.
    """
    trades: list[TradeRow] = []
    for wallet in wallets:
        trades.extend(_bitquery_wallet_trades_for_token(wallet, token_mint, window_start, window_end))
    return trades


# ---------------------------------------------------------------------------
# Dune-backed extraction (UNVERIFIED, blocked on this account's billing --
# see dune_tools.py. Kept for when that's resolved; not called by
# run_detection below, which uses the Bitquery path above instead.)
# ---------------------------------------------------------------------------

def _parse_time(value: Any) -> datetime | None:
    """Best-effort parse of a Dune timestamp.

    UNVERIFIED exact format -- Dune commonly returns ISO-8601, but this has
    not been checked against a real /execution/{id}/results response.
    Confirm via scripts/verify_dune_schema.py and adjust here if the real
    format differs.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def dune_fetch_top_traders(
    token_mint: str, pool_filter: str | None, window_start: datetime, window_end: datetime, top_n: int,
) -> list[TraderRow]:
    """UNVERIFIED, blocked on this account's Dune billing -- see dune_tools.py.
    Not called by run_detection; kept for when that's resolved.
    """
    sql = dune_tools.build_top_traders_sql(token_mint, pool_filter, window_start, window_end, top_n)
    rows = dune_tools.run_sql(sql)
    return [
        TraderRow(
            trader_id=row.get("trader_id"),
            transactions=int(row.get("transactions") or 0),
            volume_usd=float(row.get("volume_usd") or 0),
            avg_trade_usd=float(row.get("avg_trade_usd") or 0),
            first_trade=_parse_time(row.get("first_trade")),
            last_trade=_parse_time(row.get("last_trade")),
        )
        for row in rows
        if row.get("trader_id")
    ]


def compute_concentration_stats(traders: list[TraderRow]) -> ConcentrationStats:
    if not traders:
        return ConcentrationStats(0.0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    ordered = sorted(traders, key=lambda t: t.volume_usd, reverse=True)
    total = sum(t.volume_usd for t in ordered)

    def share(n: int) -> float:
        return (sum(t.volume_usd for t in ordered[:n]) / total * 100) if total else 0.0

    return ConcentrationStats(
        total_volume_usd=total,
        unique_traders=len(ordered),
        top1_share_pct=share(1),
        top10_share_pct=share(10),
        top25_share_pct=share(25),
        top100_share_pct=share(100),
        median_volume_usd=median(t.volume_usd for t in ordered),
        median_transactions=median(t.transactions for t in ordered),
    )


def dune_fetch_wallet_trades(
    wallets: list[str], token_mint: str, window_start: datetime, window_end: datetime,
) -> list[TradeRow]:
    """UNVERIFIED, blocked on this account's Dune billing -- see dune_tools.py.
    Not called by run_detection; kept for when that's resolved.
    """
    if not wallets:
        return []
    sql = dune_tools.build_wallet_trades_sql(wallets, token_mint, window_start, window_end)
    rows = dune_tools.run_sql(sql)
    trades: list[TradeRow] = []
    for row in rows:
        bought_mint = row.get("token_bought_mint_address")
        sold_mint = row.get("token_sold_mint_address")
        if bought_mint == token_mint:
            side = "buy"
        elif sold_mint == token_mint:
            side = "sell"
        else:
            side = "unknown"
        block_time = _parse_time(row.get("block_time"))
        trader_id = row.get("trader_id")
        tx_id = row.get("tx_id")
        if block_time is None or not trader_id or not tx_id:
            continue
        trades.append(TradeRow(
            block_time=block_time, trader_id=trader_id, token_mint=token_mint, side=side,
            amount_usd=float(row.get("amount_usd") or 0), project=row.get("project"), tx_id=tx_id,
        ))
    return trades


def detect_round_trips(
    trades: list[TradeRow], max_gap_seconds: float | None = None, size_tolerance_pct: float | None = None,
) -> list[RoundTrip]:
    """Per wallet: greedily pair a buy leg with the next opposite-side leg
    within max_gap_seconds whose USD size differs by at most
    size_tolerance_pct. A trade is consumed by at most one round trip.

    Excludes same-tx_id pairs: a single routed swap can post both a buy leg
    and a sell leg against one mint when that mint is only a pass-through
    hop (verified live against real StonkFun data -- one wallet had 99.3% of
    its "round trips" turn out to be same-transaction legs, not two separate
    trading actions). Pairing those would count one atomic swap as a
    wash-trade-shaped round trip, which it structurally cannot be.
    """
    max_gap = settings.wash_trading_round_trip_max_gap_seconds if max_gap_seconds is None else max_gap_seconds
    tolerance = settings.wash_trading_round_trip_size_tolerance_pct if size_tolerance_pct is None else size_tolerance_pct

    by_wallet: dict[str, list[TradeRow]] = {}
    for trade in trades:
        by_wallet.setdefault(trade.trader_id, []).append(trade)

    round_trips: list[RoundTrip] = []
    for wallet, wallet_trades in by_wallet.items():
        ordered = sorted(wallet_trades, key=lambda t: t.block_time)
        used: set[int] = set()
        for i, first in enumerate(ordered):
            if i in used or first.side not in ("buy", "sell"):
                continue
            for j in range(i + 1, len(ordered)):
                if j in used:
                    continue
                second = ordered[j]
                gap = (second.block_time - first.block_time).total_seconds()
                if gap > max_gap:
                    break  # trades are time-sorted -- nothing further out can match either
                if second.side == first.side or second.side not in ("buy", "sell"):
                    continue
                if second.tx_id == first.tx_id:
                    continue  # same transaction's own legs, not two separate trades
                usd_in, usd_out = (first.amount_usd, second.amount_usd) if first.side == "buy" else (second.amount_usd, first.amount_usd)
                if usd_in <= 0:
                    continue
                size_diff = abs(usd_in - usd_out) / usd_in
                if size_diff <= tolerance:
                    buy, sell = (first, second) if first.side == "buy" else (second, first)
                    round_trips.append(RoundTrip(
                        trader_id=wallet, buy_tx_id=buy.tx_id, sell_tx_id=sell.tx_id,
                        buy_time=buy.block_time, sell_time=sell.block_time, gap_seconds=gap,
                        usd_in=usd_in, usd_out=usd_out, size_diff_pct=size_diff * 100,
                    ))
                    used.add(i)
                    used.add(j)
                    break
    return round_trips


# ---------------------------------------------------------------------------
# Bitquery-backed funding trace (Phase 1: one hop, Solana only -- Dune's
# dex_solana.trades trader_id values are always Solana addresses)
# ---------------------------------------------------------------------------

_MEANINGFUL_FUNDING_SYMBOLS = {"SOL", "USDC", "USDT"}
_FUNDING_DUST_USD = 10.0

# UNVERIFIED: this Solana Transfers query shape follows the field-naming
# pattern already live-verified this session for BalanceUpdates/
# DEXTradeByTokens (Transfer.Sender.Address, .Amount, .AmountInUSD,
# .Currency.Symbol), but has not itself been tested against a real Bitquery
# response. A schema mismatch here fails loudly (GraphQL "errors" array or
# an empty result), not silently -- verify with
# scripts/verify_bitquery_trading_cube.py's sibling probe before trusting
# results in production use.
_FUNDING_QUERY = """query FundingSource($address: String!) {
  Solana(dataset: realtime) {
    Transfers(
      limit: {count: 10}
      orderBy: {ascending: Block_Time}
      where: {Transfer: {Receiver: {Address: {is: $address}}}, Transaction: {Result: {Success: true}}}
    ) {
      Block { Time }
      Transfer { Sender { Address } Amount AmountInUSD Currency { Symbol } }
      Transaction { Signature }
    }
  }
}"""


def _bitquery_post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    if not settings.bitquery_api_key:
        raise RuntimeError("BITQUERY_API_KEY is not configured")
    with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
        response = client.post(
            settings.bitquery_graphql_url,
            headers={"Authorization": f"Bearer {settings.bitquery_api_key}"},
            json={"query": query, "variables": variables},
        )
        response.raise_for_status()
        return response.json()


def _fetch_funding_source(wallet: str) -> FundingInfo:
    payload = None
    for attempt in range(2):
        try:
            payload = _bitquery_post(_FUNDING_QUERY, {"address": wallet})
            break
        except httpx.HTTPStatusError:
            # Bitquery's own rate limiting was verified live (elsewhere this
            # session) to reject even a single sequential request some of
            # the time -- one short-backoff retry recovers most of those.
            if attempt == 0:
                import time
                time.sleep(0.75)
    if payload is None or payload.get("errors"):
        return FundingInfo(wallet, None, None, None, None, None, "bitquery_realtime")
    transfers = (((payload.get("data") or {}).get("Solana") or {}).get("Transfers")) or []
    for row in transfers:
        transfer = row.get("Transfer") or {}
        symbol = (transfer.get("Currency") or {}).get("Symbol")
        amount_usd = transfer.get("AmountInUSD")
        try:
            amount_usd = float(amount_usd) if amount_usd is not None else None
        except (TypeError, ValueError):
            amount_usd = None
        if symbol not in _MEANINGFUL_FUNDING_SYMBOLS or amount_usd is None or amount_usd < _FUNDING_DUST_USD:
            continue
        return FundingInfo(
            wallet=wallet,
            funding_wallet=(transfer.get("Sender") or {}).get("Address"),
            funding_token=symbol,
            funding_amount_usd=amount_usd,
            first_tx_signature=(row.get("Transaction") or {}).get("Signature"),
            first_tx_time=_parse_time((row.get("Block") or {}).get("Time")),
            source="bitquery_realtime",
        )
    return FundingInfo(wallet, None, None, None, None, None, "bitquery_realtime")


def trace_funding_fanout(wallets: list[str]) -> dict[str, FundingInfo]:
    """One Bitquery hop per wallet: first meaningful (>= $10, SOL/USDC/USDT)
    incoming transfer and its sender. Sequential, not concurrent -- firing
    many Bitquery requests at once was verified live (elsewhere this
    session) to trip their rate limiting.
    """
    return {wallet: _fetch_funding_source(wallet) for wallet in wallets}


def _compute_fan_out_clusters(
    funding: dict[str, FundingInfo], traders: list[TraderRow], min_wallets: int = 2,
) -> list[dict[str, Any]]:
    """Phase 1's cut-down clustering: group wallets that share the same
    first-hop funding wallet. This is deliberately not the full weighted
    scorer (see module docstring) -- it's the single cheapest, highest-signal
    check in the methodology.
    """
    volume_by_wallet = {t.trader_id: t.volume_usd for t in traders}
    by_funder: dict[str, list[str]] = {}
    for wallet, info in funding.items():
        if info.funding_wallet:
            by_funder.setdefault(info.funding_wallet, []).append(wallet)
    clusters = [
        {
            "cluster_id": str(uuid4()),
            "funding_wallet": funder,
            "wallets": wallets,
            "cluster_size": len(wallets),
            "cluster_volume_usd": sum(volume_by_wallet.get(w, 0.0) for w in wallets),
        }
        for funder, wallets in by_funder.items()
        if len(wallets) >= min_wallets
    ]
    clusters.sort(key=lambda c: c["cluster_volume_usd"], reverse=True)
    return clusters


# ---------------------------------------------------------------------------
# Orchestration + persistence
# ---------------------------------------------------------------------------

# Cap on how many top wallets get the (expensive: Dune trade pull + one
# Bitquery call each) round-trip/funding-trace treatment. top_n can be up to
# 500; running deep per-wallet analysis on all of them would be slow and
# costly for little marginal signal beyond the highest-volume wallets.
_DEEP_ANALYSIS_WALLET_CAP = 50


def run_detection(
    token_mint: str, pool_filter: str | None, window_start: datetime, window_end: datetime,
    top_n: int, label: str | None = None,
) -> DetectionRun:
    run_id = str(uuid4())
    client = schema.get_client()
    schema.ensure_schema(client)
    client.insert(
        "wash_trading_runs",
        [(run_id, token_mint, pool_filter, label, window_start, window_end, top_n, datetime.now(timezone.utc), "running")],
        column_names=["run_id", "token_mint", "pool_filter", "label", "window_start", "window_end", "top_n", "created_at", "status"],
    )

    try:
        traders = fetch_top_traders(token_mint, pool_filter, window_start, window_end, top_n)
        concentration = compute_concentration_stats(traders)

        top_wallets = [t.trader_id for t in traders[:_DEEP_ANALYSIS_WALLET_CAP]]
        trades = fetch_wallet_trades(top_wallets, token_mint, window_start, window_end)
        round_trips = detect_round_trips(trades)
        # Computed before the wallet_stats insert below so round_trip_count
        # can be populated for real, instead of always 0 -- only wallets
        # within _DEEP_ANALYSIS_WALLET_CAP get deep-analyzed at all, so 0
        # here means "not deep-analyzed or genuinely zero", not necessarily
        # the latter; that ambiguity is inherent to the two-tier design
        # (see _DEEP_ANALYSIS_WALLET_CAP's docstring), not new here.
        round_trip_counts: dict[str, int] = {}
        for rt in round_trips:
            round_trip_counts[rt.trader_id] = round_trip_counts.get(rt.trader_id, 0) + 1

        if traders:
            client.insert(
                "wash_trading_wallet_stats",
                [
                    (run_id, t.trader_id, t.volume_usd, t.transactions, t.avg_trade_usd,
                     round_trip_counts.get(t.trader_id, 0),
                     (t.volume_usd / concentration.total_volume_usd * 100) if concentration.total_volume_usd else 0.0)
                    for t in traders
                ],
                column_names=["run_id", "trader_id", "total_volume_usd", "trade_count", "median_trade_usd", "round_trip_count", "volume_share_pct"],
            )

        if trades:
            round_trip_tx_ids = {rt.buy_tx_id for rt in round_trips} | {rt.sell_tx_id for rt in round_trips}
            client.insert(
                "wash_trading_trades",
                [
                    (run_id, t.tx_id, t.trader_id, t.token_mint, t.side, t.amount_usd, t.project or "", "",
                     t.block_time, t.tx_id in round_trip_tx_ids, None)
                    for t in trades
                ],
                column_names=["run_id", "tx_id", "trader_id", "token_mint", "side", "amount_usd", "project", "program_id", "block_time", "is_round_trip_leg", "round_trip_id"],
            )

        funding = trace_funding_fanout(top_wallets)
        funding_rows = [
            (run_id, wallet, info.funding_wallet, info.funding_token or "", info.funding_amount_usd or 0.0,
             info.first_tx_signature or "", info.first_tx_time or datetime.now(timezone.utc), info.source)
            for wallet, info in funding.items() if info.funding_wallet
        ]
        if funding_rows:
            client.insert(
                "wash_trading_funding_edges", funding_rows,
                column_names=["run_id", "wallet", "funding_wallet", "funding_token", "funding_amount_usd", "first_tx_signature", "first_tx_time", "source"],
            )

        fan_out = _compute_fan_out_clusters(funding, traders)
        if fan_out:
            cluster_rows = [
                (run_id, cluster["cluster_id"], wallet, cluster["cluster_size"], cluster["cluster_volume_usd"],
                 cluster["funding_wallet"], "shared_funding_fanout")
                for cluster in fan_out for wallet in cluster["wallets"]
            ]
            client.insert(
                "wash_trading_clusters", cluster_rows,
                column_names=["run_id", "cluster_id", "wallet", "cluster_size", "cluster_volume_usd", "shared_funding_wallet", "reason"],
            )
        client.command(f"ALTER TABLE wash_trading_runs UPDATE status = 'completed' WHERE run_id = '{run_id}'")
    except Exception:
        client.command(f"ALTER TABLE wash_trading_runs UPDATE status = 'failed' WHERE run_id = '{run_id}'")
        raise

    return DetectionRun(
        run_id=run_id, token_mint=token_mint, pool_filter=pool_filter, window_start=window_start,
        window_end=window_end, top_n=top_n, concentration=concentration,
        round_trip_count=len(round_trips), fan_out_clusters=fan_out,
    )

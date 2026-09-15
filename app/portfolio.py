"""Deterministic portfolio valuation. No LLM involved: balance math and USD
pricing happen here in typed Python, matching the policy that DSPy must never
perform balance arithmetic.
"""

import asyncio
import logging

from app.jupiter import jupiter
from app.settings import settings
from app.solana_rpc import get_sol_balance, get_token_accounts

WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
_FALLBACK_LOOKUPS = 10  # per-mint retries for mints the batch search did not return

logger = logging.getLogger(__name__)


class Holding(dict):
    """Typed alias kept as a plain dict for JSON responses; see build_portfolio_snapshot."""


async def _price_lookup(mint: str) -> dict | None:
    try:
        return await jupiter.token_by_mint(mint)
    except Exception:
        return None


async def _price_mints(mints: list[str]) -> dict[str, dict]:
    """Batched registry lookups (100 mints per call) plus a few bounded
    per-mint retries for what the batch missed. A whale wallet with thousands
    of token accounts used to fan out one call per mint; this is 1-3 calls."""
    try:
        found = await jupiter.tokens_by_mints(mints)
    except Exception:
        logger.warning("portfolio: batched price lookup failed", exc_info=True)
        found = {}
    missing = [m for m in mints if m not in found][:_FALLBACK_LOOKUPS]
    if missing:
        retried = await asyncio.gather(*(_price_lookup(m) for m in missing))
        found.update({m: info for m, info in zip(missing, retried) if info is not None})
    return found


async def build_portfolio_snapshot(wallet_address: str) -> dict:
    """Aggregate SOL + SPL balances with USD pricing and allocation percentages.

    Bounded on purpose: at most `portfolio_max_priced_holdings` holdings are
    priced (largest raw balances first) within `portfolio_snapshot_timeout_seconds`;
    past either limit the snapshot is returned with the rest unpriced and
    `partial: true` rather than holding a chat turn hostage."""
    sol_balance, accounts = await asyncio.gather(
        get_sol_balance(wallet_address),
        get_token_accounts(wallet_address),
    )

    raw_holdings = []
    for entry in accounts.get("value", []):
        parsed = entry["account"]["data"]["parsed"]["info"]
        amount = float(parsed["tokenAmount"]["uiAmount"] or 0)
        if amount <= 0:
            continue
        raw_holdings.append({"mint": parsed["mint"], "amount": amount})
    raw_holdings.sort(key=lambda h: -h["amount"])

    cap = max(1, settings.portfolio_max_priced_holdings)
    to_price = [WRAPPED_SOL_MINT] + [h["mint"] for h in raw_holdings[:cap]]
    partial = len(raw_holdings) > cap
    try:
        price_by_mint = await asyncio.wait_for(_price_mints(to_price), timeout=settings.portfolio_snapshot_timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning("portfolio: pricing timed out for %s (%d holdings); returning an unpriced snapshot", wallet_address, len(raw_holdings))
        price_by_mint, partial = {}, True

    sol_price_info = price_by_mint.get(WRAPPED_SOL_MINT)
    sol_usd_price = float(sol_price_info["usdPrice"]) if sol_price_info and sol_price_info.get("usdPrice") else None
    sol_usd_value = sol_balance["sol"] * sol_usd_price if sol_usd_price is not None else None

    holdings = []
    for holding in raw_holdings:
        info = price_by_mint.get(holding["mint"])
        symbol = info.get("symbol") if info else None
        usd_price = float(info["usdPrice"]) if info and info.get("usdPrice") is not None else None
        usd_value = holding["amount"] * usd_price if usd_price is not None else None
        holdings.append(
            {
                "mint": holding["mint"],
                "symbol": symbol or "UNKNOWN",
                "amount": holding["amount"],
                "usd_price": usd_price,
                "usd_value": usd_value,
                "verified": bool(info and "verified" in (info.get("tags") or [])),
            }
        )

    known_values = [h["usd_value"] for h in holdings if h["usd_value"] is not None]
    total_usd_value = (sol_usd_value or 0) + sum(known_values)
    total_usd_value = total_usd_value if (sol_usd_value is not None or known_values) else None

    def allocation_pct(value: float | None) -> float | None:
        if value is None or not total_usd_value:
            return None
        return round(value / total_usd_value * 100, 2)

    holdings.sort(key=lambda h: (h["usd_value"] is None, -(h["usd_value"] or 0)))
    for holding in holdings:
        holding["allocation_pct"] = allocation_pct(holding["usd_value"])

    return {
        "wallet": wallet_address,
        "sol": {
            "amount": sol_balance["sol"],
            "usd_price": sol_usd_price,
            "usd_value": sol_usd_value,
            "allocation_pct": allocation_pct(sol_usd_value),
        },
        "holdings": holdings,
        "total_usd_value": total_usd_value,
        "unpriced_holdings": sum(1 for h in holdings if h["usd_value"] is None),
        "partial": partial,
    }

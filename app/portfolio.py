"""Deterministic portfolio valuation. No LLM involved: balance math and USD
pricing happen here in typed Python, matching the policy that DSPy must never
perform balance arithmetic.
"""

import asyncio

from app.jupiter import jupiter
from app.solana_rpc import get_sol_balance, get_token_accounts

WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"


class Holding(dict):
    """Typed alias kept as a plain dict for JSON responses; see build_portfolio_snapshot."""


async def _price_lookup(mint: str) -> dict | None:
    try:
        return await jupiter.token_by_mint(mint)
    except Exception:
        return None


async def build_portfolio_snapshot(wallet_address: str) -> dict:
    """Aggregate SOL + SPL balances with USD pricing and allocation percentages."""
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

    mints_to_price = [WRAPPED_SOL_MINT] + [h["mint"] for h in raw_holdings]
    priced = await asyncio.gather(*(_price_lookup(mint) for mint in mints_to_price))
    price_by_mint = {mint: info for mint, info in zip(mints_to_price, priced) if info is not None}

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
    }

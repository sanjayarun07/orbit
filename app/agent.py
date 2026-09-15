import asyncio

from app.jupiter import jupiter
from app.portfolio import build_portfolio_snapshot
from app.solana_rpc import get_sol_balance, get_token_accounts


def _run(coro):
    """DSPy tools are synchronous; bridge the small async HTTP functions safely."""
    return asyncio.run(coro)


def sol_balance(wallet_address: str) -> dict:
    """Read the confirmed SOL balance for a Solana wallet address."""
    return _run(get_sol_balance(wallet_address))


# These tools feed a ReAct trajectory, so their output is re-sent to the model
# on every later step of the loop. A whale wallet (15k+ token accounts, verified
# live) made each subsequent LLM call take 20-40 s and the turn 504 -- so the
# model only ever sees the largest positions plus an honest count of the rest.
LLM_MAX_HOLDINGS = 50


def spl_balances(wallet_address: str) -> dict:
    """Read confirmed SPL token accounts for a Solana wallet address.

    Returns the largest `LLM_MAX_HOLDINGS` positions by token amount as
    `{mint, amount}` plus `token_account_count` / `truncated` so a wallet with
    thousands of accounts is summarised, not dumped."""
    accounts = _run(get_token_accounts(wallet_address))
    holdings = []
    for entry in accounts.get("value", []):
        parsed = entry["account"]["data"]["parsed"]["info"]
        amount = float(parsed["tokenAmount"]["uiAmount"] or 0)
        if amount > 0:
            holdings.append({"mint": parsed["mint"], "amount": amount})
    holdings.sort(key=lambda h: -h["amount"])
    return {
        "token_account_count": len(holdings),
        "holdings": holdings[:LLM_MAX_HOLDINGS],
        "truncated": len(holdings) > LLM_MAX_HOLDINGS,
    }


def portfolio_snapshot(wallet_address: str) -> dict:
    """Return SOL + SPL holdings with USD pricing and allocation already computed.

    All arithmetic (USD values, allocation percentages, totals) is done here in
    deterministic Python; never recompute or second-guess these numbers. Only
    the top `LLM_MAX_HOLDINGS` holdings by USD value are listed; `holdings_omitted`
    counts the rest (totals still include them).
    """
    snapshot = _run(build_portfolio_snapshot(wallet_address))
    holdings = snapshot.get("holdings", [])
    if len(holdings) > LLM_MAX_HOLDINGS:
        snapshot = {**snapshot, "holdings": holdings[:LLM_MAX_HOLDINGS], "holdings_omitted": len(holdings) - LLM_MAX_HOLDINGS}
    return snapshot


def search_verified_tokens(query: str) -> list[dict]:
    """Search Jupiter tokens. Return candidates for user review; never choose among ambiguous tickers."""
    matches = _run(jupiter.search_tokens(query))[:10]
    return [
        {
            "mint": item.get("id"),
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "decimals": item.get("decimals"),
            "usd_price": item.get("usdPrice"),
            "tags": item.get("tags", []),
            "organic_score": item.get("organicScore"),
        }
        for item in matches
    ]


def token_safety_warnings(mint: str) -> dict:
    """Get Jupiter Shield warnings for one exact Solana token mint."""
    return _run(jupiter.shield([mint]))


def token_identity(mint: str) -> dict | None:
    """Full Jupiter token-registry record for one exact mint (identity, tags,
    verification, organic score, holder count, audit, and mint/freeze
    authorities). None when the mint is not uniquely in the registry."""
    try:
        return _run(jupiter.token_by_mint(mint))
    except Exception:
        return None



def token_top_holders_rpc(mint: str) -> dict:
    """Largest token accounts for a Solana mint straight from the RPC node, with
    the owning wallet and share of supply. Keyless fallback for the paid
    holders providers (verified live: Bitquery answered 402 out of credits and
    Solana had no other holders source). Accounts are token accounts -- a pool
    or exchange can own several -- so shares are per account, not per entity."""
    from app.solana_rpc import get_account_owners, get_token_largest_accounts, get_token_supply

    async def _collect():
        largest, supply = await asyncio.gather(get_token_largest_accounts(mint), get_token_supply(mint))
        owners = await get_account_owners([entry["address"] for entry in largest])
        return largest, supply, owners

    largest, supply, owners = _run(_collect())
    total = float(supply.get("uiAmount") or 0) or None
    holders = []
    for entry in largest:
        amount = float(entry.get("uiAmount") or 0)
        holders.append({
            "token_account": entry.get("address"),
            "owner": owners.get(entry.get("address")),
            "amount": amount,
            "supply_pct": round(amount / total * 100, 4) if total else None,
        })
    top10 = sum(h["supply_pct"] or 0 for h in holders[:10]) if total else None
    return {
        "mint": mint,
        "total_supply": total,
        "decimals": supply.get("decimals"),
        "holders": holders,
        "top10_supply_pct": round(top10, 2) if top10 is not None else None,
    }

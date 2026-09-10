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


def spl_balances(wallet_address: str) -> dict:
    """Read confirmed SPL token accounts for a Solana wallet address."""
    return _run(get_token_accounts(wallet_address))


def portfolio_snapshot(wallet_address: str) -> dict:
    """Return SOL + SPL holdings with USD pricing and allocation already computed.

    All arithmetic (USD values, allocation percentages, totals) is done here in
    deterministic Python; never recompute or second-guess these numbers.
    """
    return _run(build_portfolio_snapshot(wallet_address))


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


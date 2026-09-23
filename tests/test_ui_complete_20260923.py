"""The complete UI review of 2026-09-23: the portfolio reads both token
programs and says when a read is partial; price-change columns say price;
small prices keep their figures; the priced token is named; holder dates and
counts mean what they say; a price alert needs a positive finite price."""
import asyncio

import pytest

from app import dexscreener_tools, geckoterminal_tools, mobula_meme, portfolio, tasks
from app.nodes import portfolio as portfolio_node, runtime

WALLET = "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS"
ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def _account(mint, amount, ui, decimals, program):
    return {"account": {"data": {"program": program, "parsed": {"info": {"mint": mint, "tokenAmount": {"amount": str(amount), "uiAmount": ui, "decimals": decimals}}}}}}


@pytest.fixture
def chain(monkeypatch):
    state = {"legacy": {"value": []}, "t22": {"value": [_account(ANSEM, 11522238, 11.522238, 6, "spl-token-2022")]}}

    async def legacy(wallet):
        if isinstance(state["legacy"], Exception):
            raise state["legacy"]
        return state["legacy"]
    async def t22(wallet):
        if isinstance(state["t22"], Exception):
            raise state["t22"]
        return state["t22"]
    async def sol(wallet):
        return {"sol": 0.073158834, "lamports": 73158834}
    async def prices(mints):
        return {m: {"symbol": "ANSEM" if m == ANSEM else "SOL", "usdPrice": 0.18 if m == ANSEM else 118.0, "tags": ["verified"]} for m in mints}
    monkeypatch.setattr(portfolio, "get_token_accounts", legacy)
    monkeypatch.setattr(portfolio, "get_token_accounts_2022", t22)
    monkeypatch.setattr(portfolio, "get_sol_balance", sol)
    monkeypatch.setattr(portfolio, "_price_mints", prices)
    return state


def test_the_portfolio_reads_token_2022_holdings_too(chain):
    snap = asyncio.run(portfolio.build_portfolio_snapshot(WALLET))
    assert [h["symbol"] for h in snap["holdings"]] == ["ANSEM"] and snap["partial"] is False and snap["unread_programs"] == []
    assert snap["sol"]["allocation_pct"] < 100 and abs(snap["sol"]["allocation_pct"] + snap["holdings"][0]["allocation_pct"] - 100) < 0.01


def test_a_failed_program_read_makes_the_snapshot_partial_not_empty(chain):
    chain["t22"] = RuntimeError("rpc timeout")
    snap = asyncio.run(portfolio.build_portfolio_snapshot(WALLET))
    assert snap["holdings"] == [] and snap["partial"] is True and snap["unread_programs"] == ["spl-token-2022"]
    state = {"wallet_address": WALLET, "request": "Analyze my portfolio. Include all token holdings", "capabilities": ["token_holdings"], "session_context": {}, "history": ""}
    out = asyncio.run(portfolio_node.portfolio_node(state))
    assert out["answer"].startswith("**Snapshot partial**") and "not an empty wallet" in out["answer"]
    chain["t22"] = {"value": []}
    out = asyncio.run(portfolio_node.portfolio_node(state))
    assert "no SPL token holdings** (both token programs read)" in out["answer"]
    chain["legacy"] = RuntimeError("down"); chain["t22"] = RuntimeError("down")
    with pytest.raises(RuntimeError):
        asyncio.run(portfolio.build_portfolio_snapshot(WALLET))


def test_price_change_columns_say_price_and_small_prices_keep_their_figures():
    assert dexscreener_tools._money(0.0000063) == "$0.000006300" and dexscreener_tools._money(0.00001234) == "$0.00001234"
    assert dexscreener_tools._money(1.5) == "$1.50" and dexscreener_tools._money(0) == "$0" and geckoterminal_tools._money(0.0000063) == "$0.000006300"
    pairs = [{"baseToken": {"symbol": "PCAT"}, "quoteToken": {"symbol": "PEPE"}, "chainId": "solana", "dexId": "raydium", "priceUsd": "0.0021",
              "volume": {"h24": 1000}, "liquidity": {"usd": 5000}, "priceChange": {"h24": 12.5}, "url": "https://dexscreener.com/x"}]
    card = dexscreener_tools.dexscreener_pair_search_from_pairs(pairs)
    assert "| Pair | Chain / DEX | Priced token | Price |" in card and "| PCAT | $0.002100 |" in card and "prices the other token" in card


def test_holder_table_separates_first_trade_from_wallet_funding_and_unknown_counts(monkeypatch):
    rows = [{"walletAddress": "So11111111111111111111111111111111111111112", "percentageOfTotalSupply": 3.2, "tokenAmountUSD": 1000.0,
             "unrealizedPnlUSD": 1000.0, "walletFundAt": "2020-10-23T00:00:00Z", "labels": []}]
    monkeypatch.setattr(mobula_meme, "_get", lambda path, params: rows)
    monkeypatch.setattr(mobula_meme, "_subject", lambda request: (ANSEM, "solana"))
    card = mobula_meme.token_holders(f"top holders of {ANSEM}")
    assert "| First token trade | Wallet funded |" in card and "| —/— | $1.0K | — | 2020-10-23 |" in card
    assert "means Mobula returned no count, not zero trades" in card and "no cost basis was found" in card


def test_a_price_alert_needs_a_positive_finite_price():
    for bad in (-1, 0, "nan", "inf", "abc", None):
        with pytest.raises(ValueError):
            tasks.validate_spec("price_alert", {"symbol": "SOL", "op": "<", "price": bad})
    with pytest.raises(ValueError):
        tasks.validate_spec("price_alert", {"symbol": "", "op": "<", "price": 100})
    with pytest.raises(ValueError):
        tasks.validate_spec("price_alert", {"symbol": "SOL", "op": "<=", "price": 100})
    assert tasks.validate_spec("price_alert", {"symbol": " sol ", "op": ">", "price": "150"}) == {"symbol": "sol", "op": ">", "price": 150.0}
    with pytest.raises(ValueError):
        tasks.validate_spec("reminder", {"message": " "})


def test_the_desk_and_the_synthesis_carry_the_unit_rules():
    assert "never a support, resistance or invalidation PRICE" in runtime.MarketResearch.__doc__
    assert "PRICE move, never volume growth" in runtime.CompositeSynthesis.__doc__

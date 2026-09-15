import asyncio

import pytest

from app import portfolio
from app.settings import settings

WSOL = portfolio.WRAPPED_SOL_MINT


def _accounts(n):
    return {"value": [{"account": {"data": {"parsed": {"info": {"mint": f"Mint{i:040d}", "tokenAmount": {"uiAmount": float(n - i)}}}}}} for i in range(n)]}


@pytest.fixture
def rpc(monkeypatch):
    async def sol(_w):
        return {"sol": 2.0}

    state = {"n": 5}

    async def accounts(_w):
        return _accounts(state["n"])

    monkeypatch.setattr(portfolio, "get_sol_balance", sol)
    monkeypatch.setattr(portfolio, "get_token_accounts", accounts)
    return state


def test_whale_wallet_is_priced_in_batches_and_capped(rpc, monkeypatch):
    rpc["n"] = 1000
    monkeypatch.setattr(settings, "portfolio_max_priced_holdings", 250)
    batches = []

    async def tokens_by_mints(mints):
        batches.append(len(mints))
        return {m: {"symbol": "T", "usdPrice": 1.0, "tags": ["verified"]} for m in mints}

    async def never(_m):
        raise AssertionError("no per-mint fallback when the batch answered everything")

    monkeypatch.setattr(portfolio.jupiter, "tokens_by_mints", tokens_by_mints)
    monkeypatch.setattr(portfolio.jupiter, "token_by_mint", never)
    snap = asyncio.run(portfolio.build_portfolio_snapshot("w"))
    assert batches == [251]  # one batched call: WSOL + the 250 largest balances, not 1000 requests
    assert snap["partial"] is True and len(snap["holdings"]) == 1000
    assert snap["unpriced_holdings"] == 750 and snap["holdings"][0]["amount"] == 1000.0  # largest first


def test_batch_misses_get_a_bounded_per_mint_retry(rpc, monkeypatch):
    rpc["n"] = 30
    retried = []

    async def tokens_by_mints(mints):
        return {m: {"symbol": "T", "usdPrice": 1.0} for m in mints[:5]}

    async def token_by_mint(m):
        retried.append(m)
        return {"symbol": "R", "usdPrice": 2.0}

    monkeypatch.setattr(portfolio.jupiter, "tokens_by_mints", tokens_by_mints)
    monkeypatch.setattr(portfolio.jupiter, "token_by_mint", token_by_mint)
    snap = asyncio.run(portfolio.build_portfolio_snapshot("w"))
    assert len(retried) == portfolio._FALLBACK_LOOKUPS
    assert snap["partial"] is False and snap["unpriced_holdings"] == 30 - 5 - portfolio._FALLBACK_LOOKUPS + 1  # WSOL took one of the 5 batch slots


def test_pricing_timeout_returns_an_unpriced_partial_snapshot(rpc, monkeypatch):
    rpc["n"] = 3
    monkeypatch.setattr(settings, "portfolio_snapshot_timeout_seconds", 0.05)

    async def slow(mints):
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(portfolio.jupiter, "tokens_by_mints", slow)
    snap = asyncio.run(portfolio.build_portfolio_snapshot("w"))
    assert snap["partial"] is True and snap["total_usd_value"] is None and len(snap["holdings"]) == 3


def test_risk_node_does_not_wait_on_a_slow_snapshot(monkeypatch):
    from types import SimpleNamespace
    from app.nodes import runtime, trading

    monkeypatch.setattr(settings, "risk_snapshot_timeout_seconds", 0.05)

    async def slow(_w):
        await asyncio.sleep(1)
        return {"total_usd_value": 100.0}

    async def fake_call_lm(_program, **kw):
        assert kw["portfolio_context"] == "unknown"
        return SimpleNamespace(verdict="ok", summary="fine", blocked_reason=None)

    monkeypatch.setattr(trading, "build_portfolio_snapshot", slow)
    monkeypatch.setattr(runtime, "_call_lm", fake_call_lm)
    plan = SimpleNamespace(plan_id="p", input_token=SimpleNamespace(symbol="SOL", decimals=9), output_token=SimpleNamespace(symbol="USDC", verified=True),
                           proposal=SimpleNamespace(amount_atomic=1, slippage_bps=50), input_value_usd=5.0, quote={}, warnings=[])
    out = asyncio.run(trading.charter_risk_node({"trade_plan": plan, "wallet_address": "w", "session_context": {"risk_charter": "max $10 per trade", "risk_charter_fields": {"max_position_pct": 1.0, "notes": "x"}}}))
    assert out["risk_assessment"].verdict == "ok"  # % rule unevaluable -> not a violation; quote not blocked

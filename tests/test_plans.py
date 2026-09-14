from datetime import datetime, timedelta, timezone

from app.models import SwapProposal, TradePlan
from app.jupiter import WRAPPED_SOL_MINT, normalize_mint
from app import plans


def test_trade_plan_confirmation_is_exact():
    plan = TradePlan(
        plan_id="abc",
        status="pending_confirmation",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        wallet_address="wallet",
        proposal=SwapProposal(
            input_mint="in", output_mint="out", amount_atomic=1, slippage_bps=50, reason="test"
        ),
        quote={},
        input_token={"mint": "in", "symbol": "IN", "name": "Input", "decimals": 9},
        output_token={"mint": "out", "symbol": "OUT", "name": "Output", "decimals": 6},
        simulation={"ok": True},
        confirmation_text="CONFIRM abc",
    )
    assert plan.confirmation_text == "CONFIRM abc"


def test_mint_normalization_removes_model_formatting_and_maps_native_sol():
    assert normalize_mint(f'"{WRAPPED_SOL_MINT}"') == WRAPPED_SOL_MINT
    assert normalize_mint(f"`{WRAPPED_SOL_MINT}`") == WRAPPED_SOL_MINT
    assert normalize_mint("SOL") == WRAPPED_SOL_MINT


def _token_payload(mint, symbol, decimals, usd_price):
    return {"id": mint, "symbol": symbol, "name": symbol, "decimals": decimals, "usdPrice": usd_price, "tags": ["verified"]}


def test_simulate_swap_computes_real_quote_math(monkeypatch):
    import asyncio

    calls = []

    async def fake_token_by_mint(mint):
        calls.append(("token_by_mint", mint))
        if mint == WRAPPED_SOL_MINT:
            return _token_payload(WRAPPED_SOL_MINT, "SOL", 9, 150.0)
        return _token_payload("USDC_MINT", "USDC", 6, 1.0)

    async def fake_quote(input_mint, output_mint, amount, slippage_bps):
        calls.append(("quote", input_mint, output_mint, amount, slippage_bps))
        return {"outAmount": "74500000", "priceImpactPct": "0.002"}  # 74.5 USDC (6 decimals)

    monkeypatch.setattr(plans.jupiter, "token_by_mint", fake_token_by_mint)
    monkeypatch.setattr(plans.jupiter, "quote", fake_quote)

    result = asyncio.run(plans.simulate_swap(WRAPPED_SOL_MINT, "USDC_MINT", 500_000_000))  # 0.5 SOL

    assert result["input_amount"] == 0.5
    assert result["input_value_usd"] == 75.0  # 0.5 SOL * $150
    assert result["output_amount"] == 74.5
    assert result["output_value_usd"] == 74.5  # 74.5 USDC * $1
    assert result["price_impact_pct"] == 0.2
    assert ("quote", WRAPPED_SOL_MINT, "USDC_MINT", 500_000_000, 50) in calls  # 50 bps default slippage


def test_simulate_swap_never_touches_the_real_execution_pipeline(monkeypatch):
    """Nothing here may build a signable transaction, simulate one via RPC,
    or create/store a TradePlan -- verified by making every one of those
    functions fail the test if called at all.
    """
    import asyncio

    async def fake_token_by_mint(mint):
        return _token_payload(mint, "TOK", 9, 1.0)

    async def fake_quote(*_args, **_kwargs):
        return {"outAmount": "1000000000", "priceImpactPct": "0.01"}

    def fail_if_called(name):
        def _fail(*_args, **_kwargs):
            raise AssertionError(f"simulate_swap must never call {name}")
        return _fail

    monkeypatch.setattr(plans.jupiter, "token_by_mint", fake_token_by_mint)
    monkeypatch.setattr(plans.jupiter, "quote", fake_quote)
    monkeypatch.setattr(plans.jupiter, "swap_transaction", fail_if_called("jupiter.swap_transaction"))
    monkeypatch.setattr(plans, "simulate_transaction", fail_if_called("simulate_transaction"))
    monkeypatch.setattr(plans, "_store_plan", fail_if_called("_store_plan"))
    monkeypatch.setattr(plans, "store_prepared_transaction", fail_if_called("store_prepared_transaction"))

    result = asyncio.run(plans.simulate_swap("in", "out", 1_000_000_000))
    assert result["output_amount"] == 1.0


def test_only_one_concurrent_submission_can_claim_a_memory_plan(monkeypatch):
    plan = TradePlan(
        plan_id="atomic-plan",
        status="pending_confirmation",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        wallet_address="wallet",
        proposal=SwapProposal(
            input_mint="in", output_mint="out", amount_atomic=1, slippage_bps=50, reason="test"
        ),
        quote={},
        input_token={"mint": "in", "symbol": "IN", "name": "Input", "decimals": 9},
        output_token={"mint": "out", "symbol": "OUT", "name": "Output", "decimals": 6},
        simulation={"ok": True},
        confirmation_text="CONFIRM atomic-plan",
    )

    async def no_pool():
        return None

    monkeypatch.setattr(plans, "get_pg_pool", no_pool)
    plans._plans[plan.plan_id] = plan

    async def race():
        return await __import__("asyncio").gather(
            plans.claim_plan_submission(plan),
            plans.claim_plan_submission(plan),
            return_exceptions=True,
        )

    results = __import__("asyncio").run(race())
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1

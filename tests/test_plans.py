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

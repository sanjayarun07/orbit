"""The planner asked "what slippage, in basis points?", the user typed "50",
and the router answered "Could you add a bit more detail". A bare number has
no words, so it was never a parameter fragment; it now continues the trade
being collected and is read as the slippage that was asked for -- only when
the amount is already known, since with the amount still open a bare number
could be either, and the planner asks rather than guesses.
"""
import asyncio

import pytest

from app import trade_context
from app.routing import resolver
from app.routing.speech import bare_number_bps, is_parameter_fragment

COLLECTING = {"intent": "trade", "status": "collecting_details", "source_chain": "solana", "destination_chain": "solana",
              "amount": "0.01", "input_token": "sol", "output_token": "ANSEM", "slippage_bps": None}


@pytest.mark.parametrize("text,bps", [("50", 50), ("50 bps", 50), ("0.5%", 50), ("1%", 100), ("50 basis points", 50), ("hi", None), ("20000", None), ("swap 50 sol", None)])
def test_a_bare_number_reads_as_basis_points_and_a_percent_converts(text, bps):
    assert bare_number_bps(text) == bps


def test_a_bare_number_continues_the_trade_being_collected_without_a_model_call():
    async def never(program, **kwargs):
        raise AssertionError("a slippage answer must not be re-classified by the model")

    assert is_parameter_fragment("50") and not is_parameter_fragment("hi")
    out = asyncio.run(resolver.resolve({"request": "50", "history": "", "session_context": {"active_workflow": COLLECTING}}, never))
    assert out["intent"] in {"trade", "cross_chain_swap"} and out["route_source"] == "session_context"


def test_a_bare_number_with_no_trade_in_progress_is_not_a_fragment_route(monkeypatch):
    async def model(program, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(understanding={"speech_act": "abstain", "domain": "general", "explicit_action": False, "confidence": 0.3})

    out = asyncio.run(resolver.resolve({"request": "50", "history": "", "session_context": {}}, model))
    assert out["intent"] == "general"


def test_the_jupiter_fields_take_the_bare_number_as_slippage_only_when_the_amount_is_known():
    history = "user: swap 0.01 sol to ANSEM on SOL\nassistant: To prepare your swap, please let me know your desired slippage tolerance in basis points."
    mint = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
    _, out, amount, bps = trade_context.complete_swap_fields("50", history, "So11111111111111111111111111111111111111112", mint, 10_000_000, None)
    assert bps == 50 and amount == 10_000_000 and out == mint
    _, _, amount, bps = trade_context.complete_swap_fields("50", history, None, mint, None, None)
    assert bps is None and amount is None, "with the amount still open the number is ambiguous; the planner asks"
    _, _, _, bps = trade_context.complete_swap_fields("0.5%", history, None, mint, 10_000_000, None)
    assert bps == 50


def test_the_relay_draft_takes_the_bare_number_as_slippage_and_keeps_its_amount(monkeypatch):
    from app.nodes import trading
    from app.settings import settings
    monkeypatch.setattr(settings, "deployment_mode", "execution")
    monkeypatch.setattr(settings, "live_trading", True)
    active = {"intent": "cross_chain_swap", "status": "collecting_details", "source_chain": "solana", "destination_chain": "base",
              "amount": "0.01", "input_token": "sol", "output_token": "usdc", "slippage_bps": None}
    out = asyncio.run(trading.cross_chain_swap_node({"request": "50", "wallet_address": "wallet", "chains": ["solana", "base"],
                                                     "history": "", "session_context": {"active_workflow": active}}))
    draft = out["cross_chain_swap"]
    assert draft is not None and draft.slippage_bps == 50 and str(draft.amount) == "0.01", draft

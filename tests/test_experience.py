from datetime import datetime, timedelta, timezone

from app.experience import (
    build_context_capsules,
    build_evidence_summary,
    build_gas_advisory,
    build_intent_lock,
    build_trade_readiness,
)
from app.models import CrossChainSwapDraft, SwapProposal, TokenInfo, TradePlan


def _plan() -> TradePlan:
    now = datetime.now(timezone.utc)
    return TradePlan(
        plan_id="plan", status="pending_confirmation", created_at=now,
        expires_at=now + timedelta(minutes=1), wallet_address="wallet",
        proposal=SwapProposal(input_mint="So11111111111111111111111111111111111111112", output_mint="9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", amount_atomic=20_000_000, slippage_bps=50, reason="test"),
        quote={}, input_token=TokenInfo(mint="So11111111111111111111111111111111111111112", symbol="SOL", name="Solana", decimals=9, usd_price=100, verified=True),
        output_token=TokenInfo(mint="9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", symbol="ANSEM", name="The Black Bull", decimals=6),
        warnings=[], simulation={"ok": True}, confirmation_text="CONFIRM plan",
    )


def test_trade_experience_is_deterministic():
    plan = _plan()
    lock = build_intent_lock(plan, None, "wallet")
    assert lock and lock.source_chain == "solana" and lock.fingerprint
    assert lock.amount == "0.02"
    assert build_intent_lock(plan, None, "wallet").fingerprint == lock.fingerprint
    capsules = build_context_capsules("buy it", "", "", plan, None)
    assert [item.label for item in capsules] == ["SOL", "ANSEM"]
    assert build_trade_readiness(plan).level == "caution"


def test_cross_chain_gas_advisory_and_evidence():
    draft = CrossChainSwapDraft(source_chain="solana", destination_chain="base", amount="0.1", input_token="SOL", output_token="USDC", slippage_bps=10)
    assert build_gas_advisory(draft).native_token == "ETH"
    evidence = build_evidence_summary({"tool_name_0": "perplexity_web_search", "observation_0": "Fresh result"})
    assert evidence and evidence.providers == ["perplexity"] and evidence.confidence == "medium"


def test_new_named_token_capsule_does_not_reuse_stale_history_address():
    stale = "0x8260000000000000000000000000000000000087"
    capsules = build_context_capsules(
        "what is ANSEM token on Solana",
        f"user: Check token safety for {stale} on Robinhood",
        "ANSEM/SOL market data",
        None,
        None,
    )
    token = next(item for item in capsules if item.kind == "token")
    assert token.label == "ANSEM"
    assert token.chain == "solana"
    assert token.address is None


def test_connected_wallet_capsule_does_not_parse_an_article_id_as_wallet():
    wallet = "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS"
    capsules = build_context_capsules(
        "Show my recent transactions",
        "",
        "Source https://example.com/article/4139b7912dbf1eebdcfde8c1cc12f23d",
        None,
        None,
        wallet,
    )
    reference = next(item for item in capsules if item.kind == "wallet")
    assert reference.address == wallet
    assert reference.chain == "solana"

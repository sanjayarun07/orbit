"""Decision receipts (app/decision_records.py) and the no-data contract in the
router (app/provider_router.NoData)."""
import asyncio

from app import decision_records, mobula_meme
from app.provider_router import NoData, ProviderRouter, ProviderTool
from app.signals import Signal, Subject, SubjectSkip

_BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
NOW = "2026-09-20T10:00:00+00:00"
# Captured at import, before the offline fixture replaces them.
_REAL = {"holders": mobula_meme.token_holders, "trades": mobula_meme.token_trades,
         "first": mobula_meme.token_first_buyers, "bundle": mobula_meme.token_bundle_check}


def setup_function():
    decision_records.reset_for_test()


def _subject():
    return Subject(kind="token", id=_BONK, chain="solana", symbol="BONK")


# ---- receipts ----

def test_a_receipt_holds_every_vote_every_skip_and_the_verdict():
    s = _subject()
    votes = [Signal.from_stance("token_deep_dive", s, NOW, "bullish", "medium", reasoning="Liquid, locked, organic."),
             Signal.abstain("bundle_check", s, NOW, "Mobula has no first buyers for this token")]
    row = asyncio.run(decision_records.record(
        kind="deep_dive", subject=s, signals=votes, verdict="Bottom line: fine.\nWhat would flip this: LP unlock.",
        coverage=[{"name": "market", "status": "available", "source": "birdeye"}, {"name": "unlocks", "status": "unavailable", "source": None}],
        skipped=[SubjectSkip(subject="Token unlocks", reason="Not tracked by DefiLlama")], price=0.0000123, user_id="u1"))
    assert row["subject_key"] == f"solana:{_BONK.lower()}" and row["user_id"] == "u1"
    receipt = decision_records.why(row)
    assert "token_deep_dive | bullish | +0.60 | Liquid, locked, organic." in receipt
    assert "bundle_check | abstained | — | Mobula has no first buyers" in receipt
    assert "- Token unlocks: Not tracked by DefiLlama" in receipt
    assert "Evidence used: market" in receipt and "LP unlock" in receipt
    assert "user_id" not in decision_records.public(row)


def test_receipts_are_per_user_and_per_subject_newest_first():
    s = _subject()
    for i, user in enumerate(("u1", "u1", "u2")):
        asyncio.run(decision_records.record(kind="deep_dive", subject=s, signals=[], verdict=f"v{i}", user_id=user))
    mine = asyncio.run(decision_records.list_for("u1"))
    assert [r["verdict"] for r in mine] == ["v1", "v0"]
    assert [r["verdict"] for r in asyncio.run(decision_records.for_subject(s.key))] == ["v2", "v1", "v0"]
    assert [r["verdict"] for r in asyncio.run(decision_records.for_subject(s.key, user_id="u2"))] == ["v2"]


def test_the_bound_turn_user_owns_the_receipt_and_anonymous_turns_have_none():
    token = decision_records.bind_turn("u9")
    try:
        row = asyncio.run(decision_records.record(kind="deep_dive", subject=_subject(), signals=[], verdict="x"))
    finally:
        decision_records.current_user.reset(token)
    assert row["user_id"] == "u9"
    anon = asyncio.run(decision_records.record(kind="deep_dive", subject=_subject(), signals=[], verdict="y"))
    assert anon["user_id"] is None


# ---- no data is not a failure ----

def _router_with(handler_a, handler_b):
    router = ProviderRouter()
    router.register(ProviderTool(name="a", provider="pa", capabilities=("cap",), handler=handler_a, priority=10))
    router.register(ProviderTool(name="b", provider="pb", capabilities=("cap",), handler=handler_b, priority=1))
    return router


def test_no_data_is_a_healthy_call_that_still_tries_the_next_provider():
    def empty(_req):
        raise NoData("nothing indexed")

    router = _router_with(empty, lambda req: "# Card\nb has it")
    out = router.try_route("holders of X", "cap")
    assert out is not None and out.tool == "b"
    assert out.failures == ("a: no data (nothing indexed)",)
    health = router._health["a"]
    assert health.calls == 1 and health.successes == 1 and health.consecutive_failures == 0  # no circuit-breaker step


def test_a_real_failure_still_counts_against_the_provider():
    def broken(_req):
        raise RuntimeError("boom")

    router = _router_with(broken, lambda req: "# Card\nb has it")
    out = router.try_route("holders of X", "cap")
    assert out.failures == ("a: provider request failed",)
    assert router._health["a"].consecutive_failures == 1


def test_no_data_is_a_runtime_error_for_callers_that_catch_the_old_way():
    assert issubclass(NoData, RuntimeError)


def test_mobula_empty_results_raise_no_data(monkeypatch):
    monkeypatch.setattr(mobula_meme, "_get", lambda path, params: [])
    monkeypatch.setattr(mobula_meme, "_get_v1", lambda path, params: [])
    for fn, req in ((_REAL["holders"], f"top holders of {_BONK} on solana"),
                    (_REAL["trades"], f"latest trades {_BONK} on solana"),
                    (_REAL["first"], f"first buyers of {_BONK} on solana"),
                    (_REAL["bundle"], f"is {_BONK} bundled on solana")):
        try:
            fn(req)
        except NoData:
            continue
        raise AssertionError(f"{fn.__name__} did not raise NoData on an empty result")


# ---- the bundle check as a vote ----

def _buyer(addr, second, current="1000"):
    return {"address": addr, "initialAmount": "1000", "currentBalance": current, "firstHoldingDate": f"2026-09-18T12:00:{second:02d}.000Z", "tags": []}


def test_bundle_vote_and_card_come_from_one_analysis(monkeypatch):
    buyers = [_buyer(f"W{i:02d}" + "A" * 30, 5) for i in range(4)] + [_buyer(f"S{i:02d}" + "A" * 30, 10 + i) for i in range(6)]
    funding = {b["address"]: {"from": "FUNDER" + "F" * 30} for b in buyers[:4]}
    monkeypatch.setattr(mobula_meme, "_get_v1", lambda path, params: buyers)
    monkeypatch.setattr(mobula_meme, "_get", lambda path, params: funding.get(params["wallet"]))
    analysis = mobula_meme._bundle_analysis(_BONK, "solana")
    assert analysis.level == "strong"
    vote = mobula_meme.bundle_signal(_subject(), NOW, analysis)
    assert vote.value == -0.8 and vote.components["funder_and_second_overlap"] == 4.0 and not vote.abstained
    assert "Strong in the sample" in vote.reasoning
    assert "Bundle evidence: **Strong** in the sample" in mobula_meme._render_bundle(analysis)


def test_bundle_vote_abstains_without_first_buyers(monkeypatch):
    monkeypatch.setattr(mobula_meme, "_get_v1", lambda path, params: [])
    vote = mobula_meme.bundle_signal(_subject(), NOW)
    assert vote.abstained and "no first buyers" in vote.metadata["abstain_reason"]


def test_a_clean_launch_is_a_real_neutral_vote(monkeypatch):
    buyers = [_buyer(f"O{i:02d}" + "A" * 30, i * 3) for i in range(8)]
    monkeypatch.setattr(mobula_meme, "_get_v1", lambda path, params: buyers)
    monkeypatch.setattr(mobula_meme, "_get", lambda path, params: None)
    vote = mobula_meme.bundle_signal(_subject(), NOW)
    assert vote.value == 0.0 and not vote.abstained and vote.metadata["level"] == "none"

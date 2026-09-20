"""The typed analyst contract and the pure book arithmetic (app/signals.py)."""
import math

import pytest

from app.signals import (
    ClampEvent, RiskLimits, Signal, Subject, SubjectSkip, apply_limits, blend_signals, limits_from_charter,
)

_BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
NOW = "2026-09-20T10:00:00+00:00"


def _token(symbol="BONK", address=_BONK, chain="solana"):
    return Subject(kind="token", id=address, chain=chain, symbol=symbol)


def _sig(model, subject, value, **meta):
    return Signal(model_name=model, subject=subject, as_of=NOW, value=value, metadata={"abstained": False, **meta})


# ---- subjects ----

def test_subject_keys_are_chain_scoped_for_tokens_and_ticker_for_equities():
    assert _token().key == f"solana:{_BONK.lower()}"
    assert Subject(kind="equity", id="aapl").key == "equity:AAPL"
    assert Subject(kind="wallet", id="0xAbC", chain="base").key == "base:0xabc"
    with pytest.raises(ValueError):
        Subject(kind="token", id="  ")


# ---- signals ----

def test_conviction_is_bounded_and_never_nan():
    with pytest.raises(ValueError):
        _sig("m", _token(), 1.5)
    with pytest.raises(ValueError):
        _sig("m", _token(), math.nan)
    assert _sig("m", _token(), -1.0).value == -1.0


def test_stance_and_confidence_fold_into_a_conviction():
    s = Signal.from_stance("lens", _token(), NOW, "Bullish", "High", reasoning="moat")
    assert s.value == 0.9 and not s.abstained and s.metadata["stance"] == "bullish"
    assert Signal.from_stance("lens", _token(), NOW, "bearish", "low").value == -0.3
    assert Signal.from_stance("lens", _token(), NOW, "neutral", "high").value == 0.0
    # A persona's 0-100 confidence uses the same door.
    assert Signal.from_stance("buffett", Subject(kind="equity", id="AAPL"), NOW, "bullish", 78).value == pytest.approx(0.78)


def test_an_unparsed_stance_abstains_instead_of_guessing():
    for stance, conf in (("", "high"), ("bullish-ish", "high"), ("bullish", "very"), ("bullish", 140), ("bullish", True)):
        s = Signal.from_stance("lens", _token(), NOW, stance, conf)
        assert s.abstained and s.value == 0.0, (stance, conf)
        assert "not parsed" in s.metadata["abstain_reason"] or "out of range" in s.metadata["abstain_reason"]


# ---- blend ----

def test_abstains_leave_the_blend_while_real_neutrals_dilute():
    t = _token()
    voting = [_sig("a", t, 0.8), Signal.abstain("b", t, NOW, "no data")]
    assert blend_signals(voting).convictions[t.key] == pytest.approx(0.8)      # abstain excluded from the mean
    diluted = [_sig("a", t, 0.8), _sig("bundle_check", t, 0.0)]
    assert blend_signals(diluted).convictions[t.key] == pytest.approx(0.4)     # neutral vote counts


def test_blend_weights_models_and_scales_to_the_gross_target():
    t = _token()
    signals = [_sig("graham", t, 1.0), _sig("buffett", t, 0.0)]
    out = blend_signals(signals, {"graham": 2.0, "buffett": 1.0}, gross_target=0.5)
    assert out.convictions[t.key] == pytest.approx(2 / 3)
    assert out.weights[t.key] == pytest.approx(0.5)                             # one name takes the whole target


def test_market_neutral_demeans_and_uniform_views_go_flat():
    a, b = _token("A", "A" * 32), _token("B", "B" * 32)
    out = blend_signals([_sig("m", a, 0.9), _sig("m", b, 0.1)], market_neutral=True)
    assert out.weights[a.key] == pytest.approx(0.5) and out.weights[b.key] == pytest.approx(-0.5)
    flat = blend_signals([_sig("m", a, 0.7), _sig("m", b, 0.7)], market_neutral=True)
    assert flat.weights == {a.key: 0.0, b.key: 0.0}


def test_all_abstains_is_a_flat_book_not_a_crash():
    t = _token()
    out = blend_signals([Signal.abstain("a", t, NOW, "x"), Signal.abstain("b", t, NOW, "y")])
    assert out.convictions == {t.key: 0.0} and out.weights == {t.key: 0.0}


# ---- risk ----

def test_per_position_cap_then_gross_scale_each_leave_a_clamp_event():
    limits = RiskLimits(max_position_pct=0.25, max_gross_exposure=1.0)
    out = apply_limits({"a": 0.6, "b": -0.5, "c": 0.2, "d": 0.2, "e": 0.2}, limits)
    events = {(e.limit, e.subject) for e in out.clamps}
    assert ("max_position_pct", "a") in events and ("max_position_pct", "b") in events
    assert ("max_gross_exposure", None) in events                                # 0.25+0.25+0.6 = 1.1 > 1.0
    assert sum(abs(w) for w in out.weights.values()) == pytest.approx(1.0)
    assert all(abs(w) <= 0.25 + 1e-12 for w in out.weights.values())            # scaling never re-violates the cap
    assert out.weights["b"] < 0                                                 # sign preserved


def test_limits_are_idempotent_and_freed_exposure_is_not_redistributed():
    limits = RiskLimits(max_position_pct=0.3, max_gross_exposure=1.0)
    once = apply_limits({"a": 0.9, "b": 0.1}, limits)
    twice = apply_limits(once.weights, limits)
    assert twice.weights == once.weights and twice.clamps == []
    assert once.weights == {"a": 0.3, "b": 0.1}                                  # b did not grow to fill the gap


def test_charter_percent_becomes_a_fraction_and_no_cap_means_no_limits():
    assert limits_from_charter({"max_position_pct": 20}) == RiskLimits(max_position_pct=0.2, max_gross_exposure=1.0)
    assert limits_from_charter({"max_trade_usd": 500}) is None
    assert limits_from_charter({"max_position_pct": "abc"}) is None
    assert limits_from_charter(None) is None


def test_clamp_event_and_skip_serialise_for_a_receipt():
    assert ClampEvent(limit="max_position_pct", subject="x", before=0.5, after=0.25).model_dump()["after"] == 0.25
    assert SubjectSkip(subject="Token unlocks", reason="not tracked").model_dump() == {"subject": "Token unlocks", "reason": "not tracked"}

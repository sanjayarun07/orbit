import asyncio

import litellm
import pytest

from app.nodes import runtime


class _Sentinel:
    """Stands in for a dspy.LM so we can tell primary vs fallback apart."""

    def __init__(self, name):
        self.name = name


def _drive(program=lambda **k: "ok", **kwargs):
    return asyncio.run(runtime._call_lm(program, **kwargs))


def _stub_run(monkeypatch, behaviour):
    """Replace _run_program with a fn that dispatches on which LM it was given."""
    def fake_run(program, lm, kwargs):
        return behaviour(lm)
    monkeypatch.setattr(runtime, "_run_program", fake_run)


def test_primary_success_never_touches_fallback(monkeypatch):
    monkeypatch.setattr(runtime, "_primary_lm", _Sentinel("primary"))
    monkeypatch.setattr(runtime, "_fallback_lm", _Sentinel("fallback"))
    _stub_run(monkeypatch, lambda lm: f"answer from {lm.name}")
    assert _drive() == "answer from primary"


def test_transient_primary_failure_falls_back(monkeypatch):
    primary, fallback = _Sentinel("primary"), _Sentinel("fallback")
    monkeypatch.setattr(runtime, "_primary_lm", primary)
    monkeypatch.setattr(runtime, "_fallback_lm", fallback)

    def behaviour(lm):
        if lm is primary:
            raise litellm.ServiceUnavailableError("down", "openai/gpt-4.1-mini", "openai")
        return f"answer from {lm.name}"

    _stub_run(monkeypatch, behaviour)
    assert _drive() == "answer from fallback"


def test_non_transient_error_does_not_fall_back(monkeypatch):
    primary, fallback = _Sentinel("primary"), _Sentinel("fallback")
    monkeypatch.setattr(runtime, "_primary_lm", primary)
    monkeypatch.setattr(runtime, "_fallback_lm", fallback)

    class _BadRequest(Exception):
        status_code = 400

    def behaviour(lm):
        if lm is primary:
            raise _BadRequest("malformed")
        raise AssertionError("fallback must not run for a non-transient error")

    _stub_run(monkeypatch, behaviour)
    with pytest.raises(_BadRequest):
        _drive()


def test_transient_failure_without_fallback_raises(monkeypatch):
    monkeypatch.setattr(runtime, "_primary_lm", _Sentinel("primary"))
    monkeypatch.setattr(runtime, "_fallback_lm", None)

    def behaviour(lm):
        raise litellm.Timeout("slow", "openai/gpt-4.1-mini", "openai")

    _stub_run(monkeypatch, behaviour)
    with pytest.raises(litellm.Timeout):
        _drive()


def test_fallback_also_failing_propagates(monkeypatch):
    primary, fallback = _Sentinel("primary"), _Sentinel("fallback")
    monkeypatch.setattr(runtime, "_primary_lm", primary)
    monkeypatch.setattr(runtime, "_fallback_lm", fallback)

    def behaviour(lm):
        raise litellm.RateLimitError("429", "m", "openai")

    _stub_run(monkeypatch, behaviour)
    with pytest.raises(litellm.RateLimitError):
        _drive()


def test_status_code_classifies_transient():
    class _E(Exception):
        status_code = 503
    assert runtime._is_transient_lm_error(_E()) is True

    class _C(Exception):
        status_code = 400
    assert runtime._is_transient_lm_error(_C()) is False


def test_fallback_gets_a_fresh_repeat_guard_scope(monkeypatch):
    # The primary run calls a guarded tool, then dies transiently. The fallback
    # re-runs the whole program and legitimately makes the SAME first call --
    # it must not be rejected as a repeat left over from the aborted primary.
    from app.repeat_guard import guard_tools

    primary, fallback = _Sentinel("primary"), _Sentinel("fallback")
    monkeypatch.setattr(runtime, "_primary_lm", primary)
    monkeypatch.setattr(runtime, "_fallback_lm", fallback)
    (tool,) = guard_tools([lambda symbol: f"price of {symbol}"])

    def behaviour(lm):
        observation = tool(symbol="SOL")
        if lm is primary:
            raise litellm.ServiceUnavailableError("down", "openai/gpt-4.1-mini", "openai")
        return f"{lm.name}: {observation}"

    _stub_run(monkeypatch, behaviour)
    assert _drive() == "fallback: price of SOL"

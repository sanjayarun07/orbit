import inspect

import pytest

from app.metrics import snapshot
from app.repeat_guard import guard_tools, reset_guard, start_guard


def _tool(wallet_address: str, chain: str = "solana") -> str:
    """A stand-in tool with a real signature, mirroring app.agent's shape."""
    return f"{wallet_address}:{chain}"


def test_no_guard_active_passes_through_unaffected():
    """Matches how existing tests import and call tool functions directly --
    e.g. tests/test_agent.py calling sol_balance(...) with no ReAct/_call_lm
    involved at all."""
    (wrapped,) = guard_tools([_tool])
    assert wrapped(wallet_address="abc") == "abc:solana"
    assert wrapped(wallet_address="abc") == "abc:solana"  # no guard scope: repeats are fine


def test_exact_repeat_call_raises_within_an_active_guard_scope():
    (wrapped,) = guard_tools([_tool])
    token = start_guard()
    try:
        assert wrapped(wallet_address="abc", chain="solana") == "abc:solana"
        with pytest.raises(RuntimeError, match="already called _tool"):
            wrapped(wallet_address="abc", chain="solana")
    finally:
        reset_guard(token)


def test_different_arguments_do_not_trigger_the_guard():
    (wrapped,) = guard_tools([_tool])
    token = start_guard()
    try:
        assert wrapped(wallet_address="abc") == "abc:solana"
        assert wrapped(wallet_address="xyz") == "xyz:solana"  # different wallet -- not a repeat
        assert wrapped(wallet_address="abc", chain="base") == "abc:base"  # different chain -- not a repeat
    finally:
        reset_guard(token)


def test_exact_repeat_call_increments_the_repeat_guard_metric():
    (wrapped,) = guard_tools([_tool])
    token = start_guard()
    try:
        wrapped(wallet_address="abc")
        before = snapshot().get("repeat_guard_blocked__tool", 0)
        with pytest.raises(RuntimeError):
            wrapped(wallet_address="abc")
        after = snapshot().get("repeat_guard_blocked__tool", 0)
        assert after == before + 1
    finally:
        reset_guard(token)


def test_guard_scope_is_isolated_per_start_reset_pair():
    """Mirrors how app/nodes/runtime.py's _call_lm gives each DSPy program
    invocation its own fresh scope -- a repeat blocked in one scope must not
    leak into the next."""
    (wrapped,) = guard_tools([_tool])
    token1 = start_guard()
    wrapped(wallet_address="abc")
    reset_guard(token1)

    token2 = start_guard()
    try:
        assert wrapped(wallet_address="abc") == "abc:solana"  # fresh scope, not a repeat
    finally:
        reset_guard(token2)


def test_functools_wraps_preserves_signature_for_dspy_tool_introspection():
    """dspy.adapters.types.tool.Tool.__init__ uses inspect.signature(func) to
    build the argument schema shown to the model -- verified live this
    follows __wrapped__, so a naive *args/**kwargs wrapper would have
    silently broken tool-calling schemas."""
    (wrapped,) = guard_tools([_tool])
    assert inspect.signature(wrapped) == inspect.signature(_tool)
    assert wrapped.__name__ == "_tool"

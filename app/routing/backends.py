"""What answers the routing classification seam, chosen by ROUTING_BACKEND.

- speech_model    the DSPy speech classifier on the intent model (default)
- jev             TypeSafe's Jev decides; if Jev is unreachable the speech
                  model answers instead -- an outage at a vendor is never a
                  dark router
- jev_fallthrough Jev decides when it is confident (its stated confidence at
                  or above the resolver's threshold and not abstaining);
                  otherwise the speech model decides. The measured shape of
                  Jev's calibration: at >=0.9 it was right 100% of 49 times,
                  below that a coin flip, and its misses were under-confidence
                  on terse prompts rather than wrong choices.

Every path records which backend decided in routing.decided, and the resolver
stamps that into routing_decision, so an A/B deployment can compare the two
from the intent_decision log line alone. explicit_action stays enforced in
code by jev_backend whichever backend runs; nothing here can authorize a
swap.
"""
from __future__ import annotations

from app.metrics import increment
from app.routing import jev_backend
from app.routing.decided import decided_by
from app.settings import settings

BACKENDS = ("speech_model", "jev", "jev_fallthrough")


def intent_call_lm():
    """The call_lm the resolver should use this turn."""
    backend = settings.routing_backend
    if backend == "speech_model":
        from app.nodes import runtime
        return runtime._call_intent_lm
    if backend == "jev":
        return _jev
    if backend == "jev_fallthrough":
        return _jev_fallthrough
    raise ValueError(f"ROUTING_BACKEND={backend!r} is not one of {BACKENDS}")


async def _speech_model(program, reason: str, **kwargs):
    from app.nodes import runtime
    decided_by.set(f"speech_model:{reason}")
    return await runtime._call_intent_lm(program, **kwargs)


async def _jev(program, **kwargs):
    try:
        result = await jev_backend.call_lm(program, **kwargs)
    except Exception:
        increment("routing_jev_unavailable")
        return await _speech_model(program, "jev_unavailable", **kwargs)
    increment("routing_jev_decisions")
    decided_by.set("jev")
    return result


async def _jev_fallthrough(program, **kwargs):
    try:
        result = await jev_backend.call_lm(program, **kwargs)
    except Exception:
        increment("routing_jev_unavailable")
        return await _speech_model(program, "jev_unavailable", **kwargs)
    understanding = result.understanding
    confident = understanding["speech_act"] != "abstain" and float(understanding["confidence"]) >= settings.intent_model_confidence_threshold
    if confident:
        increment("routing_jev_decisions")
        decided_by.set("jev")
        return result
    increment("routing_jev_fallthrough")
    return await _speech_model(program, "jev_uncertain", **kwargs)

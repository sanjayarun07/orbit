"""One routing entry point: controls, canonical context, anchored rules, model.

Precedence, in order:

1. Controls and session continuations (confirm/cancel, quick actions, a
   parameter fragment for an active trade) -- deterministic, never a model.
2. Anchored rules -- a rule that fired on a HARD signal (an execution command
   with its fields, an address or URL, "my" ownership, a control verb, an
   equity ticker) keeps deciding by itself: it is precise, free, and the
   execution paths must stay deterministic.
3. The speech model decides everything else -- every keyword-topical request
   ("explain TVL", "what's my exposure to SOL", "should I buy X?") -- with the
   rule that fired, if any, kept only as a capability/chain hint when it
   agrees on the intent. A rule can no longer misread the *act* of a
   question; the model can never grant execution beyond what the rules and
   the explicit-action check already allow.
4. Fallbacks: an uncertain model defers to a strong rule or asks; an
   unavailable model falls back to the embedding tier, then asks.

Legacy conversation text is never replayed to recover execution intent.
"""
import asyncio
import json
import logging
import threading
from collections import OrderedDict

from app.settings import settings
from . import lexicon as lx
from .contracts import CapabilityRoute
from .controls import is_trade_cancellation, is_trade_confirmation, is_trade_modifier
from .entities import extract_chains
from .intent_router import route_capabilities, plan_execution_route, default_capabilities
from .instruments import equity_instruments
from .model import speech_classifier
from .semantic import SpeechUnderstanding, embedding_router
from .speech import has_competing_speech, is_parameter_fragment
from .trade_parser import extract_cross_chain_draft

# Rule reasons anchored by a hard signal rather than topic keywords. Execution
# intents (trade / cross_chain_swap, any reason except semantic_required) are
# anchored by construction and handled separately in _is_anchored.
_ANCHORED_REASONS = frozenset({
    "conceptual", "trade_cancel", "trade_confirm", "risk_charter", "team_mode", "equity",
    "trade_simulation", "own_token_balance", "own_token_holdings", "own_wallet_activity",
    "wallet_health", "portfolio_scenario", "own_wallet", "token_address", "wallet_address", "url",
})

_understanding_cache: "OrderedDict[str, SpeechUnderstanding]" = OrderedDict()
_cache_lock = threading.Lock()


def route_fields(route: CapabilityRoute, source=None) -> dict:
    return {
        "intent": route.intent, "capabilities": list(route.capabilities),
        "chains": list(route.chains), "execution_provider": route.execution_provider,
        "missing_fields": list(route.missing_fields), "route_source": source or route.source,
    }


def _speech_route(understanding: SpeechUnderstanding, method: str, chains: list[str] | None = None) -> dict:
    chains = list(chains or [])
    if understanding.speech_act == "abstain":
        return _clarify_route(method)
    if understanding.domain == "equity":
        return {"intent": "research", "capabilities": ["equity_research"], "chains": [], "route_source": method}
    if understanding.speech_act == "portfolio":
        return {"intent": "portfolio", "capabilities": ["portfolio", "wallet_intelligence"], "chains": chains, "route_source": method}
    if understanding.speech_act == "explain":
        return {"intent": "general", "capabilities": [], "chains": [], "route_source": method}
    if understanding.speech_act == "advice":
        # Advice is answered as research, never as a quote path, and is tagged
        # in routing_decision so the caller can attach a not-financial-advice
        # disclaimer. finance_data is only requested for crypto/equity subjects.
        caps = ["finance_data", "web_research"] if understanding.domain in {"crypto", "equity"} else ["web_research"]
        return {"intent": "research", "capabilities": caps, "chains": chains, "route_source": method}
    return {"intent": "research", "capabilities": ["web_research"], "chains": chains, "route_source": method}


def _clarify_route(method: str) -> dict:
    return {
        "intent": "general", "capabilities": [], "chains": [], "route_source": method,
        "clarification": (
            "Could you add a bit more detail so I answer the right question — for example the "
            "token, wallet, company, or topic you mean, and whether you want research or a "
            "transaction prepared for review?"
        ),
        "execution_provider": None,
    }


def _is_anchored(candidate: CapabilityRoute | None, request: str) -> bool:
    if candidate is None or candidate.reason == "semantic_required":
        return False
    if candidate.intent in {"trade", "cross_chain_swap"}:
        return True
    if candidate.confidence < 0.8:
        return False
    # An explicit address is a hard entity: "positions for 0x..." is a lookup
    # about that address whichever topical rule named it, and the model's
    # own-holdings-vs-research distinction has nothing to add.
    if lx.ADDRESS.search(request):
        return True
    return candidate.reason in _ANCHORED_REASONS


def _strong_rule(candidate: CapabilityRoute | None) -> bool:
    return candidate is not None and candidate.reason != "semantic_required" and candidate.confidence >= 0.8


def _quote_route(understanding: SpeechUnderstanding, request: str, method: str) -> dict:
    # The competition signals ("if", "when", "?", "should"...) that already defer
    # a lexical execution candidate also veto a model-proposed quote: a
    # conditional or questioning phrasing is never turned into a quote now.
    if understanding.explicit_action and understanding.domain == "crypto" and not has_competing_speech(request):
        route = plan_execution_route(request, extract_chains(request))
        return {"intent": route.intent, "capabilities": list(route.capabilities), "chains": list(route.chains),
                "execution_provider": route.execution_provider, "missing_fields": list(route.missing_fields),
                "route_source": method}
    return _clarify_route(method)


async def _classify_with_model(request: str, call_lm) -> SpeechUnderstanding:
    key = " ".join(request.lower().split())
    with _cache_lock:
        hit = _understanding_cache.get(key)
        if hit is not None:
            _understanding_cache.move_to_end(key)
            return hit
    result = await call_lm(speech_classifier, request=request)
    understanding = SpeechUnderstanding.model_validate(result.understanding)
    with _cache_lock:
        _understanding_cache[key] = understanding
        while len(_understanding_cache) > max(1, settings.intent_classifier_cache_entries):
            _understanding_cache.popitem(last=False)
    return understanding


async def _embedding_fallback(request: str, embedding_factory) -> tuple[dict, dict]:
    """The pre-model tier, used only when the model is unavailable."""
    match = await asyncio.to_thread(embedding_factory(settings.intent_embedding_model).classify, request)
    meta = {"method": match.method, "confidence": match.score, "similarity": match.score, "margin": match.margin,
            "reason": match.reason, "embedding_model": settings.intent_embedding_model}
    understanding = match.understanding
    if understanding is None:
        return _clarify_route(match.method), meta
    meta.update(speech_act=understanding.speech_act, domain=understanding.domain, explicit_action=understanding.explicit_action)
    if understanding.speech_act == "quote":
        return _quote_route(understanding, request, match.method), meta
    return _speech_route(understanding, match.method), meta


async def _model_first(request: str, candidate: CapabilityRoute | None, call_lm, embedding_factory, source) -> tuple[dict, dict]:
    hint = candidate.reason if candidate else None
    meta: dict = {"method": "speech_model", "rules_hint": hint, "confidence": 0.0, "reason": None}
    try:
        understanding = await _classify_with_model(request, call_lm)
    except Exception:
        if _strong_rule(candidate):
            return route_fields(candidate, source), {**meta, "method": "rules", "reason": f"model_unavailable:{hint}"}
        update, emeta = await _embedding_fallback(request, embedding_factory)
        return update, {**meta, **emeta, "rules_hint": hint, "model_unavailable": True}
    meta.update(confidence=understanding.confidence, speech_act=understanding.speech_act,
                domain=understanding.domain, explicit_action=understanding.explicit_action)
    uncertain = understanding.speech_act == "abstain" or understanding.confidence < settings.intent_model_confidence_threshold
    if uncertain:
        # An uncertain model never decides. A strong rule may; the embedding
        # tier is not consulted as a second fuzzy opinion.
        if _strong_rule(candidate):
            return route_fields(candidate, source), {**meta, "method": "rules", "reason": f"model_uncertain:{hint}"}
        return _clarify_route("speech_model"), {**meta, "reason": "model_uncertain"}
    if understanding.speech_act == "quote":
        return _quote_route(understanding, request, "speech_model"), meta
    chains = list(candidate.chains) if candidate is not None else list(extract_chains(request))
    update = _speech_route(understanding, "speech_model", chains)
    if candidate is not None and candidate.reason != "semantic_required" and candidate.intent == update["intent"]:
        # Same act, same intent: the rule's capabilities are the richer,
        # deterministic hint (e.g. defi_data for a TVL question).
        update = route_fields(candidate, source or "rules+model")
        meta["reason"] = f"agrees:{hint}"
    elif candidate is not None:
        meta["reason"] = f"overrides:{hint}"
    return update, meta


async def resolve(state: dict, call_lm, embedding_factory=embedding_router) -> dict:
    """Return a route decision; the model caller is injected for budget/testing."""
    request = state["request"]
    contextual = state.get("contextual_request") or request
    controlled = is_trade_cancellation(request) or is_trade_confirmation(request)
    candidate = route_capabilities(request if controlled else contextual)
    active = (state.get("session_context") or {}).get("active_workflow") or {}
    action = state.get("quick_action") or {}
    modifier = is_trade_modifier(request) and not has_competing_speech(request)
    continuation = modifier or (active.get("status") == "collecting_details" and is_parameter_fragment(request))
    metadata = {"method": "rules", "confidence": candidate.confidence if candidate else 0.0,
                "reason": candidate.reason if candidate else None}
    entity_source = "session_entity" if contextual != request else None
    if controlled:
        update = route_fields(candidate)
    elif action.get("intent") in {"research", "portfolio", "general"}:
        update = {"intent": action["intent"], "capabilities": list(action.get("capabilities") or default_capabilities(action["intent"])),
                  "chains": [action["chain"]] if action.get("chain") else [], "route_source": "quick_action"}
    elif equity_instruments(request) and candidate and candidate.intent in {"trade", "cross_chain_swap"}:
        update = {"intent": "research", "capabilities": ["equity_research"], "chains": [], "route_source": "equity_registry"}
    elif continuation and (candidate is None or modifier):
        if active.get("intent") in {"trade", "cross_chain_swap"} and active.get("status") in {"collecting_details", "pending_approval"}:
            partial = extract_cross_chain_draft(request, extract_chains(request))
            chains = tuple(dict.fromkeys(filter(None, (
                partial.get("source_chain") or active.get("source_chain"),
                partial.get("destination_chain") or active.get("destination_chain"),
            ))))
            update = route_fields(plan_execution_route("swap", chains), "session_context")
        else:
            update = {"intent": "general", "capabilities": [], "chains": [], "route_source": "session_context",
                      "clarification": "There is no active trade to modify. Please describe the swap you want to prepare."}
    elif _is_anchored(candidate, contextual):
        update = route_fields(candidate, entity_source)
    else:
        update, metadata = await _model_first(contextual, candidate, call_lm, embedding_factory, entity_source)

    # Team mode ("trading desk"): route content requests through the multi-agent
    # Coordinator, which reuses the underlying single-agent nodes. Stash the
    # original intent as team_subintent and keep capabilities/chains/
    # execution_provider so the Coordinator knows trade vs analysis. Control and
    # general messages resolve to non-content intents and are never redirected;
    # explicit quick actions keep their typed route.
    team_mode = bool((state.get("session_context") or {}).get("team_mode"))
    intent0 = update.get("intent")
    caps0 = update.get("capabilities") or []
    # Content intents the desk handles: a real swap (trade), asset research
    # (research), and a hypothetical of the user's own position (portfolio/
    # trade_simulation) -- the latter two are analysis to the desk.
    is_content = intent0 in {"trade", "research"} or (intent0 == "portfolio" and "trade_simulation" in caps0)
    if team_mode and is_content and update.get("route_source") != "quick_action":
        update["team_subintent"] = "trade" if intent0 == "trade" else "analysis"
        update["intent"] = "team"

    metadata["method"] = update.get("route_source", metadata["method"])
    metadata["intent"] = update["intent"]
    logging.getLogger("orbit.routing").info("intent_decision %s", json.dumps(metadata, sort_keys=True))
    update["routing_decision"] = metadata
    return update

"""One routing entry point: controls, canonical context, rules, semantics.

Legacy conversation text is never replayed to recover execution intent.
"""
import asyncio
import json
import logging

from app.settings import settings
from .contracts import CapabilityRoute
from .controls import is_trade_cancellation, is_trade_confirmation, is_trade_modifier
from .entities import extract_chains
from .intent_router import route_capabilities, plan_execution_route, default_capabilities
from .instruments import equity_instruments
from .model import speech_classifier
from .semantic import SpeechUnderstanding, embedding_router
from .speech import has_competing_speech, is_parameter_fragment
from .trade_parser import extract_cross_chain_draft


def route_fields(route: CapabilityRoute, source=None) -> dict:
    return {
        "intent": route.intent, "capabilities": list(route.capabilities),
        "chains": list(route.chains), "execution_provider": route.execution_provider,
        "missing_fields": list(route.missing_fields), "route_source": source or route.source,
    }


def _speech_route(understanding: SpeechUnderstanding, method: str) -> dict:
    if understanding.speech_act == "abstain":
        return _clarify_route(method)
    if understanding.domain == "equity":
        return {"intent": "research", "capabilities": ["equity_research"], "chains": [], "route_source": method}
    if understanding.speech_act == "portfolio":
        return {"intent": "portfolio", "capabilities": ["portfolio", "wallet_intelligence"], "chains": [], "route_source": method}
    if understanding.speech_act == "explain":
        return {"intent": "general", "capabilities": [], "chains": [], "route_source": method}
    if understanding.speech_act == "advice":
        # Advice is answered as research, never as a quote path, and is tagged
        # in routing_decision so the caller can attach a not-financial-advice
        # disclaimer. finance_data is only requested for crypto/equity subjects.
        caps = ["finance_data", "web_research"] if understanding.domain in {"crypto", "equity"} else ["web_research"]
        return {"intent": "research", "capabilities": caps, "chains": [], "route_source": method}
    return {"intent": "research", "capabilities": ["web_research"], "chains": [], "route_source": method}


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
    elif candidate is not None and candidate.reason != "semantic_required":
        update = route_fields(candidate, "session_entity" if contextual != request else None)
    else:
        match = await asyncio.to_thread(embedding_factory(settings.intent_embedding_model).classify, request)
        understanding = match.understanding
        method = match.method
        metadata = {"method": method, "confidence": match.score, "similarity": match.score, "margin": match.margin,
                    "reason": match.reason, "embedding_model": settings.intent_embedding_model}
        if understanding is None:
            # A low-margin conflict is deliberately not promoted by another
            # fuzzy classifier. The residual model is for unavailable/unmatched
            # embeddings, not for overriding a contradictory classification.
            if match.reason == "low_margin":
                update = _clarify_route(method)
            else:
                try:
                    result = await call_lm(speech_classifier, request=request)
                    understanding = SpeechUnderstanding.model_validate(result.understanding)
                    method = "speech_model"
                    metadata.update(method=method, confidence=understanding.confidence)
                except Exception:
                    update = _clarify_route("semantic_unavailable")
        if understanding is not None:
            # Embeddings use the similarity/margin gates in rank(); model
            # self-reported confidence is separately bounded and never consent.
            sufficient = method != "speech_model" or understanding.confidence >= settings.intent_model_confidence_threshold
            if not sufficient:
                update = _clarify_route(method)
            elif understanding.speech_act == "quote":
                if understanding.explicit_action and understanding.domain == "crypto":
                    route = plan_execution_route(request, extract_chains(request))
                    update = {"intent": route.intent, "capabilities": list(route.capabilities), "chains": list(route.chains),
                              "execution_provider": route.execution_provider, "missing_fields": list(route.missing_fields), "route_source": method}
                else:
                    update = _clarify_route(method)
            else:
                update = _speech_route(understanding, method)
            metadata.update(speech_act=understanding.speech_act, domain=understanding.domain,
                            explicit_action=understanding.explicit_action)

    metadata["method"] = update.get("route_source", metadata["method"])
    metadata["intent"] = update["intent"]
    logging.getLogger("orbit.routing").info("intent_decision %s", json.dumps(metadata, sort_keys=True))
    update["routing_decision"] = metadata
    return update

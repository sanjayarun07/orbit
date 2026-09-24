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
import re
import threading
from collections import OrderedDict

from app.settings import settings
from . import lexicon as lx
from .contracts import CapabilityRoute
from .decided import decided_by
from .controls import is_trade_cancellation, is_trade_confirmation, is_trade_modifier
from .entities import extract_chains
from .intent_router import route_capabilities, plan_execution_route, default_capabilities
from .instruments import equity_instruments
from .model import speech_classifier
from .semantic import SpeechUnderstanding, embedding_router
from app import deployment, product_actions, snapshot_compare
from .speech import CONDITIONAL_ORDER_ANSWER, has_competing_speech, is_conditional_order, is_parameter_fragment
from .trade_parser import extract_cross_chain_draft
from app.clarify import is_clarification, is_market_text

from . import subject_probe

# Rule reasons anchored by a hard signal rather than topic keywords. Execution
# intents (trade / cross_chain_swap, any reason except semantic_required) are
# anchored by construction and handled separately in _is_anchored.
_ANCHORED_REASONS = frozenset({
    "knowledge_base", "news_explainer", "conceptual", "trade_cancel", "trade_confirm", "risk_charter", "team_mode", "equity",
    "trade_simulation", "own_token_balance", "own_token_holdings", "own_wallet_activity",
    "wallet_health", "portfolio_scenario", "token_address", "wallet_address", "url", "perp_positions",
})
# "own_wallet" (bare "my wallet" + anything) is deliberately NOT anchored: "my
# wallet policy?" is a policy question, "analyze my wallet" is a portfolio one;
# the model tells them apart and the rule survives as the capability hint.

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
    if understanding.speech_act in {"explain", "policy", "app"}:
        return {"intent": "general", "capabilities": [], "chains": [], "route_source": method}
    if understanding.speech_act == "advice":
        # Advice is answered as research, never as a quote path, and is tagged
        # in routing_decision so the caller can attach a not-financial-advice
        # disclaimer. finance_data is only requested for crypto/equity subjects.
        # A crypto advice ask with no single asset ("what should I day-trade")
        # is answered from movers, volume, sentiment and news; those tools
        # must be in reach, not only the web-search ones.
        caps = (["finance_data", "web_research", "market_data", "market_sentiment", "token_discovery", "news"] if understanding.domain == "crypto"
                else ["finance_data", "web_research"] if understanding.domain == "equity" else ["web_research"])
        return {"intent": "research", "capabilities": caps, "chains": chains, "route_source": method}
    return {"intent": "research", "capabilities": ["web_research"], "chains": chains, "route_source": method}


# An asset named in an advice-shaped message: $TICKER, an all-caps ticker,
# an address, or the word after buy/sell/long/short/into/on/about.
_ADVICE_ASSET = re.compile(
    r"\$[A-Za-z][A-Za-z0-9]{1,9}\b|(?-i:\b[A-Z][A-Z0-9]{1,9}\b)"
    r"|\b(?:buy|sell|long|short|ape\s+into|into|hold|on|about|in)\s+(?:some\s+|more\s+|the\s+)?([a-z][a-z0-9]{1,9})\b",
    re.I,
)
_ADVICE_STOP = {"the", "it", "this", "that", "crypto", "market", "markets", "now", "today", "here", "general", "stocks", "btc", "eth", "sol",
                "me", "you", "my", "your", "what", "which", "them", "these", "those", "something", "anything", "everything", "all", "tokens", "coins"}


def _desk_wanted(update: dict, metadata: dict, request: str) -> bool:
    """Whether the desk should take this turn on its own: an advice-shaped
    crypto ask (the classifier's "advice") about a named asset, routed as
    research or a trade simulation. Never a security check (the dossier is
    the right answer), never a trade or a quick action, never when off."""
    if not settings.team_desk_auto or update.get("route_source") == "quick_action":
        return False
    intent = update.get("intent")
    caps = update.get("capabilities") or []
    if not (intent == "research" or (intent == "portfolio" and "trade_simulation" in caps)):
        return False
    if metadata.get("speech_act") != "advice" or metadata.get("domain") not in (None, "crypto", "general"):
        return False
    if "token_security" in caps or lx.SECURITY.search(request or "") or "equity_research" in caps:
        return False
    if re.search(r"\b(?:compare|comparison|vs\.?|versus)\b", request or "", re.I):
        return False          # two subjects: the single path asks or compares; the desk has one asset
    for match in _ADVICE_ASSET.finditer(request or ""):
        word = (match.group(1) or match.group(0)).lstrip("$").lower()
        if word not in _ADVICE_STOP:
            return True
    return bool(lx.ADDRESS.search(request or ""))


RESEARCH_MODE_ANSWER = (
    "This deployment runs in research mode: no swap, bridge or order is prepared or signed here, so there is no quote to review. "
    "What works: `what would happen if I sold 0.05 SOL for USDC` (a read-only Jupiter quote of the outcome), `exit analysis for X` "
    "(what your position would fetch at 25/50/100%), `compare buying $500, $2,000 and $5,000 of X` (entry and reverse-exit quotes), "
    "and price alerts. Trading opens when the deployment is switched to execution mode."
)


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
    if understanding.speech_act == "app" and not product_actions.matches(request):
        # No product answer exists for it: the model's "app" is a misread of a
        # word like "saved" or "report"; the ask is research.
        understanding = understanding.model_copy(update={"speech_act": "research"})
    meta.update(confidence=understanding.confidence, speech_act=understanding.speech_act,
                domain=understanding.domain, explicit_action=understanding.explicit_action)
    uncertain = understanding.speech_act == "abstain" or understanding.confidence < settings.intent_model_confidence_threshold
    if uncertain:
        # An uncertain model never decides. A strong rule may; the embedding
        # tier is not consulted as a second fuzzy opinion. Failing both, the
        # subject of the message is looked up on the web first (user rule,
        # 2026-09-18: "if we are not sure what the query is, start with a web
        # search to get the context, then follow") and the answer routes it.
        if _strong_rule(candidate):
            return route_fields(candidate, source), {**meta, "method": "rules", "reason": f"model_uncertain:{hint}"}
        # Two look-ups at once: what the subject is (a strict-JSON probe,
        # which decides the route) and what the web answers to the question
        # itself (kept as the first evidence card, so the tools' cards are
        # read together with it -- "search first, synthesize, then the
        # tools", user rule 2026-09-18). With no settled subject the web's
        # answer still stands on its own before we ask the user anything.
        found, context = await asyncio.gather(asyncio.to_thread(subject_probe.probe, request),
                                              asyncio.to_thread(subject_probe.context_search, request))
        # The web asking "which unlock do you mean?" is not context; it is the
        # same missing subject, and it ends the turn as a question to the user.
        web_asks = bool(context) and is_clarification(context)
        if web_asks:
            context = None
        probed = subject_probe.route_from(found, request) if found else None
        if probed:
            # One subject per turn: the web's answer rides along only when it
            # is about the thing the probe identified (TRUMP: the probe said
            # the Solana memecoin, the web described the politician -- the
            # token route stands, the web card is dropped).
            agreed = subject_probe.agrees(found, context)
            if context and agreed:
                probed["web_context"] = context
            return probed, {**meta, "method": "subject_probe", "reason": f"probe:{found.get('kind')}", "subject": found.get("subject"),
                            "probe_confidence": found.get("confidence"),
                            "web_context": True if (context and agreed) else ("dropped:off_subject" if context else False)}
        if context and is_market_text(context):
            return ({"intent": "research", "capabilities": ["web_research"], "chains": [], "route_source": "subject_probe", "web_context": context},
                    {**meta, "method": "web_context", "reason": "model_uncertain:web_context" + (f":probe_{found.get('kind')}" if found else "")})
        # Nothing settled it (or the web read it as something outside markets:
        # Mercury the planet). Ask, and say what the web made of it.
        reason = "model_uncertain" + (f":probe_{found.get('kind')}" if found else "") + (":web_asks" if web_asks else ":off_market" if context else "")
        return {**_clarify_route("speech_model"), "clarification": subject_probe.clarify_text(found, request)}, {**meta, "reason": reason}
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


def _bare_referent(request: str, state: dict) -> bool:
    """A pronoun question with nothing in the conversation for it to point at."""
    from app.routing.subject_probe import _REFERENT, has_own_subject
    context = state.get("session_context") or {}
    if not _REFERENT.match(request or "") or has_own_subject(request or ""):
        return False
    return not (context.get("focus") or context.get("last_contract") or context.get("pending_token") or (state.get("history") or "").strip())


async def resolve(state: dict, call_lm, embedding_factory=embedding_router) -> dict:
    """Return a route decision; the model caller is injected for budget/testing."""
    request = state["request"]
    contextual = state.get("contextual_request") or request
    controlled = is_trade_cancellation(request) or is_trade_confirmation(request)
    if not controlled and product_actions.is_product_question(request):
        # "What can I do here without connecting a wallet": about this product,
        # answered deterministically; the classifier read it as explain and the
        # web described marketplaces (funded UI run, 2026-09-23).
        return {"intent": "general", "capabilities": [], "chains": [], "route_source": "rules",
                "routing_decision": {"method": "rules", "reason": "product_question", "speech_act": "app"}}
    if not controlled and _bare_referent(request, state):
        # "What did it do today?" in a fresh chat: "it" points at nothing this
        # conversation holds, so the answer is a question, never a web read
        # of whatever "it" the search returns (Iran sanctions, expanded UI
        # review 2026-09-24).
        return {**_clarify_route("rules"), "clarification": "Which token, protocol or topic do you mean by that? Name it and I'll look.",
                "routing_decision": {"method": "rules", "reason": "bare_referent"}}
    if not controlled and snapshot_compare.dated_ask(request):
        # A comparison between dates is research on the snapshot ledger,
        # whatever else the sentence says ("using saved snapshots" read as an
        # app action; "September" read as an asset, live run 2026-09-23).
        return {"intent": "research", "capabilities": ["token_discovery", "market_data", "token_security"], "chains": list(extract_chains(request)),
                "route_source": "rules", "routing_decision": {"method": "rules", "reason": "dated_comparison", "speech_act": "research"}}
    if not controlled and is_conditional_order(request):
        # "If SOL drops below $100, automatically buy 2 SOL": there is no
        # such order here, and no rule or model should turn it into one.
        return {**_clarify_route("rules"), "clarification": CONDITIONAL_ORDER_ANSWER, "routing_decision": {"method": "rules", "reason": "conditional_order"}}
    candidate = route_capabilities(request if controlled else contextual)
    active = (state.get("session_context") or {}).get("active_workflow") or {}
    action = state.get("quick_action") or {}
    modifier = is_trade_modifier(request) and not has_competing_speech(request)
    # A parameter fragment (".01sol", "50bps", "on base") continues the active
    # trade whether it is still collecting details or already has a quote
    # awaiting approval: amending a quote is the everyday case.
    continuation = modifier or (active.get("status") in {"collecting_details", "pending_approval"} and is_parameter_fragment(request))
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
    elif _desk_wanted(update, metadata, contextual):
        # The desk on its own (user, 2026-09-18: "make it auto in the backend
        # whenever required"): an opinion about an asset gets Market Research,
        # a conviction and a risk read; a fact, a security check or a swap
        # does not.
        update["team_subintent"] = "analysis"
        update["intent"] = "team"
        metadata["team_auto"] = True

    # A portfolio ask that names an address is a read-only look at THAT
    # wallet: the address becomes the turn's wallet when none is connected,
    # so the portfolio node can run instead of asking to connect one. A
    # connected wallet is never replaced by a pasted address.
    if update.get("intent") == "portfolio" and not state.get("wallet_address"):
        pasted = lx.ADDRESS.search(state.get("request") or "")
        if pasted:
            update["wallet_address"] = pasted.group(0)
            metadata["wallet_source"] = "pasted_address"

    metadata["method"] = update.get("route_source", metadata["method"])
    metadata["intent"] = update["intent"]
    # Provenance for an A/B of routing backends: which backend is configured
    # and which one actually decided this turn (none: cache or rules).
    metadata["routing_backend"] = settings.routing_backend
    metadata["decided_by"] = decided_by.get()
    logging.getLogger("orbit.routing").info("intent_decision %s", json.dumps(metadata, sort_keys=True))
    if (update.get("clarification") and not deployment.execution_enabled()
            and (lx.TRADE.search(request) or lx.IMPERATIVE_MOVE.search(request))
            and not re.search(r"\?|\b(?:should|would|could|can|shall|do|does)\s+i\b|\bwhat\s+(?:if|would)\b|\bexplain\b|\bhow\s+(?:do|does|to|much)\b|\bsimulat\w*\b", request, re.I)):
        # A trade-shaped ask the router could not place, in a research
        # deployment: the answer is the deployment's, not "which token did you
        # mean" (live 2026-09-23: "Start a cross-chain swap"). Routed trades
        # keep their route; the plan path refuses them with the same words.
        update["clarification"] = RESEARCH_MODE_ANSWER
        metadata["reason"] = "research_mode"
    update["routing_decision"] = metadata
    return update

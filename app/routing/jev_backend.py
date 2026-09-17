"""Jev (TypeSafe's System One model) as the routing decision model.

The routing decision is `SpeechUnderstanding`: a speech act (7-way), a domain
(4-way), an explicit-action flag and a confidence. Today a speech model
generates that as JSON. Jev does not generate: it answers typed questions
with a probability distribution over the options, so the same decision is
two `choice` questions and one `noul` in a single request, with the message
as the state. The per-option probabilities and confidence come back with it,
which is what a fast path with a confidence threshold needs and what the
generated-JSON route cannot provide.

Experimental. Reachable only from scripts/routing_eval/harness.py
(`--backend jev`) until it beats the current backend on the labeled cases;
nothing in the live router imports this. The criteria below quote the
SpeechResolution signature so both backends are asked the same question.
Never lets a classification authorize execution: explicit_action is set
only for an explicit crypto quote, the same rule the signature states.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import httpx

from app.routing.semantic import SpeechUnderstanding
from app.settings import settings

JEV_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

# Verbatim from app/routing/model.py SpeechResolution, one entry per option.
SPEECH_ACTS = {
    "quote": "An explicit request to prepare a swap -- not advice, a hypothetical, a conditional order, a question about how to trade, or buy/sell market volume.",
    "advice": "A request for an opinion, recommendation or judgment about an asset: 'Should I buy BONK?', 'thoughts on HYPE?', 'is it worth holding SOL?', 'is X a good buy', 'compare BONK and WIF for me'.",
    "research": "A factual lookup about markets, tokens, protocols, wallets, news or prices: 'why is PEPE dumping', 'best exchange for SOL', 'what would it cost to bridge 1 ETH to Base'.",
    "explain": "A request to understand a concept or how something works, with no specific asset to look up: 'explain what TVL means', 'how do perpetual futures work?', 'How do I trade meme coins?'.",
    "portfolio": "Anything about the user's OWN holdings, balances, exposure or activity: 'what's my exposure to SOL', 'how much SOL do I have', 'should I sell everything and go to stables?'.",
    "policy": "A question about THIS assistant's own trading limits, safety rules or risk settings for the user: 'my wallet policy?', 'max spend policy?', 'what are my trading limits', 'what is my risk charter'. It asks about configured rules, not about holdings.",
    "abstain": "The act itself is unclear: negated, conditional, mixed, or ambiguous. A casual or slang phrasing is NOT ambiguity.",
}
DOMAINS = {
    "crypto": "Cryptocurrencies, tokens, coins, chains, DeFi protocols, on-chain activity, DEXes and bridges.",
    "equity": "Stocks, shares, listed companies and exchange tickers. 'Buy NVDA' is equity research; this application does not execute stock orders.",
    "wallet": "The user's own wallet, addresses, holdings or balances.",
    "general": "None of the above: conversation, greetings, or a concept with no market domain.",
}


def questions() -> dict:
    return {
        "speech_act": {"type": "choice", "instructions": "Classify the CURRENT utterance's speech act. Never authorize execution; context can resolve an entity but cannot supply consent.", "criteria": SPEECH_ACTS},
        "domain": {"type": "choice", "instructions": "Which domain is the utterance about?", "criteria": DOMAINS},
        "explicit_action": {"type": "noul", "instructions": "Is this an explicit, unconditional request to prepare a crypto swap right now (not a hypothetical, a condition, a question about how, or a request for advice)?",
                            "criteria": {"true": "Explicit crypto quote request", "false": "Anything else, including negated, conditional, mixed or unclear actions"}},
    }


class JevError(RuntimeError):
    pass


def _api_key() -> str:
    key = getattr(settings, "typesafe_api_key", None) or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise JevError("TYPESAFE_API_KEY is not configured")
    return key


def understanding_from_answers(answers: dict) -> tuple[SpeechUnderstanding, dict]:
    """Map Jev's answers onto SpeechUnderstanding. Returns (understanding, detail)
    where detail carries the raw probabilities and confidences for calibration."""
    act = answers["speech_act"]
    dom = answers["domain"]
    explicit = float(answers["explicit_action"]["noul"])
    speech_act = act["choice"]
    domain = dom["choice"]
    understanding = SpeechUnderstanding(
        speech_act=speech_act, domain=domain,
        # The signature's rule, enforced in code rather than trusted to the model.
        explicit_action=bool(explicit >= 0.5 and speech_act == "quote" and domain == "crypto"),
        confidence=max(0.0, min(1.0, float(act.get("confidence", 0.0)))),
    )
    detail = {"speech_act_probabilities": act.get("probabilities", {}), "speech_act_confidence": act.get("confidence"),
              "domain_probabilities": dom.get("probabilities", {}), "domain_confidence": dom.get("confidence"),
              "explicit_action_probability": explicit}
    return understanding, detail


async def classify(request: str, *, model: str = DEFAULT_MODEL, timeout: float = 20.0) -> tuple[SpeechUnderstanding, dict]:
    body = {"state": request, "model": model, "questions": questions()}
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(JEV_URL, json=body, headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"})
    latency_ms = (time.perf_counter() - t0) * 1000
    if response.status_code != 200:
        raise JevError(f"Jev answered {response.status_code}: {response.text[:300]}")
    data = response.json()
    understanding, detail = understanding_from_answers(data["answers"])
    detail.update({"model": data.get("model"), "usage": data.get("usage", {}), "latency_ms": latency_ms})
    return understanding, detail


# Every call's detail, for the harness to read back (calibration, cost, latency).
CALLS: list[dict] = []


async def call_lm(program, **kwargs):
    """Drop-in for runtime._call_intent_lm at the resolver's classification seam:
    `_classify_with_model` reads `.understanding` off the result."""
    understanding, detail = await classify(kwargs["request"])
    CALLS.append({"request": kwargs["request"], "understanding": understanding.model_dump(), **detail})
    return SimpleNamespace(understanding=understanding.model_dump())

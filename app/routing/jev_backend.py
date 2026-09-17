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

# The SpeechResolution signature's definitions, one entry per option, kept to
# a line each: the first round sent ~900 tokens of criteria per call and
# measured Jev putting 0.4-0.7 of its mass on `abstain` for terse prompts
# ("hot metas right now", "honeypot check") -- the long abstain text read as
# "anything short is unclear". Abstain is now defined narrowly and the
# instructions say what to do with a fragment: classify it.
SPEECH_ACTS = {
    "quote": "An explicit, unconditional request to prepare a swap now ('swap 1 SOL to USDC'). Not advice, a hypothetical, a condition, or a how-to question.",
    "advice": "Asks for an opinion or recommendation about an asset: 'Should I buy BONK?', 'thoughts on HYPE?', 'is X a good buy'.",
    "research": "A factual lookup: markets, tokens, protocols, wallets, news, prices, on-chain data. 'why is PEPE dumping', 'best exchange for SOL', 'hot metas right now', 'honeypot check 0x...'.",
    "explain": "Asks how a concept works, naming no specific asset to look up: 'explain what TVL means', 'how do perpetual futures work?'.",
    "portfolio": "About the user's OWN holdings, balances, exposure or activity: 'how much SOL do I have', 'what's my exposure to SOL'.",
    "policy": "About THIS assistant's own trading limits or risk settings: 'what are my trading limits', 'what is my risk charter'.",
    "abstain": "ONLY when the act itself cannot be determined: an action that is negated ('don't buy'), conditional ('buy if it dips'), or two acts mixed. A terse, slang or fragmentary request is NOT abstain -- it has an act; classify it.",
}
DOMAINS = {
    "crypto": "Cryptocurrencies, tokens, chains, DeFi protocols, DEXes, bridges, on-chain data. The default for any coin, token, mint or contract address.",
    "equity": "Stocks, shares, listed companies, stock tickers, earnings. Only when the subject is a company's stock, not a token.",
    "wallet": "The user's own wallet, addresses, holdings or balances.",
    "general": "No market subject at all: greetings, chit-chat, or a concept with no asset or market in it.",
}


def questions() -> dict:
    return {
        "speech_act": {"type": "choice", "instructions": "Classify the speech act of this message. Most messages are short and casual; that is normal, not ambiguous -- pick the act they most plausibly perform. Never authorize execution.", "criteria": SPEECH_ACTS},
        "domain": {"type": "choice", "instructions": "Which domain is the message about? A token, coin, mint or contract address is crypto; a company's stock is equity.", "criteria": DOMAINS},
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

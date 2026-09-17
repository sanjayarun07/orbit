"""The Jev routing backend, against a stubbed API: the request it sends and the
SpeechUnderstanding it maps back. No network; the live comparison is the
routing eval harness (`--mode resolve --backend jev`)."""
import asyncio

import httpx
import pytest

from app.routing import jev_backend
from app.routing.model import SpeechResolution
from app.routing.semantic import SpeechUnderstanding


def _answers(act="research", act_conf=0.91, domain="crypto", explicit=0.03, act_probs=None):
    return {
        "speech_act": {"type": "choice", "choice": act, "probabilities": act_probs or {act: act_conf}, "confidence": act_conf},
        "domain": {"type": "choice", "choice": domain, "probabilities": {domain: 0.97}, "confidence": 0.97},
        "explicit_action": {"type": "noul", "noul": explicit},
    }


def test_the_questions_offer_exactly_the_signatures_options():
    q = jev_backend.questions()
    assert set(q["speech_act"]["criteria"]) == set(SpeechUnderstanding.model_fields["speech_act"].annotation.__args__)
    assert set(q["domain"]["criteria"]) == set(SpeechUnderstanding.model_fields["domain"].annotation.__args__)
    # Every option Jev is offered is one the signature defines, and each
    # criterion carries the signature's own example for it, so both backends
    # are asked the same question.
    doc = " ".join(SpeechResolution.__doc__.split())
    examples = {"advice": "Should I buy BONK?", "research": "why is PEPE dumping", "explain": "explain what TVL means",
                "portfolio": "how much SOL do I have", "policy": "what are my trading limits"}
    for option, text in jev_backend.SPEECH_ACTS.items():
        assert option in doc
        if option in examples:
            assert examples[option] in text and examples[option] in doc, option


def test_answers_map_to_the_decision_the_resolver_consumes():
    understanding, detail = jev_backend.understanding_from_answers(_answers())
    assert understanding == SpeechUnderstanding(speech_act="research", domain="crypto", explicit_action=False, confidence=0.91)
    assert detail["explicit_action_probability"] == 0.03 and detail["speech_act_probabilities"] == {"research": 0.91}


@pytest.mark.parametrize("act,domain,explicit,expected", [
    ("quote", "crypto", 0.95, True),
    ("quote", "crypto", 0.4, False),     # the model was not sure it was explicit
    ("quote", "equity", 0.95, False),    # 'Buy NVDA' never authorizes anything
    ("advice", "crypto", 0.95, False),   # only a quote may carry the flag
])
def test_explicit_action_is_only_ever_an_explicit_crypto_quote(act, domain, explicit, expected):
    understanding, _ = jev_backend.understanding_from_answers(_answers(act=act, domain=domain, explicit=explicit))
    assert understanding.explicit_action is expected


def test_the_request_carries_the_message_as_state_and_the_key_as_bearer(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(jev_backend.settings, "typesafe_api_key", None, raising=False)
    sent = {}

    def handler(request: httpx.Request):
        sent["url"] = str(request.url)
        sent["auth"] = request.headers.get("authorization")
        sent["body"] = request.read()
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": _answers(), "usage": {"input_tokens": 300, "output_tokens": 0}})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    jev_backend.CALLS.clear()
    result = asyncio.run(jev_backend.call_lm(None, request="why is PEPE dumping"))
    import json
    body = json.loads(sent["body"])
    assert sent["url"] == jev_backend.JEV_URL and sent["auth"] == "Bearer test-key"
    assert body["state"] == "why is PEPE dumping" and body["model"] == "jev-latest" and set(body["questions"]) == {"speech_act", "domain", "explicit_action"}
    assert result.understanding["speech_act"] == "research"
    assert jev_backend.CALLS[0]["usage"] == {"input_tokens": 300, "output_tokens": 0} and jev_backend.CALLS[0]["latency_ms"] >= 0


def test_a_non_200_answer_is_an_error_not_a_decision(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(jev_backend.settings, "typesafe_api_key", None, raising=False)
    transport = httpx.MockTransport(lambda request: httpx.Response(429, json={"detail": "slow down"}))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    with pytest.raises(jev_backend.JevError):
        asyncio.run(jev_backend.classify("hi"))


def test_a_missing_key_is_refused_before_any_request(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(jev_backend.settings, "typesafe_api_key", None, raising=False)
    with pytest.raises(jev_backend.JevError):
        asyncio.run(jev_backend.classify("hi"))

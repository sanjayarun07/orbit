import json

import pytest
from fastapi.testclient import TestClient

from app import main, x402_gate
from app.graph import AgentRun
from app.settings import settings

PAY_TO = "0x000000000000000000000000000000000000dEaD"


class _FakeFacilitator:
    """Answers the facilitator protocol without any network: supported kinds
    for Base Sepolia 'exact', and (unused here) verify/settle."""

    def __init__(self):
        from x402.http import FacilitatorConfig, HTTPFacilitatorClient
        self._impl = HTTPFacilitatorClient(FacilitatorConfig(url="http://fake.invalid"))

    def __getattr__(self, name):
        return getattr(self._impl, name)

    def get_supported(self):
        from x402.schemas.responses import SupportedKind, SupportedResponse
        return SupportedResponse(kinds=[SupportedKind(x402Version=2, scheme="exact", network="eip155:84532")],
                                 extensions=[], signers={"eip155": ["0x0000000000000000000000000000000000000001"]})

    async def verify(self, payload, requirements):
        from x402.schemas.responses import VerifyResponse
        return VerifyResponse(is_valid=True, payer=PAY_TO)

    async def settle(self, payload, requirements):
        from x402.schemas.responses import SettleResponse
        return SettleResponse(success=True, transaction="0xabc", network="eip155:84532", payer=PAY_TO)


@pytest.fixture
def paid_chat(monkeypatch):
    async def fake_run(message, wallet, history, session_context, action):
        return AgentRun(answer="ok", trajectory=None, trade_plan=None, intent="general", capabilities=[])

    monkeypatch.setattr(main, "run_agent", fake_run)
    monkeypatch.setattr(settings, "x402_enabled", True)
    monkeypatch.setattr(settings, "x402_pay_to", PAY_TO)
    monkeypatch.setattr(settings, "x402_network", "eip155:84532")
    monkeypatch.setattr(settings, "x402_price", "$0.01")
    x402_gate.set_facilitator(_FakeFacilitator())
    yield TestClient(main.app)
    x402_gate.reset()


def test_disabled_gate_is_a_passthrough(monkeypatch):
    async def fake_run(message, wallet, history, session_context, action):
        return AgentRun(answer="free", trajectory=None, trade_plan=None, intent="general", capabilities=[])

    monkeypatch.setattr(main, "run_agent", fake_run)
    monkeypatch.setattr(settings, "x402_enabled", False)
    x402_gate.reset()
    response = TestClient(main.app).post("/chat", json={"message": "hello"})
    assert response.status_code == 200 and response.json()["answer"] == "free"
    assert TestClient(main.app).get("/config/public").json()["x402"]["enabled"] is False


def test_unpaid_chat_gets_402_with_the_payment_requirements(paid_chat):
    response = paid_chat.post("/chat", json={"message": "hello"}, headers={"accept": "application/json"})
    assert response.status_code == 402, response.text
    # x402 v2 carries the requirements in a base64 PAYMENT-REQUIRED header.
    import base64
    required = json.loads(base64.b64decode(response.headers["payment-required"]))
    assert required["x402Version"] == 2
    accept = required["accepts"][0]
    assert accept["scheme"] == "exact" and accept["network"] == "eip155:84532"
    assert accept["payTo"].lower() == PAY_TO.lower()
    assert int(accept["amount"]) == 10_000  # $0.01 of USDC (6 decimals)
    # Everything else stays free: health and history are not behind the paywall.
    assert paid_chat.get("/health").status_code == 200


def test_public_config_advertises_the_price_and_network(paid_chat):
    cfg = paid_chat.get("/config/public").json()["x402"]
    assert cfg == {"enabled": True, "network": "eip155:84532", "price": "$0.01", "pay_to": PAY_TO,
                   "facilitator_url": settings.x402_facilitator_url}


def test_enabled_without_pay_to_fails_loudly(monkeypatch):
    monkeypatch.setattr(settings, "x402_enabled", True)
    monkeypatch.setattr(settings, "x402_pay_to", None)
    x402_gate.reset()
    with pytest.raises(RuntimeError):
        x402_gate._build()
    x402_gate.reset()

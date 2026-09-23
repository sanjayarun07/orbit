"""Authenticated HTTP → exit controls → jobs → inbox/export, with simulated chain data.

Only external balances/quotes and email transport are replaced. No real wallet,
provider, account or Redis data is used.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import emailer, exit_controls, exit_monitor, jobs, main
from tests.conftest import sign_in

WALLET = "7GK7tZ1yD1mS3sJt7dcqJ2k5aZm7gJ8yq1uZ6R9WWEFm"
MINT = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


@pytest.fixture
def market(monkeypatch):
    state = {"quantity": 100_000_000, "price": 1.0, "impact": 1.0, "failure": None}

    async def balance(wallet, mint):
        if state["failure"] == "balance":
            raise exit_monitor.BalanceUnavailable("simulated RPC outage")
        if not state["quantity"]:
            return None
        return {"mint": mint, "quantity_raw": state["quantity"], "decimals": 6, "quantity": state["quantity"] / 1e6}

    async def quote(_in, _out, amount):
        if state["failure"] in ("timeout", "no_route"):
            raise RuntimeError("Could not find any route" if state["failure"] == "no_route" else "request timed out")
        quantity = amount / 1e6
        proceeds = quantity * state["price"] * (1 - state["impact"] / 100)
        return {"input_token": SimpleNamespace(symbol="ANSEM", usd_price=state["price"], decimals=6),
                "input_amount": quantity, "input_value_usd": quantity * state["price"], "output_amount": proceeds,
                "price_impact_pct": state["impact"], "quote": {"otherAmountThreshold": str(int(proceeds * .995 * 1e6)),
                "slippageBps": 50, "contextSlot": 123, "routePlan": [{"swapInfo": {"label": "Simulated pool"}}]}}

    async def resolve(_token):
        return MINT, "ANSEM", None

    monkeypatch.setattr(exit_monitor, "position_of", balance)
    monkeypatch.setattr(exit_monitor, "simulate_swap", quote)
    monkeypatch.setattr(exit_controls, "resolve_token", resolve)
    return state


def chat(client, prompt, sid=None):
    response = client.post("/chat/stream", json={"message": prompt, "wallet_address": WALLET, "session_id": sid})
    assert response.status_code == 200, response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    done = [event["data"] for event in events if "data" in event and "answer" in event["data"]]
    assert len(done) == 1, response.text
    return done[0]


def tick(position):
    async def run():
        row = await jobs.get(position["job_id"])
        result = await jobs.run(row)
        assert result["status"] == "scheduled", result
    asyncio.run(run())


def test_monitor_stream_history_alert_export_and_stop(market, monkeypatch):
    client = TestClient(main.app)
    user = sign_in(client)["user"]
    first = chat(client, "watch my exit on ANSEM")
    sid = first["session_id"]
    assert "Watching your ANSEM exit" in first["answer"]
    assert not first.get("suggestions")
    repeat = chat(client, "watch my exit on ANSEM", sid)
    assert "Already watching" in repeat["answer"]
    positions = asyncio.run(exit_monitor.list_for(user["id"]))
    assert len(positions) == 1
    position = positions[0]
    threshold = chat(client, "Tell me when the discount on my full-position exit quote for ANSEM exceeds 5%.", sid)
    assert "Set:" in threshold["answer"]
    market["impact"] = 7.0
    tick(position)
    inbox = client.get("/me/inbox").json()["items"]
    assert len(inbox) == 1 and "discount 7.0%" in inbox[0]["title"]
    tick(position)
    assert len(client.get("/me/inbox").json()["items"]) == 1  # cooldown
    history = client.get(f"/chat/history/{sid}").json()["messages"]
    assert all(isinstance(message.get("ts"), (int, float)) for message in history)
    exported = client.get("/me/export").json()
    assert len(exported["exit_positions"]) == 1
    assert exported["exit_positions"][0]["rules"]["discount_pct"] == 5
    assert len(exported["exit_positions"][0]["history"]) >= 3
    outsider = TestClient(main.app)
    sign_in(outsider, "other@example.com")
    assert outsider.get(f"/chat/history/{sid}").status_code in (403, 404)
    assert outsider.get("/me/export").json()["exit_positions"] == []
    assert "Stopped watching" in chat(client, "stop watching my exit on ANSEM", sid)["answer"]
    assert "not watching any exits" in chat(client, "show my exits", sid)["answer"]
    assert asyncio.run(jobs.get(position["job_id"]))["status"] == "cancelled"


def test_failures_full_exit_and_email_retry_through_monitor(market, monkeypatch):
    client = TestClient(main.app)
    user = sign_in(client)["user"]
    first = chat(client, "watch my exit on ANSEM")
    sid = first["session_id"]
    position = asyncio.run(exit_monitor.list_for(user["id"]))[0]
    for failure in ("balance", "timeout"):
        market["failure"] = failure
        tick(position)
        assert client.get("/me/inbox").json()["items"] == []
    market["failure"] = "no_route"
    tick(position)
    assert "no route" in client.get("/me/inbox").json()["items"][0]["title"]
    market.update(failure=None, quantity=0)
    assert "no longer holds the token" in chat(client, "What changed since I entered ANSEM?", sid)["answer"]
    market.update(quantity=100_000_000, price=.6)
    now = datetime.now(timezone.utc) + timedelta(hours=7)
    monkeypatch.setattr(exit_monitor, "_now", lambda: now)
    assert "by email" in chat(client, "email me when my ANSEM exit drops 10%", sid)["answer"]
    attempts = []

    async def send(*args, **kwargs):
        attempts.append(args)
        return len(attempts) > 1

    monkeypatch.setattr(emailer, "send_email", send)
    tick(position)
    assert asyncio.run(exit_monitor.get(position["id"]))["last_alert"]["email_pending"]
    tick(position)
    assert len(attempts) == 2
    assert not asyncio.run(exit_monitor.get(position["id"]))["last_alert"]["email_pending"]
    assert len(client.get("/me/inbox").json()["items"]) == 2

"""Per-user tasks: scheduling math, natural-language creation from chat,
alert evaluation, brief delivery (inbox + email + credit), the worker tick,
plan caps, and the REST surface."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import execution_policy
from app import accounts, credits, emailer, home_highlights, main, tasks, tasks_nl
from app.billing_plans import FREE
from app.settings import settings
from tests.conftest import sign_in

IST = 330  # minutes east of UTC


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    async def fake_send(to, subject, html, text=None):
        if subject.startswith("Sign in to"):
            return False
        sent.append({"to": to, "subject": subject, "text": text})
        return True

    monkeypatch.setattr(emailer, "send_email", fake_send)
    return sent


@pytest.fixture
def prices(monkeypatch):
    table = {"SOL": 100.0, "BTC": 60000.0}

    async def fake_price(symbol):
        return table.get(symbol.upper().lstrip("$"))

    monkeypatch.setattr(tasks, "price_for", fake_price)
    return table


def test_next_run_respects_local_time_and_weekdays():
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)  # Tuesday
    daily = tasks.next_run({"daily": "08:00"}, IST, after=now)
    assert daily == datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)  # 08:00 IST tomorrow (today's 08:00 IST already passed)
    assert tasks.next_run({"daily": "20:00"}, IST, after=now) == datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc)
    friday = tasks.next_run({"weekly": {"day": 4, "time": "09:00"}}, 0, after=now)
    assert friday == datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
    assert tasks.next_run({"at": "2020-01-01T00:00:00+00:00"}, 0, after=now) is None
    assert tasks.next_run({"every_minutes": 5}, 0, after=now) == now + timedelta(minutes=5)
    assert tasks.describe_schedule({"weekly": {"day": 4, "time": "09:00"}}) == "every Fri at 09:00"


def test_reminder_phrases_parse():
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    sched, msg = tasks_nl.parse_reminder("tomorrow at 9am to check SOL", IST, now)
    assert msg == "check SOL" and sched["at"].startswith("2026-09-16T03:30")
    sched, msg = tasks_nl.parse_reminder("in 2 hours to rebalance", 0, now)
    assert msg == "rebalance" and sched["at"].startswith("2026-09-15T14:00")
    sched, msg = tasks_nl.parse_reminder("every Monday at 9:30am to review my portfolio", 0, now)
    assert sched == {"weekly": {"day": 0, "time": "09:30"}} and msg == "review my portfolio"
    sched, msg = tasks_nl.parse_reminder("every day at 8am to read the brief", 0, now)
    assert sched == {"daily": "08:00"}
    sched, msg = tasks_nl.parse_reminder("to call the exchange at 5pm", 0, now)
    assert msg == "call the exchange" and sched["at"].startswith("2026-09-15T17:00")
    sched, msg = tasks_nl.parse_reminder("on friday to check unlocks", 0, now)
    assert sched["at"].startswith("2026-09-18T09:00")
    assert tasks_nl.parse_reminder("about stuff", 0, now) is None
    assert tasks_nl.is_task_control("alert me when SOL drops below $90")
    assert not tasks_nl.is_task_control("what is a liquidity pool?")


def test_chat_creates_lists_and_manages_tasks(prices, monkeypatch):
    async def fake_run(*args, **kwargs):
        raise AssertionError("task controls must not reach the graph")

    monkeypatch.setattr(execution_policy, "run_agent", fake_run)
    client = TestClient(main.app)
    sign_in(client, email="tasks@example.com")
    r = client.post("/chat", json={"message": "remind me tomorrow at 9am to check SOL", "tz_offset_min": IST}).json()
    assert r["answer"].startswith("Reminder set — **check SOL**") and r["credits"]["charged"] == settings.credit_cost_chat_turn
    r = client.post("/chat", json={"message": "alert me when SOL drops below $90", "tz_offset_min": IST}).json()
    assert "Alert set: **SOL < $90** (now $100)" in r["answer"]
    r = client.post("/chat", json={"message": "send me a morning brief at 8am by email", "tz_offset_min": IST}).json()
    assert "daily at 08:00" in r["answer"] and "email + inbox" in r["answer"]
    listing = client.post("/chat", json={"message": "show my tasks"}).json()["answer"]
    assert "1. **Reminder: check SOL**" in listing and "3. **Morning brief**" in listing
    assert "paused" in client.post("/chat", json={"message": "pause task 2"}).json()["answer"]
    assert client.get("/me/tasks").json()["tasks"][1]["status"] == "paused"
    assert "Deleted task 1" in client.post("/chat", json={"message": "delete task 1"}).json()["answer"]
    assert len(client.get("/me/tasks").json()["tasks"]) == 2
    # Unknown price -> no alert.
    assert "couldn't find a trustworthy price" in client.post("/chat", json={"message": "alert me when XYZQ goes above 5"}).json()["answer"]
    # Anonymous users fall through to the normal turn (which here is the fake agent).
    anon = TestClient(main.app).post("/chat", json={"message": "show my tasks"})
    assert anon.status_code == 500  # AssertionError from fake_run -> the graph was reached


def test_price_alert_fires_once_and_reschedules_while_waiting(prices, outbox):
    client = TestClient(main.app)
    me = sign_in(client, email="alert@example.com")
    user = asyncio.run(accounts.get_user(me["user"]["id"]))
    task = asyncio.run(tasks.create_task(user, "price_alert", {"symbol": "SOL", "op": "<", "price": 90}, {"every_minutes": 5}, "email", 0))
    # Not due yet.
    assert asyncio.run(tasks.tick()) == []
    future = datetime.now(timezone.utc) + timedelta(minutes=6)
    result = asyncio.run(tasks.tick(future))
    assert result[0]["fired"] is False and result[0]["status"] == "active"  # 100 > 90: keep watching
    prices["SOL"] = 85.0
    result = asyncio.run(tasks.tick(future + timedelta(minutes=6)))
    assert result[0]["fired"] is True and result[0]["status"] == "done"
    inbox = client.get("/me/inbox").json()
    assert inbox["unread"] == 1 and "crossed your alert" in inbox["items"][0]["body"]
    assert outbox[-1]["to"] == "alert@example.com" and "SOL < $90" in outbox[-1]["subject"]
    assert client.post("/me/inbox/read", json={}).json() == {"marked": 1, "unread": 0}
    assert asyncio.run(tasks.tick(future + timedelta(days=1))) == []  # done tasks never run again


def test_brief_costs_a_credit_and_includes_headlines(monkeypatch, outbox):
    monkeypatch.setattr(home_highlights, "get_highlights", lambda force=False: {"as_of": "x", "source": "news", "cards": [
        {"kind": "crypto", "title": "Bitcoin tops $120k", "summary": "ETF inflows hit a record."},
        {"kind": "stocks", "title": "Nvidia beats", "summary": "Revenue $46B."}]})
    monkeypatch.setattr(tasks.market_overview, "_get_json", lambda url: {"bitcoin": {"usd": 120000, "usd_24h_change": 2.0}, "ethereum": {"usd": 3000, "usd_24h_change": -1.0}, "solana": {"usd": 150, "usd_24h_change": 0.5}})
    client = TestClient(main.app)
    me = sign_in(client, email="brief@example.com")
    user = asyncio.run(accounts.get_user(me["user"]["id"]))
    task = asyncio.run(tasks.create_task(user, "brief", {}, {"daily": "08:00"}, "email", 0))
    before = client.get("/me").json()["credits"]["balance"]
    run = client.post(f"/me/tasks/{task['id']}/run").json()
    assert run["fired"] is True and run["status"] == "active"  # daily: reschedules
    body = client.get("/me/inbox").json()["items"][0]["body"]
    assert "Bitcoin tops $120k" in body and "BTC $120,000 (+2.0%)" in body
    assert client.get("/me").json()["credits"]["balance"] == before - settings.credit_cost_brief
    assert outbox[-1]["subject"].endswith("Morning brief")
    nxt = client.get("/me/tasks").json()["tasks"][0]["next_run_at"]
    assert nxt and nxt > datetime.now(timezone.utc).isoformat()


def test_plan_caps_active_tasks_and_rest_surface():
    client = TestClient(main.app)
    sign_in(client, email="cap@example.com")
    for i in range(tasks.TASK_LIMITS["free"]):
        assert client.post("/me/tasks", json={"kind": "reminder", "spec": {"message": f"r{i}"}, "schedule": {"every_minutes": 60}}).status_code == 201
    over = client.post("/me/tasks", json={"kind": "reminder", "spec": {"message": "one more"}, "schedule": {"every_minutes": 60}})
    assert over.status_code == 400 and "Free plan allows 3" in over.json()["detail"]
    listing = client.get("/me/tasks").json()
    assert listing["limit"] == 3 and len(listing["tasks"]) == 3 and listing["tasks"][0]["schedule_text"] == "every 60 min"
    first = listing["tasks"][0]["id"]
    assert client.patch(f"/me/tasks/{first}", json={"status": "paused"}).json()["status"] == "paused"
    assert client.post("/me/tasks", json={"kind": "reminder", "spec": {"message": "fits now"}, "schedule": {"every_minutes": 60}}).status_code == 201
    assert client.delete(f"/me/tasks/{first}").json() == {"deleted": True}
    assert client.delete(f"/me/tasks/{first}").status_code == 404
    assert client.post("/me/tasks", json={"kind": "reminder", "spec": {"message": "past"}, "schedule": {"at": "2020-01-01T00:00:00Z"}}).status_code == 400
    assert TestClient(main.app).get("/me/tasks").status_code == 401

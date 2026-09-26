"""Phase 0 of docs/agentic-wallets-plan.md: the armed exit rule.

The exit watch quotes the sized exit when its alert fires, checks it, writes
the receipt first, and hands the user the exact sell line; nothing is
prepared or signed by the rule itself."""
from __future__ import annotations

import asyncio

from app import agent_rules, exit_controls, exit_monitor, jobs, tasks
from app.settings import settings
from tests.test_exit_monitor import MINT, WALLET, _sim, chain  # noqa: F401  (the chain fixture)

USER = {"id": "u1"}


WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"


async def _search(self, query):
    return [{"id": MINT, "symbol": "BONK", "name": "Bonk", "tags": ["verified"]}, {"id": WIF, "symbol": "WIF", "name": "dogwifhat", "tags": ["verified"]}]


def _watched(monkeypatch) -> dict:
    # Patch the class, not the instance: restoring an instance attribute leaves a
    # bound method on the singleton that shadows later class-level patches (the
    # Jupiter batch test failed whenever this file ran before it).
    monkeypatch.setattr(type(exit_controls.jupiter), "search_tokens", _search)
    monkeypatch.setattr(settings, "exit_alert_drop_pct", 20.0)
    monkeypatch.setattr(settings, "exit_alert_cooldown_hours", 6.0)
    reply = asyncio.run(exit_controls.handle("watch my exit on BONK", USER, WALLET))
    assert reply.startswith("Watching your BONK exit")
    return asyncio.run(exit_monitor.list_for("u1"))[0]


def _fire(monkeypatch, position: dict, price: float = 0.000014, impact: float = 1.0) -> None:
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(price, impact))            # about -34% on the full exit, inside the impact cap
    asyncio.run(jobs.run(asyncio.run(jobs.get(position["job_id"]))))


def test_arming_sets_the_rule_and_says_what_it_does(chain, monkeypatch):
    p = _watched(monkeypatch)
    reply = asyncio.run(exit_controls.handle("arm my BONK exit: sell half if it drops 20%", USER, WALLET))
    assert reply.startswith("Armed: when a full exit of BONK is quoted 20% lower than the baseline, a 50% exit is quoted at that moment")
    assert "never sells on its own" in reply and "disarm my BONK exit" in reply
    rules = asyncio.run(exit_monitor.get(p["id"]))["rules"]
    assert rules["arm_fraction"] == 0.5 and rules["drop_pct"] == 20.0 and not rules.get("arm_paper")
    listing = asyncio.run(exit_controls.handle("show my exits", USER, WALLET))
    assert "armed: 50% exit ready to confirm, up to 3/day" in listing
    assert asyncio.run(exit_controls.handle("arm my WIF exit: sell half if it drops 20%", USER, WALLET)).startswith("You are not watching an exit on")


def test_the_rule_quotes_checks_receipts_and_hands_over_the_sell_line(chain, monkeypatch):
    p = _watched(monkeypatch)
    asyncio.run(exit_controls.handle("arm my BONK exit: sell half if it drops 20%", USER, WALLET))
    _fire(monkeypatch, p)
    inbox = asyncio.run(tasks.inbox("u1"))
    kinds = sorted(i["kind"] for i in inbox)
    assert kinds == ["exit_alert", "exit_armed"]
    armed = next(i for i in inbox if i["kind"] == "exit_armed")
    assert armed["title"] == "BONK: 50% exit ready to confirm"
    receipts = asyncio.run(agent_rules.list_for("u1"))
    assert len(receipts) == 1
    r = receipts[0]
    assert r["status"] == "armed" and r["signer"] == "user" and r["kind"] == "exit_rule" and r["source_id"] == p["id"]
    assert r["trigger"]["kind"] == "deterioration" and r["trigger"]["drop_pct"] > 25
    assert r["proposal"]["amount_raw"] == 500_000_00000 and r["proposal"]["amount"] == "500000"
    assert r["proposal"]["prompt"] == f"sell 500000 {MINT} for USDC on solana with 50 bps slippage"
    assert r["checks"]["ok"] and r["quote"]["ok"] and r["quote"]["fraction"] == 0.5
    assert f"send `{r['proposal']['prompt']}`" in armed["body"] and f"Token pinned by mint {MINT}" in armed["body"]
    assert "Nothing is prepared or signed until you do" in armed["body"] and "Rule: 1 of 3 prepared today." in armed["body"]


def test_a_quote_over_the_built_in_cap_is_refused_and_recorded(chain, monkeypatch):
    p = _watched(monkeypatch)
    asyncio.run(exit_controls.handle("arm my BONK exit: sell all if it drops 20%", USER, WALLET))
    monkeypatch.setattr(settings, "max_trade_usd", 5.0)                                   # the full exit quotes about $13
    _fire(monkeypatch, p)
    r = asyncio.run(agent_rules.list_for("u1"))[0]
    assert r["status"] == "refused" and "built-in max per trade of $5.00" in r["reason"] and r["proposal"]["fraction"] == 1.0
    armed = next(i for i in asyncio.run(tasks.inbox("u1")) if i["kind"] == "exit_armed")
    assert armed["title"] == "BONK exit rule fired, not prepared" and "was not prepared: $" in armed["body"] and "the watch continues" in armed["body"]


def test_the_users_charter_is_checked_before_anyone_is_told(chain, monkeypatch):
    p = _watched(monkeypatch)
    asyncio.run(exit_controls.handle("arm my BONK exit: sell half if it drops 20%", USER, WALLET))

    async def fields(user_id):
        return {"max_trade_usd": 3.0, "verified_only": True}
    monkeypatch.setattr(agent_rules, "_charter_fields", fields)
    _fire(monkeypatch, p)
    r = asyncio.run(agent_rules.list_for("u1"))[0]
    assert r["status"] == "refused" and "your max per trade of $3.00" in r["reason"]
    assert r["checks"]["deferred"] == ["verified_only"]


def test_paper_mode_records_without_a_line_to_send(chain, monkeypatch):
    p = _watched(monkeypatch)
    reply = asyncio.run(exit_controls.handle("paper arm my BONK exit: sell all if it drops 15%", USER, WALLET))
    assert reply.startswith("Paper-armed")
    _fire(monkeypatch, p)
    r = asyncio.run(agent_rules.list_for("u1"))[0]
    assert r["status"] == "paper" and r["signer"] == "paper"
    armed = next(i for i in asyncio.run(tasks.inbox("u1")) if i["kind"] == "exit_armed")
    assert armed["title"] == "BONK: paper exit recorded (100%)" and "Recorded only; nothing to confirm" in armed["body"]
    assert "send `sell" not in armed["body"]


def test_the_daily_cap_refuses_the_next_firing(chain, monkeypatch):
    p = _watched(monkeypatch)
    asyncio.run(exit_controls.handle("arm my BONK exit: sell half if it drops 20%", USER, WALLET))
    asyncio.run(exit_monitor.set_rules(p["id"], arm_max_per_day=1))
    monkeypatch.setattr(settings, "exit_alert_cooldown_hours", 0.0)
    _fire(monkeypatch, p)
    _fire(monkeypatch, p, price=0.000009, impact=1.5)                                    # worse again: a second alert
    receipts = asyncio.run(agent_rules.list_for("u1"))
    assert [r["status"] for r in receipts] == ["refused", "armed"]
    assert receipts[0]["reason"].startswith("daily cap reached: 1 of 1")
    assert asyncio.run(agent_rules.count_today(p["id"])) == 1


def test_disarming_keeps_the_watch_and_the_alerts(chain, monkeypatch):
    p = _watched(monkeypatch)
    asyncio.run(exit_controls.handle("arm my BONK exit: sell half if it drops 20%", USER, WALLET))
    assert asyncio.run(exit_controls.handle("disarm my BONK exit", USER, WALLET)) == "Disarmed BONK: alerts continue, nothing is prepared for you to confirm."
    assert "arm_fraction" not in asyncio.run(exit_monitor.get(p["id"]))["rules"]
    _fire(monkeypatch, p)
    assert [i["kind"] for i in asyncio.run(tasks.inbox("u1"))] == ["exit_alert"]
    assert asyncio.run(agent_rules.list_for("u1")) == []
    assert asyncio.run(exit_controls.handle("disarm my exits", USER, WALLET)).startswith("No armed exit rule to remove.")


def test_an_arming_failure_never_loses_the_alert(chain, monkeypatch):
    p = _watched(monkeypatch)
    asyncio.run(exit_controls.handle("arm my BONK exit: sell half if it drops 20%", USER, WALLET))

    async def boom(row, alert, rows):
        raise RuntimeError("receipts store down")
    monkeypatch.setattr(agent_rules, "arm_exit", boom)
    _fire(monkeypatch, p)
    assert [i["kind"] for i in asyncio.run(tasks.inbox("u1"))] == ["exit_alert"]


def test_receipts_leave_with_the_account(chain, monkeypatch):
    p = _watched(monkeypatch)
    asyncio.run(exit_controls.handle("arm my BONK exit: sell half if it drops 20%", USER, WALLET))
    _fire(monkeypatch, p)
    assert asyncio.run(agent_rules.scrub_user("u1")) == 1 and asyncio.run(agent_rules.list_for("u1")) == []


def test_the_precheck_is_deterministic_and_never_guesses():
    fields = {"max_trade_usd": 25.0, "max_slippage_bps": 30, "verified_only": True, "max_position_pct": 5}
    ok = agent_rules.charter_precheck(fields, 10.0, 30, 0.5)
    assert ok["ok"] and ok["deferred"] == ["verified_only", "max_position_pct"]
    bad = agent_rules.charter_precheck(fields, 40.0, 50, 0.5)
    assert not bad["ok"] and "$40.00 is over your max per trade of $25.00" in bad["violations"] and "50 bps is over your max slippage of 30 bps" in bad["violations"]
    unknown = agent_rules.charter_precheck(fields, None, 30, None)
    assert not unknown["ok"] and unknown["unresolved"] and not unknown["violations"]

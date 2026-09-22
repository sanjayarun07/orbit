"""Position-specific exit analysis (app/exit_monitor.py, app/exit_controls.py):
the wallet's real balance, exact-size quotes at 25/50/100%, four numbers
never conflated, a scheduled monitor job, and the deterioration alert."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import exit_controls, exit_monitor, jobs, tasks
from app.settings import settings

WALLET = "7GK7tZ1yD1mS3sJt7dcqJ2k5aZm7gJ8yq1uZ6R9WWEFm"
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def _accounts(amount: int, decimals: int = 5):
    return {"value": [{"account": {"data": {"parsed": {"info": {"mint": MINT, "tokenAmount": {"amount": str(amount), "decimals": decimals}}}}}}]}


def _sim(price_per_token: float, impact: float, fee: float = 0.0):
    async def simulate_swap(input_mint, output_mint, amount):
        tokens = amount / 1e5
        out = tokens * price_per_token * (1 - impact / 100) - fee
        return {"input_token": SimpleNamespace(symbol="BONK", usd_price=price_per_token, decimals=5), "output_token": SimpleNamespace(symbol="USDC", decimals=6),
                "input_amount": tokens, "input_value_usd": tokens * price_per_token, "output_amount": out, "output_value_usd": out, "price_impact_pct": impact,
                "quote": {"otherAmountThreshold": str(int(out * 0.995 * 1e6)), "slippageBps": 50, "routePlan": [{"swapInfo": {"label": "Raydium"}}, {"swapInfo": {"label": "Orca"}}]}}
    return simulate_swap


@pytest.fixture
def chain(monkeypatch):
    async def accounts(wallet):
        return _accounts(1_000_000_00000)             # one million BONK
    async def none(wallet):
        return {"value": []}
    monkeypatch.setattr(exit_monitor, "get_token_accounts", accounts)
    monkeypatch.setattr(exit_monitor, "_token_accounts_2022", none)
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.00002, 1.5))
    monkeypatch.setattr(exit_controls, "_resolved", {})            # no symbol resolution carried between tests


def test_the_position_is_the_wallets_real_balance(chain):
    pos = asyncio.run(exit_monitor.position_of(WALLET, MINT))
    assert pos == {"mint": MINT, "quantity_raw": 1_000_000_00000, "decimals": 5, "quantity": 1_000_000.0}


def test_quotes_keep_marked_value_quoted_proceeds_and_minimum_apart(chain):
    pos = asyncio.run(exit_monitor.position_of(WALLET, MINT))
    rows = asyncio.run(exit_monitor.quote_exit(MINT, pos["quantity_raw"]))
    assert [r["fraction"] for r in rows] == [0.25, 0.5, 1.0] and all(r["ok"] for r in rows)
    full = exit_monitor.full_exit(rows)
    assert full["marked_value_usd"] == pytest.approx(20.0) and full["quoted_usdc"] == pytest.approx(19.7) and full["minimum_usdc"] == pytest.approx(19.6015, abs=1e-3)
    assert full["route"] == ["Raydium", "Orca"]
    card = exit_monitor.render_card({"wallet": WALLET, "mint": MINT, "symbol": "BONK", **pos}, rows)
    assert "| 100% | 1,000,000 | $20.00 | **$19.70** | $19.60 | 1.50% | Raydium → Orca |" in card
    assert "**1.5%** is the cost of getting out at this size" in card and "partial exits do not add up" in card
    assert "Nothing here prepares or submits a trade" in card


def test_a_failed_route_is_a_row_that_says_so(monkeypatch, chain):
    async def flaky(input_mint, output_mint, amount):
        if amount > 500_000_00000:
            raise RuntimeError("Could not find any route")
        return await _sim(0.00002, 1.0)(input_mint, output_mint, amount)
    monkeypatch.setattr(exit_monitor, "simulate_swap", flaky)
    rows = asyncio.run(exit_monitor.quote_exit(MINT, 1_000_000_00000))
    assert rows[2] == {"fraction": 1.0, "amount_raw": 1_000_000_00000, "ok": False, "error": "no route"}
    card = exit_monitor.render_card({"wallet": WALLET, "mint": MINT, "symbol": "BONK", "quantity_raw": 1_000_000_00000, "decimals": 5}, rows)
    assert "| 100% | 1,000,000 | — | **no route** |" in card and "A missing quote is not a zero" in card


def test_watching_records_the_entry_and_the_monitor_job_reschedules_itself(chain, monkeypatch):
    monkeypatch.setattr(settings, "exit_monitor_interval_minutes", 15)
    pos = asyncio.run(exit_monitor.position_of(WALLET, MINT))
    rows = asyncio.run(exit_monitor.quote_exit(MINT, pos["quantity_raw"]))
    watched = asyncio.run(exit_monitor.watch("u1", WALLET, MINT, "BONK", pos, rows))
    job = asyncio.run(jobs.get(watched["job_id"]))
    assert job["status"] == "scheduled" and job["kind"] == "exit_monitor"
    due = asyncio.run(jobs.due(datetime.now(timezone.utc) + timedelta(minutes=16)))
    assert [j["id"] for j in due] == [job["id"]]
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.000019, 2.0))            # the market moved
    after = asyncio.run(jobs.run(due[0]))
    assert after["status"] == "scheduled" and after["id"] == job["id"] and after["attempts"] == 0     # one row, rescheduled
    hist = asyncio.run(exit_monitor.history(watched["id"]))
    assert len(hist) == 2 and exit_monitor.full_exit(hist[0]["quotes"])["quoted_usdc"] == pytest.approx(18.62)
    card = exit_monitor.render_card({**watched, **pos}, hist[0]["quotes"], watched["entry"], hist)
    assert "a full exit was quoted at $19.70, now $18.62 (**-5.5%**)" in card and "## Full-exit quote over time" in card


def test_the_deterioration_alert_fires_once_per_cooldown_with_both_quotes(chain, monkeypatch):
    monkeypatch.setattr(settings, "exit_alert_drop_pct", 20.0)
    monkeypatch.setattr(settings, "exit_alert_cooldown_hours", 6.0)
    pos = asyncio.run(exit_monitor.position_of(WALLET, MINT))
    rows = asyncio.run(exit_monitor.quote_exit(MINT, pos["quantity_raw"]))
    watched = asyncio.run(exit_monitor.watch("u1", WALLET, MINT, "BONK", pos, rows))
    job = asyncio.run(jobs.get(watched["job_id"]))
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.000014, 6.0))            # -34% quoted
    asyncio.run(jobs.run(job))
    inbox = asyncio.run(tasks.inbox("u1"))
    assert len(inbox) == 1 and inbox[0]["kind"] == "exit_alert" and "deteriorated 33%" in inbox[0]["title"]
    assert "quoted at $13.16, down 33.2% from $19.70" in inbox[0]["body"] and "not a price alert and not a sell instruction" in inbox[0]["body"]
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.000010, 8.0))            # worse again, inside the cooldown
    asyncio.run(jobs.run(asyncio.run(jobs.get(job["id"]))))
    assert len(asyncio.run(tasks.inbox("u1"))) == 1


def test_stopping_closes_the_position_and_cancels_its_job(chain):
    pos = asyncio.run(exit_monitor.position_of(WALLET, MINT))
    rows = asyncio.run(exit_monitor.quote_exit(MINT, pos["quantity_raw"]))
    watched = asyncio.run(exit_monitor.watch("u1", WALLET, MINT, "BONK", pos, rows))
    assert asyncio.run(exit_monitor.stop(watched["id"], "u2")) is False
    assert asyncio.run(exit_monitor.stop(watched["id"], "u1")) is True
    assert asyncio.run(exit_monitor.get(watched["id"]))["status"] == "closed"
    assert asyncio.run(jobs.get(watched["job_id"]))["status"] == "cancelled"


# ---- the chat controls ----

def test_controls_are_recognised_and_resolve_the_token(chain, monkeypatch):
    for text in ("watch my exit on BONK", "exit analysis for $BONK", "can I exit my BONK position", "how is my exit on BONK", "stop watching my exit on BONK", "show my exits"):
        assert exit_controls.is_exit_control(text), text
    assert not exit_controls.is_exit_control("what would I get if I sold half my SOL")       # the simulation's, not ours

    async def search(query):
        return [{"id": MINT, "symbol": "BONK", "name": "Bonk", "tags": ["verified"]}, {"id": "Fake" + "1" * 40, "symbol": "BONK", "name": "Bonk fake", "tags": []}]
    monkeypatch.setattr(exit_controls.jupiter, "search_tokens", search)
    user = {"id": "u1"}
    reply = asyncio.run(exit_controls.handle("watch my exit on BONK", user, None))
    assert reply.startswith("Connect a Solana wallet first")
    reply = asyncio.run(exit_controls.handle("watch my exit on BONK", user, WALLET))
    assert reply.startswith("Watching your BONK exit") and "| 100% | 1,000,000 | $20.00 | **$19.70** |" in reply
    assert asyncio.run(exit_controls.handle("show my exits", user, WALLET)).startswith("Watched exits:\n1. **BONK**")
    assert asyncio.run(exit_controls.handle("stop watching my exit on BONK", user, WALLET)) == "Stopped watching your BONK exit."


def test_an_ambiguous_symbol_asks_for_the_mint(monkeypatch, chain):
    async def search(query):
        return [{"id": MINT, "symbol": "BONK", "name": "Bonk", "tags": ["verified"]}, {"id": "Bonk2" + "1" * 39, "symbol": "BONK", "name": "Other Bonk", "tags": ["verified"]}]
    monkeypatch.setattr(exit_controls.jupiter, "search_tokens", search)
    reply = asyncio.run(exit_controls.handle("exit analysis for BONK", {"id": "u1"}, WALLET))
    assert reply.startswith("Several tokens use the symbol BONK") and "Paste the mint" in reply

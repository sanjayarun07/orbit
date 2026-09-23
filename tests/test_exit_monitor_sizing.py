"""What changed since entry, said apart; rules the user sets; email delivery;
sizing before entry (product decision, 2026-09-23)."""
import asyncio
from types import SimpleNamespace

import pytest

from app import accounts, emailer, exit_controls, exit_monitor, jobs, tasks
from app.settings import settings

WALLET = "7GK7tZ1yD1mS3sJt7dcqJ2k5aZm7gJ8yq1uZ6R9WWEFm"
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
USDC = exit_monitor.USDC_MINT


def _accounts(amount: int, decimals: int = 5):
    return {"value": [{"account": {"data": {"parsed": {"info": {"mint": MINT, "tokenAmount": {"amount": str(amount), "decimals": decimals}}}}}}]}


def _sim(price: float, impact: float, route=("Raydium",)):
    async def simulate_swap(input_mint, output_mint, amount):
        if input_mint == USDC:                                      # entry: dollars in, tokens out
            usd = amount / 1e6
            tokens = usd / price * (1 - impact / 100)
            return {"input_token": SimpleNamespace(symbol="USDC", usd_price=1.0, decimals=6), "output_token": SimpleNamespace(symbol="BONK", usd_price=price, decimals=5),
                    "input_amount": usd, "input_value_usd": usd, "output_amount": tokens, "output_value_usd": tokens * price, "price_impact_pct": impact,
                    "quote": {"outAmount": str(int(tokens * 1e5)), "contextSlot": 100, "slippageBps": 50, "routePlan": [{"swapInfo": {"label": r}} for r in route]}}
        tokens = amount / 1e5
        out = tokens * price * (1 - impact / 100)
        return {"input_token": SimpleNamespace(symbol="BONK", usd_price=price, decimals=5), "output_token": SimpleNamespace(symbol="USDC", decimals=6),
                "input_amount": tokens, "input_value_usd": tokens * price, "output_amount": out, "output_value_usd": out, "price_impact_pct": impact,
                "quote": {"otherAmountThreshold": str(int(out * 0.995 * 1e6)), "slippageBps": 50, "contextSlot": 101, "routePlan": [{"swapInfo": {"label": r}} for r in route]}}
    return simulate_swap


@pytest.fixture(autouse=True)
def _stores():
    jobs.reset_for_test(); exit_monitor.reset_for_test()
    yield


@pytest.fixture
def chain(monkeypatch):
    async def accounts_(wallet):
        return _accounts(1_000_000_00000)
    async def none(wallet):
        return {"value": []}
    monkeypatch.setattr(exit_monitor, "get_token_accounts", accounts_)
    monkeypatch.setattr(exit_monitor, "_token_accounts_2022", none)
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.00002, 2.0))
    monkeypatch.setattr(exit_controls, "_resolved", {})

    async def search(query):
        return [{"id": MINT, "symbol": "BONK", "name": "Bonk", "tags": ["verified"]}]
    monkeypatch.setattr(exit_controls.jupiter, "search_tokens", search)


def _watch(user="u1"):
    pos = asyncio.run(exit_monitor.position_of(WALLET, MINT))
    rows = asyncio.run(exit_monitor.quote_exit(MINT, pos["quantity_raw"]))
    return asyncio.run(exit_monitor.watch(user, WALLET, MINT, "BONK", pos, rows))


def test_the_change_is_decomposed_into_price_and_discount():
    base = {"quoted_usdc": 100.0, "marked_value_usd": 102.0, "reference_price_usd": 1.0, "price_impact_pct": 1.0, "route": ["Raydium"]}
    now = {"quoted_usdc": 88.0, "marked_value_usd": 98.0, "reference_price_usd": 0.96, "price_impact_pct": 5.0, "route": ["Orca"]}
    c = exit_monitor.explain_change(base, now)
    assert round(c["drop_pct"], 1) == 12.0 and round(c["price_change_pct"], 1) == -4.0
    assert round(c["discount_then_pct"], 1) == 2.0 and round(c["discount_now_pct"], 1) == 10.2 and round(c["discount_widening_pts"], 1) == 8.2
    text = exit_monitor.explain_sentence(c)
    assert text.startswith("Your full-position exit quote fell 12.0%. The reference price fell 4.0%, while the quote's discount to that reference widened from 2.0% to 10.2%.")
    assert "The route changed from Raydium to Orca." in text and text.endswith("Review the latest route and liquidity observations.")


def test_the_alert_explains_the_change_and_a_discount_rule_fires_on_its_own(chain, monkeypatch):
    watched = _watch()
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.000015, 6.0, route=("Meteora",)))      # price -25%, impact 2% -> 6%
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))
    inbox = asyncio.run(tasks.inbox("u1"))
    assert len(inbox) == 1 and inbox[0]["body"].startswith("Your full-position exit quote fell 28.1%. The reference price fell 25.0%, while the quote's discount to that reference widened from 2.0% to 6.0%.")
    assert "The route changed from Raydium to Meteora." in inbox[0]["body"]
    # a user's discount rule, on a fresh position, fires even when proceeds moved little
    tasks.reset()
    exit_monitor.reset_for_test(); jobs.reset_for_test()
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.00002, 2.0))
    watched = _watch()
    asyncio.run(exit_monitor.set_rules(watched["id"], discount_pct=5.0))
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.00002, 7.0))                         # same price, the book thinned
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))
    inbox = asyncio.run(tasks.inbox("u1"))
    assert len(inbox) == 1 and inbox[0]["title"] == "Exit for BONK: discount 7.0%" and "past your 5.0% rule" in inbox[0]["body"]


def test_email_delivery_follows_the_rule_and_only_on_a_new_alert(chain, monkeypatch):
    sent = []

    async def send_email(to, subject, html, text=None):
        sent.append((to, subject))
        return True
    async def get_user(user_id, db=None):
        return {"id": user_id, "email": "holder@example.com"}
    monkeypatch.setattr(emailer, "send_email", send_email)
    monkeypatch.setattr(accounts, "get_user", get_user)
    watched = _watch()
    asyncio.run(exit_monitor.set_rules(watched["id"], channel="email", drop_pct=10.0))
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.000015, 2.0))
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))
    assert sent == [("holder@example.com", f"{settings.product_name}: Exit for BONK deteriorated 25%")]
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))                                 # inside the cooldown: nothing new
    assert len(sent) == 1


def test_thresholds_and_channels_are_set_by_chat(chain):
    user = {"id": "u1"}
    assert asyncio.run(exit_controls.handle("tell me when the discount on my full-position exit quote exceeds 5%", user, WALLET)).startswith("You are not watching any exits yet")
    _watch()
    reply = asyncio.run(exit_controls.handle("tell me when the discount on my full-position exit quote exceeds 5%", user, WALLET))
    assert reply.startswith("Set: you will be told when the discount to the reference price exceeds 5% for BONK.")
    reply = asyncio.run(exit_controls.handle("email me when my BONK exit drops 10%", user, WALLET))
    assert reply.startswith("Set: you will be told when a full exit is quoted 10% lower than the baseline for BONK, by email as well as here.")
    rules = asyncio.run(exit_monitor.list_for("u1"))[0]["rules"]
    assert rules == {"discount_pct": 5.0, "drop_pct": 10.0, "channel": "email"}
    assert asyncio.run(exit_controls.handle("exit alerts to inbox", user, WALLET)) == "Exit alerts for 1 position(s) will stay in the app."
    assert asyncio.run(exit_monitor.list_for("u1"))[0]["rules"]["channel"] == "inapp"


def test_sizing_before_entry_quotes_the_entry_and_the_immediate_reverse_exit(chain):
    rows = asyncio.run(exit_monitor.size_comparison(MINT, (500.0, 2000.0)))
    assert [r["usd"] for r in rows] == [500.0, 2000.0] and all(r["ok"] and r["exit_ok"] for r in rows)
    r = rows[0]
    assert r["tokens"] == pytest.approx(500 / 0.00002 * 0.98) and r["entry_slot"] == 100 and r["exit_slot"] == 101
    assert r["round_trip_cost_pct"] == pytest.approx((1 - 0.98 * 0.98) * 100, abs=0.01)         # 2% in, 2% out
    card = exit_monitor.render_sizes("BONK", MINT, rows)
    assert "| $500.00 | 24,500,000 | $0.00002041 | 2.00% | $480.20 | 4.0% | Raydium / Raydium |" in card
    assert "liquidity diagnostic, not a forecast" in card and "Quotes computed at slot 100 and 101" in card and "Nothing here prepares or submits a trade" in card
    reply = asyncio.run(exit_controls.handle("compare buying $500, $2,000 and $5,000 of BONK", {"id": "u1"}, None))
    assert reply.startswith("# Sizing — BONK") and "| $5,000.00 |" in reply
    assert exit_controls.is_exit_control("what would $250 of WIF cost to enter and exit")


def test_a_rate_limited_quote_is_retried_once_after_a_pause(chain, monkeypatch):
    calls = []
    inner = _sim(0.00002, 2.0)

    async def flaky(input_mint, output_mint, amount):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Jupiter quote failed: 429 Too Many Requests")
        return await inner(input_mint, output_mint, amount)
    monkeypatch.setattr(exit_monitor, "simulate_swap", flaky)
    rows = asyncio.run(exit_monitor.size_comparison(MINT, (500.0,)))
    assert rows[0]["ok"] and rows[0]["exit_ok"] and len(calls) == 3


def test_the_listing_shows_the_rules(chain):
    watched = _watch()
    asyncio.run(exit_monitor.set_rules(watched["id"], drop_pct=10.0, discount_pct=5.0, channel="email"))
    reply = asyncio.run(exit_controls.handle("show my exits", {"id": "u1"}, WALLET))
    assert reply.endswith("· alerts: drop 10%, discount 5%, email")

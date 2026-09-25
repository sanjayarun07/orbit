"""The UI flow report of 2026-09-23: instruction words are not assets, a
declared token is never a deployer wallet, one synthesis per answer, clause
deltas do not interleave, a dated comparison comes from the ledger or says
it cannot, sizing tolerates the chain and trailing sentences, and "what
changed since I entered X" reads the entry baseline."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import accounts, composition, emailer, exit_controls, exit_monitor, holder_snapshots, jobs, mobula_meme, product_actions, snapshot_compare, streaming, tasks
from app.experience import build_context_capsules
from app.nodes import general
from app.routing import subject_probe
from app.routing.resolver import _speech_route
from app.routing.semantic import SpeechUnderstanding

MINT = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
DEPLOYER = "yHCxHBEaJW5tbndqC8JciSThr7U1cqLpdcsvHcx6PRe"


# ---- identity -------------------------------------------------------------

@pytest.mark.parametrize("request_, expected", [
    ("Save this investigation so I can revisit it, and export an evidence-linked report with sources, timestamps and coverage gaps.", None),
    ("What changed since I entered ANSEM? Separate token price movement, changes in my holdings, and worsening exit liquidity.", "ANSEM"),
    ("Show my token watchlist ranked by meaningful changes in liquidity, holder concentration, deployer activity and exit quotes.", None),
    ("Bonk price?", "Bonk"),
    ("ANSEM holders on solana", "ANSEM"),
    ("Audit report on Ansem", "Ansem"),
])
def test_sentence_starters_are_not_subjects(request_, expected):
    assert subject_probe.subject_of(request_) == expected


def test_capsules_never_name_an_instruction_word_as_a_token():
    for request_ in ("Show my token watchlist ranked by changes in liquidity",
                     "What changed since I entered ANSEM? Separate token price movement, changes in my holdings, and worsening exit liquidity."):
        capsules = build_context_capsules(request_, "", "", None, None)
        assert all(c.label.upper() not in ("MY", "SEPARATE") for c in capsules), [c.label for c in capsules]
    capsules = build_context_capsules("is the $WIF token safe", "", "", None, None)
    assert any(c.kind == "token" and c.label.upper() == "WIF" for c in capsules)


def test_the_app_speech_act_routes_to_the_product_answer():
    route = _speech_route(SpeechUnderstanding(speech_act="app", domain="general", explicit_action=False, confidence=0.9), "speech_model")
    assert route["intent"] == "general" and route["capabilities"] == []
    state = {"request": "Save this investigation so I can revisit it, and export an evidence-linked report", "routing_decision": {"speech_act": "app"},
             "session_context": {}, "conversation_history": ""}
    out = asyncio.run(general.general_node(state))
    assert "Every conversation is kept automatically under Recents" in out["answer"] and "no evidence-linked report export yet" in out["answer"]
    assert "Nothing was saved, exported or changed" in out["answer"]
    watch = product_actions.answer("Show my token watchlist ranked by meaningful changes in liquidity, holder concentration and exit quotes")
    assert "no ranked token watchlist yet" in watch and "watch my exit on BONK" in watch
    assert product_actions.answer("open the thing") == product_actions.GENERIC


# ---- a declared token is never a deployer wallet -------------------------

def test_declared_token_resolves_the_deployer_and_never_substitutes_the_mint(monkeypatch):
    calls = []

    def fake_meta(version, path, params, *a, **k):
        calls.append((version, path, params["asset"]))
        return {"deployer": DEPLOYER}                 # mobula_client.get returns the unwrapped data
    monkeypatch.setattr(mobula_meme.mobula_client, "get", fake_meta)
    monkeypatch.setattr(mobula_meme, "_get", lambda path, params: (calls.append((2, path, params["wallet"])) or {"data": []}))
    monkeypatch.setattr(mobula_meme, "_funding_line", lambda wallet, chain: "")
    req = f"Investigate the launch of Solana token {MINT}. Show deployer funding, early-buyer linked wallets and the transaction evidence behind each finding."
    assert mobula_meme.declared_token(req) == MINT
    card = mobula_meme.wallet_deployer(req)
    assert (1, "/metadata", MINT) in calls and (2, "/wallet/deployer", DEPLOYER) in calls
    assert f"**Wallet**: `{DEPLOYER}` · **Token**: `{MINT}`" in card
    # no deployer known: the mint is not used in its place
    monkeypatch.setattr(mobula_meme.mobula_client, "get", lambda *a, **k: {"name": "x"})
    calls.clear()
    card = mobula_meme.wallet_deployer(f"deployer track record for token {MINT}")
    assert "names no deployer for this token" in card and "was not used in its place" in card and not [c for c in calls if c[1] == "/wallet/deployer"]
    # a wallet named as a wallet is still a wallet
    assert mobula_meme.declared_token(f"what did wallet {DEPLOYER} deploy") is None


# ---- composition ---------------------------------------------------------

def test_clauses_without_a_subject_carry_the_message_address():
    req = f"Investigate the launch of Solana token {MINT}. Show deployer funding, early-buyer linked wallets and the transaction evidence behind each finding."
    clauses = composition.carry_subject(composition.split_asks(req), req)
    assert len(clauses) == 2 and MINT in clauses[0] and clauses[1].endswith(f"(token {MINT} on solana)")
    assert composition.carry_subject(["price of BONK", "and WIF volume"], "price of BONK and WIF volume") == ["price of BONK", "and WIF volume"]
    req = "What changed in ANSEM on Solana holders between September 21 and September 22, 2026? Compare saved snapshots. If snapshots are unavailable, say so."
    clauses = composition.carry_subject(composition.split_asks(req), req)
    assert clauses[1:] == ["Compare saved snapshots (ANSEM token on solana)", "If snapshots are unavailable, say so (ANSEM token on solana)"]
    assert composition.carry_subject(["show the market", "and the movers"], "show the market and the movers") == ["show the market", "and the movers"]


def test_an_earlier_synthesis_is_stripped_before_the_next():
    answer = "**Taken together**\n\nA summary.\n\n_Taken together from the cards below. Not financial advice._\n\n---\n\n# Card A\n\nrows\n\n---\n\n# Card B"
    assert composition.strip_synthesis(answer) == "# Card A\n\nrows\n\n---\n\n# Card B"
    assert composition.strip_synthesis("# Card only") == "# Card only"


def test_muted_events_are_dropped_only_inside_the_block():
    async def run():
        q = asyncio.Queue()
        token = streaming.attach(q)
        try:
            streaming.emit("delta", text="a")
            with streaming.muted("delta"):
                streaming.emit("delta", text="b")
                streaming.emit("status", text="s")
            streaming.emit("delta", text="c")
        finally:
            streaming.detach(token)
        return [q.get_nowait() for _ in range(q.qsize())]
    events = asyncio.run(run())
    assert [(e["event"], e.get("text")) for e in events] == [("delta", "a"), ("status", "s"), ("delta", "c")]


# ---- dated comparison ----------------------------------------------------

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def test_a_dated_ask_is_recognised_with_its_window():
    w = snapshot_compare.dated_ask("What changed in ANSEM on Solana holders, liquidity and deployer activity between September 21 and September 22, 2026?", NOW)
    assert w == (datetime(2026, 9, 21, tzinfo=timezone.utc), datetime(2026, 9, 22, 23, 59, 59, tzinfo=timezone.utc))
    assert snapshot_compare.dated_ask("what changed since 2026-09-20", NOW)[1] == NOW
    assert snapshot_compare.dated_ask("price of BONK on September 21", NOW) is None       # no comparison asked
    assert snapshot_compare.dated_ask("what changed for BONK today", NOW) is None          # no date


def _row(when, top10, price, id_):
    return {"id": id_, "taken_at": when, "top10_pct": top10, "top50_pct": 50.0, "holders_count": 100, "dev_pct": 0.0, "sniper_pct": 0.0, "bundler_pct": 0.0,
            "insider_pct": 0.0, "lp_burned_pct": 0.0, "lp_locked_pct": 0.0, "lp_unlocked_pct": 100.0, "price_usd": price, "liquidity_usd": 1e5, "market_cap_usd": 1e6,
            "top_holders": [], "flags": {"missing": []}}


def test_the_lead_is_the_ledger_comparison_or_the_limitation():
    holder_snapshots.reset_for_test()
    token = {"address": MINT, "chain": "solana", "symbol": "ANSEM"}
    window = (datetime(2026, 9, 21, tzinfo=timezone.utc), datetime(2026, 9, 22, 23, 59, 59, tzinfo=timezone.utc))
    lead = asyncio.run(snapshot_compare.lead(token, window))
    assert lead.startswith("**No saved snapshot of ANSEM for September 21, 2026.** Anvaya has not recorded ANSEM") and "current data, not a change over that period" in lead
    key = f"token:solana:{MINT}"
    from app.signals import Subject
    key = Subject(kind="token", id=MINT, chain="solana", symbol="ANSEM").key
    asyncio.run(holder_snapshots.store(key, _row(datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc), 30.0, 0.01, "a")))
    asyncio.run(holder_snapshots.store(key, _row(datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc), 36.0, 0.008, "b")))
    lead = asyncio.run(snapshot_compare.lead(token, window))
    assert lead.startswith("# Snapshot comparison — ANSEM") and "**Earlier**: 2026-09-21 09:00 UTC · **Later**: 2026-09-22 21:00 UTC" in lead
    assert "- Top-10 share 30.00% → 36.00%" in lead and "- Price $0.01 → $0.008 (-20.0%)" in lead and "The cards below are current data" in lead
    assert asyncio.run(snapshot_compare.lead(None, window)).startswith("**No saved snapshot to compare.**")
    holder_snapshots.reset_for_test()


# ---- exit controls -------------------------------------------------------

WALLET = "7GK7tZ1yD1mS3sJt7dcqJ2k5aZm7gJ8yq1uZ6R9WWEFm"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
USDC = exit_monitor.USDC_MINT


def _sim(price, impact, route=("Raydium",)):
    async def simulate_swap(input_mint, output_mint, amount):
        if input_mint == USDC:
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


@pytest.fixture
def chain(monkeypatch):
    jobs.reset_for_test(); exit_monitor.reset_for_test()
    holding = {"amount": 1_000_000_00000}

    async def accounts_(wallet):
        return {"value": [{"account": {"data": {"parsed": {"info": {"mint": BONK, "tokenAmount": {"amount": str(holding["amount"]), "decimals": 5}}}}}}]}
    async def none(wallet):
        return {"value": []}
    monkeypatch.setattr(exit_monitor, "get_token_accounts", accounts_)
    monkeypatch.setattr(exit_monitor, "_token_accounts_2022", none)
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.00002, 2.0))
    monkeypatch.setattr(exit_controls, "_resolved", {})

    async def search(query):
        return [{"id": BONK, "symbol": "BONK", "name": "Bonk", "tags": ["verified"]}]
    monkeypatch.setattr(exit_controls.jupiter, "search_tokens", search)
    return holding


def test_the_full_sizing_prompt_is_the_same_control(chain):
    long = "Compare buying $500, $2,000 and $5,000 of BONK on Solana. Show entry quotes and immediate reverse-exit estimates. Do not prepare or execute any trade."
    assert exit_controls.is_exit_control(long)
    reply = asyncio.run(exit_controls.handle(long, {"id": "u1"}, None))
    assert reply.startswith("# Sizing — BONK") and "| $5,000.00 |" in reply and "Nothing here prepares or submits a trade" in reply
    assert exit_controls.is_public_control(long) and asyncio.run(exit_controls.handle(long, None, None)).startswith("# Sizing — BONK")   # a guest may size
    assert asyncio.run(exit_controls.handle("show my exits", None, None)).startswith("Sign in to use exit monitors")


def test_what_changed_since_entry_reads_the_baseline_or_says_there_is_none(chain, monkeypatch):
    user = {"id": "u1"}
    ask = "What changed since I entered BONK? Separate token price movement, changes in my holdings, and worsening exit liquidity. If you do not have my entry snapshot, say so."
    assert exit_controls.is_exit_control(ask)
    reply = asyncio.run(exit_controls.handle(ask, user, WALLET))
    assert reply.startswith("I have no entry snapshot for BONK: you are not watching an exit on it")
    asyncio.run(exit_controls.handle("watch my exit on BONK", user, WALLET))
    chain["amount"] = 800_000_00000                                                  # sold a fifth
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.000018, 5.0, route=("Orca",)))   # price -10%, book thinner
    reply = asyncio.run(exit_controls.handle(ask, user, WALLET))
    assert reply.startswith("# Since entry — BONK")
    assert "- **Holding**: 1,000,000 → 800,000 BONK (-20.0%)." in reply
    assert "- **Reference price**: $0.00002000 → $0.00001800 (-10.0%)." in reply
    assert "- **Route discount to the reference**: 2.0% → 5.0% (wider: the book got thinner for your size)." in reply
    assert "- **Route**: Raydium → Orca; price impact 2.00% → 5.00%." in reply and "Quotes computed at slot 101" in reply
    # missing data is its own state
    async def down(*a):
        raise RuntimeError("Jupiter quote failed: 503 Service Unavailable")
    monkeypatch.setattr(exit_monitor, "simulate_swap", down)
    reply = asyncio.run(exit_controls.handle(ask, user, WALLET))
    assert "the full-exit quote is unavailable right now" in reply and "no current reading came back" in reply


def test_a_clause_that_asks_back_does_not_make_the_answer_a_clarification(monkeypatch):
    from app.nodes import research

    async def fake(state, sink):
        if state["request"].startswith("Compare"):
            return {"answer": "Which token should I check? Paste its contract address.", "trajectory": None}
        sink["resolved_token"] = {"symbol": "ANSEM", "address": MINT, "chain": "solana"}
        return {"answer": "# Holders — ANSEM\n\nrows", "trajectory": {"tool_name_0": "mobula_token_holders"}, "resolved_token": sink["resolved_token"]}

    async def synth(request, cards, trajectory, advice=False):
        return f"**Taken together**\n\nsummary\n\n---\n\n{cards}"

    async def gate(q, r):
        return r
    monkeypatch.setattr(research, "_research_node", fake)
    monkeypatch.setattr(research.composition, "synthesize", synth)
    monkeypatch.setattr(research.answer_gate, "gate", gate)
    monkeypatch.setattr(research, "_web_context_part", lambda state: None)
    holder_snapshots.reset_for_test()
    out = asyncio.run(research.research_node({"request": "What changed in ANSEM holders between September 21 and September 22, 2026? Compare saved snapshots.",
                                              "contextual_request": None, "history": "", "session_context": {}}))
    assert out["answer"].startswith("**No saved snapshot of ANSEM for September 21, 2026.**")
    assert "Which token should I check" in out["answer"] and "# Holders — ANSEM" in out["answer"]      # every ask kept; the lead still leads


# ---- review of c8bf7764 (2026-09-23) --------------------------------------

def test_a_dollar_ticker_is_carried_whole():
    req = "Show $BONK holders. Check liquidity."
    assert composition.carry_subject(composition.split_asks(req), req) == ["Show $BONK holders", "Check liquidity (BONK token)"]


def test_sizing_accepts_a_mint_with_digits(chain):
    ask = f"Compare buying $500 of {BONK}"
    m = exit_controls._SIZES.match(ask)
    assert m and m.group("t1") == BONK
    reply = asyncio.run(exit_controls.handle(ask, None, None))
    assert reply.startswith("# Sizing — BONK")


def test_since_entry_after_selling_everything_and_with_two_wallets(chain, monkeypatch):
    user = {"id": "u1"}
    other = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
    asyncio.run(exit_controls.handle("watch my exit on BONK", user, WALLET))
    asyncio.run(exit_controls.handle("watch my exit on BONK", user, other))
    ask = "What changed since I entered BONK?"
    reply = asyncio.run(exit_controls.handle(ask, user, other))
    assert reply.count("# Since entry — BONK") == 1 and f"`{other[:6]}…{other[-4:]}`" in reply       # the connected wallet's watch
    reply = asyncio.run(exit_controls.handle(ask, user, None))
    assert reply.count("# Since entry — BONK") == 2                                                    # no wallet: every watch, each on its own
    chain["amount"] = 0
    async def none(wallet):
        return {"value": []}
    monkeypatch.setattr(exit_monitor, "get_token_accounts", none)
    reply = asyncio.run(exit_controls.handle(ask, user, WALLET))
    assert "this wallet no longer holds the token" in reply and "1,000,000 → 0 BONK" in reply


def test_holding_change_is_read_against_the_baseline_not_the_latest_tick(chain, monkeypatch):
    user = {"id": "u1"}
    asyncio.run(exit_controls.handle("watch my exit on BONK", user, WALLET))
    watched = asyncio.run(exit_monitor.list_for("u1"))[0]
    chain["amount"] = 995_000_00000                                     # -0.5%: below the re-baseline threshold
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))    # the tick updates the row's quantity, not the baseline
    row = asyncio.run(exit_monitor.list_for("u1"))[0]
    assert int(row["quantity_raw"]) == 995_000_00000 and int(row["entry"]["quantity_raw"]) == 1_000_000_00000
    reply = asyncio.run(exit_controls.handle("What changed since I entered BONK?", user, WALLET))
    assert "- **Holding**: 1,000,000 → 995,000 BONK (-0.5%)." in reply


def test_a_failed_alert_email_is_retried_on_the_next_tick(chain, monkeypatch):
    sent, ok = [], {"value": False}

    async def send_email(to, subject, html, text=None):
        sent.append(subject)
        return ok["value"]
    async def get_user(user_id, db=None):
        return {"id": user_id, "email": "holder@example.com"}
    monkeypatch.setattr(emailer, "send_email", send_email)
    monkeypatch.setattr(accounts, "get_user", get_user)
    asyncio.run(exit_controls.handle("watch my exit on BONK", {"id": "u1"}, WALLET))
    watched = asyncio.run(exit_monitor.list_for("u1"))[0]
    asyncio.run(exit_monitor.set_rules(watched["id"], channel="email", drop_pct=10.0))
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.000015, 2.0))
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))
    assert len(sent) == 1 and asyncio.run(exit_monitor.list_for("u1"))[0]["last_alert"]["email_pending"] is True
    ok["value"] = True
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))                 # inside the cooldown: no new alert, the email retried
    row = asyncio.run(exit_monitor.list_for("u1"))[0]
    assert len(sent) == 2 and row["last_alert"]["email_pending"] is False and len(asyncio.run(tasks.inbox("u1"))) == 1
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))
    assert len(sent) == 2                                                            # delivered: nothing more to send

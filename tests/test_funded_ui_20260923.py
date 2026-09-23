"""The funded UI review of 2026-09-23 (48 persona cases): follow-ups keep
the subject, imperative words are never assets, a mint with token words is
not a wallet, exit controls tolerate punctuation and trailing clauses, user
constraints filter before ranking, and a conditional order gets a plain no."""
import asyncio

import pytest

from app import additional_providers, dexscreener_tools, exit_controls, exit_monitor, jobs, tasks_nl, wallet_insights
from app.context_entities import resolve_contextual_request
from app.experience import advance_session_context
from app.routing import subject_probe
from app.routing.speech import CONDITIONAL_ORDER_ANSWER, is_conditional_order

ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


@pytest.mark.parametrize("request_", [
    "You showed 24-hour changes. Which are price changes and which are volume changes? Show the original metric names.",
    "List funding rounds by date, amount, instrument, investors and primary source. Mark database-only claims.",
    "Build bull, base and bear cases. Label assumptions; do not invent valuation multiples or probabilities.",
    "Compare top token accounts with beneficial-owner concentration. Handle pools, exchanges, PDAs and burn addresses explicitly.",
    "Which LP pools are executable for a full exit? Explain CLMM or DLMM depth, active liquidity, slippage and network fees.",
    "I am new to crypto. What can I do here without connecting a wallet? Explain in plain language.",
    "Were early buys bundled or sniped? Show the funding links and transactions you examined.",
    "How concentrated are customers, operators and dependencies? Link incidents and distinguish resolved issues from open risks.",
    "Revisit your conclusion: which claims are proven, inferred, disputed or unsupported?",
])
def test_instruction_sentences_name_no_subject(request_):
    assert subject_probe.subject_of(request_) is None and not subject_probe.has_own_subject(request_)


def test_real_names_still_are_subjects():
    assert subject_probe.subject_of("Prepare an initial diligence brief on EigenLayer and Eigen Labs.") == "EigenLayer"
    assert subject_probe.subject_of("Bonk price?") == "Bonk" and subject_probe.subject_of("ANSEM holders on solana") == "ANSEM"
    assert subject_probe.has_own_subject(f"latest transactions for {ANSEM}")


def test_a_subjectless_follow_up_continues_the_token_or_topic():
    token_focus = {"focus": {"kind": "token", "label": "ANSEM", "address": ANSEM, "chain": "solana"}}
    out = resolve_contextual_request("Were early buys bundled or sniped? Show the transactions you examined.", "user: deep dive on ANSEM", token_focus)
    assert out.endswith(f"Resolved from canonical session context: token ANSEM mint {ANSEM} on solana.")
    topic_focus = {"focus": {"kind": "topic", "label": "EigenLayer", "address": None, "chain": None}}
    out = resolve_contextual_request("Build bull, base and bear cases. Label assumptions.", "user: diligence brief on EigenLayer", topic_focus)
    assert "Resolved from conversation context: this continues the discussion about EigenLayer" in out
    # a message with its own subject is left alone; a two-word control too
    assert resolve_contextual_request("Compare EigenLayer with Symbiotic and Karak", "user: x", topic_focus) == "Compare EigenLayer with Symbiotic and Karak"
    assert resolve_contextual_request("show tasks", "user: x", token_focus) == "show tasks"
    assert resolve_contextual_request("what time is it", "user: x", token_focus) == "what time is it"
    assert resolve_contextual_request("What's trending in crypto right now?", "user: x", token_focus) == "What's trending in crypto right now?"


def test_the_session_keeps_or_takes_a_topic_focus():
    ctx = {"focus": {"kind": "token", "label": "ANSEM", "address": ANSEM, "chain": "solana"}}
    ctx = advance_session_context(ctx, "Were early buys bundled or sniped?", None, "research", [], [], None)
    assert ctx["focus"]["label"] == "ANSEM"                                            # a subject-less follow-up keeps the subject
    ctx = advance_session_context(ctx, "Prepare an initial diligence brief on EigenLayer.", None, "research", [], [], None)
    assert ctx["focus"] == {"kind": "topic", "label": "EigenLayer", "address": None, "chain": None, "confidence": 0.6, "source": "request"}
    ctx = advance_session_context(ctx, "Build bull, base and bear cases. Label assumptions.", None, "research", [], [], None)
    assert ctx["focus"]["label"] == "EigenLayer"                                       # a follow-up on the analysis keeps the topic
    ctx = advance_session_context(ctx, "What's trending in crypto right now?", None, "research", [], [], None)
    assert ctx["focus"] is None                                                         # a market-wide ask opens a new topic


def test_one_letter_interception_needs_asset_intent():
    from app.nodes import research
    assert research._DATA_ASK.search("is M safe?") and not research._DATA_ASK.search("I am new to crypto. What can I do here without connecting a wallet?")


def test_a_mint_with_token_words_is_not_a_wallet_history():
    assert additional_providers._token_role(f"latest transactions and first buyers for {ANSEM}")
    assert additional_providers._token_role(f"token {ANSEM} recent activity")
    assert not additional_providers._token_role(f"recent transactions for wallet {ANSEM}")
    assert not additional_providers._token_role(f"activity of {ANSEM}")


def test_single_asset_yields_and_named_protocol_matching(monkeypatch):
    pools = [{"symbol": "USDC", "project": "aave-v3", "chain": "Base", "apy": 4.1, "tvlUsd": 5e7, "exposure": "single", "stablecoin": True},
             {"symbol": "USDC-WETH", "project": "aerodrome", "chain": "Base", "apy": 41.0, "tvlUsd": 3e7, "exposure": "multi", "ilRisk": "yes"}]
    monkeypatch.setattr(additional_providers, "_llama_pools", lambda: pools)
    owner = additional_providers.DefiLlamaProvider
    card = owner().yields("Find USDC yield on Base without exposing me to another volatile token")
    assert "aave-v3" in card and "aerodrome" not in card and "single-asset only" in card
    card = owner().yields("best USDC yield on Base")
    assert "aerodrome" in card


def test_protocol_tvl_matches_the_named_protocol_only(monkeypatch):
    import httpx
    rows = [{"name": "Aave V3", "slug": "aave-v3", "tvl": 1e10, "chains": ["Ethereum"]}, {"name": "What The Hook", "slug": "what-the-hook", "tvl": 1e6, "chains": ["Solana"]}]

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return rows

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, *a, **k): return _Resp()
    monkeypatch.setattr(httpx, "Client", _Client)
    card = additional_providers.DefiLlamaProvider().protocols("what is Aave's TVL right now")
    assert "Aave V3" in card and "What The Hook" not in card


def test_bnb_chain_aliases_scope_discovery():
    assert dexscreener_tools._chain("trending tokens on BNB Smart Chain") == "bsc"
    assert dexscreener_tools._chain("new pairs on bnb") == "bsc"


# ---- exit controls ---------------------------------------------------------

WALLET = "7GK7tZ1yD1mS3sJt7dcqJ2k5aZm7gJ8yq1uZ6R9WWEFm"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


@pytest.fixture
def chain(monkeypatch):
    from tests.test_ui_flows_20260923 import _sim
    jobs.reset_for_test(); exit_monitor.reset_for_test()

    async def accounts_(wallet):
        return {"value": [{"account": {"data": {"parsed": {"info": {"mint": BONK, "tokenAmount": {"amount": "100000000000", "decimals": 5}}}}}}]}
    async def none(wallet):
        return {"value": []}
    monkeypatch.setattr(exit_monitor, "get_token_accounts", accounts_)
    monkeypatch.setattr(exit_monitor, "_token_accounts_2022", none)
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.00002, 2.0))
    monkeypatch.setattr(exit_controls, "_resolved", {})

    async def search(query):
        assert "." not in query, query                     # the ticker is looked up without the sentence's full stop
        return [{"id": BONK, "symbol": "BONK", "name": "Bonk", "tags": ["verified"]}]
    monkeypatch.setattr(exit_controls.jupiter, "search_tokens", search)


def test_exit_controls_tolerate_punctuation_and_trailing_clauses(chain):
    user = {"id": "u1"}
    detailed = "Can I exit my BONK position in the connected wallet? Show 25%, 50% and 100% quotes to USDC."
    assert exit_controls.is_exit_control(detailed)
    assert asyncio.run(exit_controls.handle(detailed, user, WALLET)).startswith("# Exit analysis — BONK")
    assert asyncio.run(exit_controls.handle("Compare buying $500, $2,000 and $5,000 of BONK. Show entry quotes and immediate reverse-exit estimates. Do not execute.", None, None)).startswith("# Sizing — BONK")
    asyncio.run(exit_controls.handle("watch my exit on BONK", user, WALLET))
    reply = asyncio.run(exit_controls.handle("Tell me when the discount on my full-position exit quote for BONK exceeds 5%. What exactly will trigger this?", user, WALLET))
    assert reply.startswith("Set: you will be told when the discount to the reference price exceeds 5% for BONK.")
    assert "Trigger: every 15 minutes Jupiter is asked" in reply and "It never sells and never places an order." in reply


# ---- conditional orders, wallet health, reminders --------------------------

def test_a_conditional_order_is_answered_plainly():
    assert is_conditional_order("If SOL drops below $100, automatically buy 2 SOL.") and is_conditional_order("if SOL drops under 100 buy 2 SOL")
    assert not is_conditional_order("should I buy SOL?") and not is_conditional_order("alert me when SOL drops below $100")
    assert not is_conditional_order("should I buy BONK if it dips?") and not is_conditional_order("what if SOL drops below 100, should I sell?")
    assert "never buys, sells or submits a transaction on its own" in CONDITIONAL_ORDER_ANSWER


def test_wallet_health_states_concentration_without_a_verdict():
    report = wallet_insights.wallet_health({"wallet": "w", "sol": {"amount": 0.07, "usd_value": 8.0, "allocation_pct": 81.0},
                                           "holdings": [{"symbol": "ANSEM", "allocation_pct": 19.0, "verified": True}], "unpriced_holdings": 0})
    assert report["status"] == "healthy" and "not that holdings are safe or liquid" in report["scope"]
    report = wallet_insights.wallet_health({"wallet": "w", "sol": {"amount": 0.07}, "holdings": [{"symbol": "X", "allocation_pct": 90.0, "verified": True}], "unpriced_holdings": 0})
    assert "Concentration is a fact, not a verdict" in report["findings"][0]["detail"]


def test_reminder_naming_and_channel_are_fields_not_message():
    rest, title, channel = tasks_nl._reminder_fields("in 3 minutes to review my research. Name it Persona E2E disposable reminder. Use the in-app inbox only.")
    assert (rest, title, channel) == ("in 3 minutes to review my research", "Persona E2E disposable reminder", "inapp")
    rest, title, channel = tasks_nl._reminder_fields("tomorrow at 9am to check SOL by email")
    assert (rest, title, channel) == ("tomorrow at 9am to check SOL", None, "email")
    assert tasks_nl._reminder_fields("in 2 hours to rebalance") == ("in 2 hours to rebalance", None, "inapp")


def test_a_resolution_note_never_turns_a_question_into_a_simulation_or_a_wallet_read():
    from app.capability_router import route_capabilities
    from app.nodes import research
    q = ("I mean the Ethereum token. What should I check to avoid buying the wrong one?\n"
         "Resolved from canonical session context: token PEPE mint 0x6982508145454Ce325dDbE47a25d4ec3d2311933 on ethereum.")
    c = route_capabilities(q)
    assert not (c and c.reason == "trade_simulation")
    assert research._detect_wallet_request(f"latest transactions and first buyers for {ANSEM}") is None
    assert research._detect_wallet_request(f"recent transactions for wallet {ANSEM}") is not None


def test_the_general_node_answers_product_questions_itself():
    from app.nodes import general
    state = {"request": "I am new to crypto. What can I do here without connecting a wallet? Explain in plain language.",
             "routing_decision": {"speech_act": "explain"}, "session_context": {}, "conversation_history": ""}
    out = asyncio.run(general.general_node(state))
    assert out["answer"].startswith("**What you can do here without connecting a wallet.**") and "no trade is placed" in out["answer"]


def test_a_product_question_is_routed_before_any_model():
    from app.routing import resolver

    async def never(*a, **k):
        raise AssertionError("the model must not be consulted")
    out = asyncio.run(resolver.resolve({"request": "I am new to crypto. What can I do here without connecting a wallet? Explain in plain language.",
                                        "contextual_request": None, "session_context": {}}, never))
    assert out["intent"] == "general" and out["routing_decision"]["reason"] == "product_question"

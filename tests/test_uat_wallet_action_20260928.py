"""The wallet and action-flow UAT probe (reports/uat-wallet-action-2026-09-28):
one stated swap read four different ways by the keyword rules, a product
phrase pre-empting a stated action, and a Token-2022 position reported as
"your wallet holds no ANSEM"."""
from __future__ import annotations

import asyncio

import pytest

from app import agent, contracts, product_actions
from app.routing.resolver import stated_swap

WALLET = "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS"
ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"

# the four live phrasings of one request: sell 1 ANSEM for USDC, read-only, 50 bps
PHRASINGS = [
    "With this view-only wallet, prepare a sale of 1 ANSEM for USDC at max 50 bps. Do not sign or submit anything.",
    "Sell 1 ANSEM for USDC from this wallet. Prepare only; do not execute.",
    "Read-only quote: what would selling 1 ANSEM for USDC return now at 50 bps? Do not prepare or submit a transaction.",
    "What would I get if I sold 1 ANSEM for USDC right now? 50 bps slippage. Quote only.",
]


@pytest.mark.parametrize("request_text", PHRASINGS)
def test_every_phrasing_of_one_swap_reads_as_the_same_contract(request_text):
    plan = contracts.plan_by_rules(request_text)
    assert plan.kind == "transaction_intent"
    filters = plan.filters or {}
    assert filters["amount"] == "1" and filters["input_token"] == "ANSEM" and filters["output_token"] == "USDC"
    assert stated_swap(request_text)


@pytest.mark.parametrize("request_text,intent,reason", [
    (PHRASINGS[0], "trade", "stated_swap"),              # "prepare a sale …": the execution route, where the deployment gate answers
    (PHRASINGS[1], "trade", "stated_swap"),              # "sell … prepare only": the same, however phrased
    (PHRASINGS[2], "portfolio", "stated_swap_quote"),    # "read-only quote …": the read-only quote
    (PHRASINGS[3], "portfolio", "stated_swap_quote"),    # "what would I get …": the same
])
def test_a_stated_swap_anchors_the_route(request_text, intent, reason):
    """The keyword rules had read these four as own_wallet, the model,
    current_information and trade_simulation; the contract parser decides the
    family and the read-only wording decides quote versus preparation."""
    async def never(*args, **kwargs):
        raise AssertionError("the model must not be asked: the contract states the fields")
    out = asyncio.run(_resolve(request_text, never))
    assert out["intent"] == intent and out["routing_decision"]["reason"] == reason and out["route_source"] == "rules"
    if intent == "portfolio":
        assert "trade_simulation" in out["capabilities"]


def test_an_ordinary_swap_keeps_its_execution_route_and_venue():
    async def never(*args, **kwargs):
        raise AssertionError("the model must not be asked")
    out = asyncio.run(_resolve("swap 0.1 SOL to USDC on solana", never))
    assert out["intent"] == "trade" and out["execution_provider"] == "jupiter" and "swap" in out["capabilities"]
    out = asyncio.run(_resolve("what would happen if I sold 0.05 SOL for USDC", never))
    assert out["intent"] == "portfolio" and "trade_simulation" in out["capabilities"]


def _resolve(request_text: str, call_lm):
    from app.routing import resolver
    return resolver.resolve({"request": request_text, "session_context": {}, "history": ""}, call_lm)


def test_a_product_phrase_never_pre_empts_a_stated_action():
    assert not product_actions.is_product_question(PHRASINGS[0])          # "view-only wallet" + a stated sale
    assert product_actions.is_product_question("Can I see a wallet without giving you control of it?")
    assert product_actions.is_product_question("what can I do here without connecting a wallet?")


def test_the_contract_vocabulary_knows_the_swap_nominalisations():
    for text, verb in (("prepare a sale of 1 ANSEM for USDC", "ANSEM"), ("prepare a purchase of 2 SOL with USDC", "SOL")):
        assert contracts.plan_by_rules(text).kind == "transaction_intent", text
        assert (contracts.plan_by_rules(text).filters or {}).get("input_token") == verb
    assert contracts.plan_by_rules("how does a sale of a token work?").kind != "transaction_intent"


# --- the false "holds no ANSEM": a Token-2022 position the tool could not see ---

def _accounts(mint: str, amount: float) -> dict:
    return {"value": [{"account": {"data": {"parsed": {"info": {"mint": mint, "tokenAmount": {"uiAmount": amount}}}}}}]}


def test_the_balance_tool_reads_both_token_programs(monkeypatch):
    async def standard(wallet):
        return {"value": []}

    async def token_2022(wallet):
        return _accounts(ANSEM, 11.522238)
    monkeypatch.setattr(agent, "get_token_accounts", standard)
    monkeypatch.setattr(agent, "get_token_accounts_2022", token_2022)
    out = agent.spl_balances(WALLET)
    assert out["holdings"] == [{"mint": ANSEM, "amount": 11.522238}] and out["complete"] is True
    assert out["programs_read_failed"] == [] and "absence_not_established" not in out


def test_a_failed_program_read_is_never_a_zero_balance(monkeypatch):
    async def standard(wallet):
        return {"value": []}

    async def broken(wallet):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(agent, "get_token_accounts", standard)
    monkeypatch.setattr(agent, "get_token_accounts_2022", broken)
    out = agent.spl_balances(WALLET)
    assert out["holdings"] == [] and out["complete"] is False
    assert out["programs_read_failed"] == ["token-2022 program: RuntimeError"]
    assert "do not say the wallet holds none" in out["absence_not_established"]


def test_a_truncated_list_cannot_establish_absence(monkeypatch):
    many = {"value": [{"account": {"data": {"parsed": {"info": {"mint": f"mint{i}", "tokenAmount": {"uiAmount": float(i + 1)}}}}}}
                      for i in range(agent.LLM_MAX_HOLDINGS + 5)]}

    async def standard(wallet):
        return many

    async def token_2022(wallet):
        return {"value": []}
    monkeypatch.setattr(agent, "get_token_accounts", standard)
    monkeypatch.setattr(agent, "get_token_accounts_2022", token_2022)
    out = agent.spl_balances(WALLET)
    assert out["truncated"] is True and out["complete"] is False
    assert len(out["holdings"]) == agent.LLM_MAX_HOLDINGS and "truncated" in out["absence_not_established"]
    assert out["token_account_count"] == agent.LLM_MAX_HOLDINGS + 5


def test_a_stated_swap_still_reaches_team_mode_and_the_topical_rules_yield():
    """The anchor is a branch in the decision chain, not a bypass: team mode,
    venue detection and the rest still apply."""
    async def never(*args, **kwargs):
        raise AssertionError("the model must not be asked")
    from app.routing import resolver
    out = asyncio.run(resolver.resolve({"request": "swap 0.01 SOL to USDC with 50 bps slippage",
                                        "session_context": {"team_mode": True}, "history": ""}, never))
    assert out["intent"] == "team" and out["team_subintent"] == "trade" and out["execution_provider"] == "jupiter"


def test_research_mode_leads_with_the_mode_never_with_a_missing_field(monkeypatch):
    """"To quote this I need the chain" before "this deployment is in research
    mode" reads as though the swap were otherwise available."""
    from app import deployment
    from app.nodes import trading
    from app.settings import settings
    monkeypatch.setattr(deployment, "execution_enabled", lambda: False)
    monkeypatch.setattr(settings, "contract_pipeline_enabled", True)      # the conftest disables it by default
    out = asyncio.run(trading.trade_planner_node({"request": PHRASINGS[1], "capabilities": [], "missing_fields": ["chain"]}))
    answer = out["answer"]
    assert answer.startswith("This deployment is running in research mode") and "I need the chain" not in answer
    assert "1 ANSEM to USDC" in answer


def test_a_stated_size_quote_uses_the_contracts_fields_even_with_a_wallet_bound(monkeypatch):
    """A bound wallet had sent the quote to the ReAct simulator, which quoted
    1,000 ANSEM for "selling 1 ANSEM" and, before that, claimed the wallet
    held none."""
    from app.nodes import portfolio as portfolio_node
    from app.settings import settings
    monkeypatch.setattr(settings, "contract_pipeline_enabled", True)
    assert portfolio_node._stated_swap_fields(PHRASINGS[2]) and portfolio_node._stated_swap_fields(PHRASINGS[3])
    assert not portfolio_node._stated_swap_fields("what is my exposure to SOL?")
    seen = {}

    def complete(spelled, history, *rest):
        seen.setdefault("calls", []).append(spelled)
        return ("So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 1_000_000, None)

    async def never(*args, **kwargs):
        raise AssertionError("the ReAct simulator must not re-derive a stated swap")
    monkeypatch.setattr(portfolio_node, "complete_swap_fields", complete)
    monkeypatch.setattr(portfolio_node.runtime, "answer", never)

    async def verified_mint(symbol):
        return "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    monkeypatch.setattr(portfolio_node, "_verified_mint", verified_mint)

    async def simulate(*args, **kwargs):
        return {"answer": "quoted", "trajectory": None}
    monkeypatch.setattr(portfolio_node, "_simulate_swap", simulate, raising=False)
    try:
        asyncio.run(portfolio_node.portfolio_node({
            "request": PHRASINGS[2], "wallet_address": WALLET, "capabilities": ["trade_simulation", "portfolio"], "history": "",
        }))
    except Exception:
        pass                                        # the quote itself needs the chain; the binding is what this asserts
    assert seen["calls"][0] == "swap 1 ANSEM to USDC"      # the contract's fields, not the model's reading

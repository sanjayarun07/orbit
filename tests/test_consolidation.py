from app.nodes import runtime, routing, portfolio, research
import asyncio
from types import SimpleNamespace

from app import graph, plans, execution
from app.context_entities import extract_token_reference, resolve_contextual_request
from app.dexscreener_tools import dexscreener_pair_search
from app.market_providers import DexScreenerProvider
from app.provider_router import ProviderRouter
from app.suggestions import suggested_actions, structured_quick_actions
from app.routing.workflow import WorkflowState, WorkflowEvent, apply_event

WALLET = "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"
MINT = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def test_public_activity_does_not_expose_raw_arguments_or_errors():
    from app.public_activity import public_activity
    raw = {"tool_name_0": "perplexity_web_search", "thought_0": "private",
           "tool_args_0": {"api_key": "secret"},
           "observation_0": "Provider failed: request headers secret"}
    exposed = public_activity(raw)
    assert exposed["tool_name_0"] == "perplexity_web_search"
    assert "failed" in exposed["observation_0"]
    assert "secret" not in str(exposed) and "private" not in str(exposed)


def test_public_research_progress_shows_source_status_without_model_text():
    from app.public_activity import public_activity
    raw = {"research_loop": {
        "question": "private model planning text", "calls": ["web_discovery: private query", "wallet_state: skipped (no wallet)"],
        "verdicts": [
            {"name": "Akash", "url": "https://akash.network/docs?id=42&api_key=secret", "verdict": "related_but_different", "provenance": "page", "quote": "private source passage"},
            {"name": "Unsafe", "url": "javascript:alert(1)", "verdict": "qualifies", "provenance": "page"},
        ],
    }}
    exposed = public_activity(raw)
    assert exposed == {"research_progress": {"status": "complete", "provider_calls": 1, "checked_pages": 0, "sources": [
        {"name": "Akash", "url": "https://akash.network/docs?id=42", "verdict": "related_but_different", "provenance": "page"},
    ]}}
    assert "private" not in str(exposed)


def test_public_research_progress_counts_distinct_direct_page_reads():
    from app.public_activity import public_activity
    raw = {"research_loop": {"calls": [
        "page_read (url_reader): https://docs.example.org/one",
        "page_read (url_reader): https://docs.example.org/two",
        "page_read (url_reader): https://docs.example.org/two",
        "page_read (perplexity_fetch_url): https://docs.example.org/three",
    ], "verdicts": []}}
    public = public_activity(raw)
    assert public["research_progress"]["checked_pages"] == 2
    assert public_activity(public) == public


def test_wallet_leverage_action_keeps_wallet_capability(monkeypatch):
    action = structured_quick_actions([f"Review leverage risk for {WALLET}"], 1)[0]
    assert action.capabilities == ["wallet_intelligence"]
    def tool():
        pass
    monkeypatch.setattr(runtime._mcp_registry, "get", lambda _: tool)
    name, arguments = research.direct_mcp_request(action.prompt)
    assert arguments["request"]["walletAddress"] == WALLET


def test_token_overview_after_swap_uses_bound_entity():
    state = {"focus": {"kind": "token", "label": "ANSEM", "address": MINT, "chain": "solana"}}
    resolved = resolve_contextual_request("tell me about ANSEM", "assistant: CONFIRM old-plan", state)
    assert MINT in resolved
    route = asyncio.run(graph.resolve_intent_node({"request": "tell me about ANSEM", "contextual_request": resolved, "session_context": state}))
    assert route["intent"] == "research"
    assert "token_discovery" in route["capabilities"]


def test_dex_search_filters_irrelevant_liquid_pairs(monkeypatch):
    from app import dexscreener_tools as dex
    queries = []
    def get(path, params=None):
        queries.append(params["q"])
        return {"pairs": [
            {"baseToken": {"symbol": "AI"}, "quoteToken": {"symbol": "NVDA"}, "liquidity": {"usd": 99999999}},
            {"baseToken": {"symbol": "ANSEM"}, "quoteToken": {"symbol": "SOL"}, "liquidity": {"usd": 5000}},
        ]}
    monkeypatch.setattr(dex, "_get", get)
    output = dexscreener_pair_search("analyze ANSEM token")
    assert queries == ["ANSEM"]
    assert "ANSEM/SOL" in output
    assert "AI/NVDA" not in output


def test_hype_query_cannot_select_global_profiles():
    registered = []
    DexScreenerProvider().register(SimpleNamespace(register=registered.append))
    tool = next(item for item in registered if item.name == "dexscreener_latest_profiles")
    assert not tool.matches("latest onchain data on HYPE token")
    assert tool.matches("Show latest token profiles on Base")


def test_pool_url_cannot_establish_token_identity():
    assert extract_token_reference(f"Token results: [AI/NVDA](https://dexscreener.com/robinhood/{WALLET})") is None


def test_equity_suggestions_ignore_old_token_history():
    actions = suggested_actions("Analyse APPLE stock", "research", "user: analyze ANSEM token", ["equity_research"])
    assert all("token" not in item.lower() for item in actions)


def test_old_text_cannot_resurrect_workflow():
    result = asyncio.run(graph.resolve_intent_node({"request": "use 25 bps instead", "history": "user: Swap 0.1 SOL on Solana to USDC on Base", "session_context": {}}))
    assert result["intent"] == "general"


def test_superseded_plan_cannot_reach_wallet_signing(monkeypatch):
    async def no_db():
        return None
    monkeypatch.setattr(plans, "get_pg_pool", no_db)
    plan = SimpleNamespace(plan_id="superseded-test", status="pending_confirmation")
    monkeypatch.setitem(plans._plans, plan.plan_id, plan)

    async def check():
        await plans.mark_plan_superseded(plan.plan_id)
        assert plan.status == "superseded"
        try:
            await plans.claim_plan_submission(plan)
        except ValueError:
            return
        raise AssertionError("Superseded plan was claimed")
    asyncio.run(check())


def test_workflow_cancel_and_modify_invalidate_approval():
    old = WorkflowState(status="pending_approval", plan_id="old", fields={"amount": "0.1", "output_token": "ANSEM"})
    result, superseded = apply_event(old, WorkflowEvent.CANCEL)
    assert result is None and superseded == "old"
    result, superseded = apply_event(old, WorkflowEvent.MODIFY, WorkflowState(fields={"amount": "0.2"}))
    assert result.plan_id is None and superseded == "old"
    assert result.fields["output_token"] == "ANSEM"

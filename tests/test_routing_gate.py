"""The routing gate: every case the eval harness knows must route right, in
the suite, on every commit. And the one security vocabulary must be the one
every layer reads, so a phrasing known to one layer is known to all.

Why (2026-09-18, user: "I don't want this to happen again and again"): the
same class of miss kept recurring -- an audit ask reached web search, a
follow-up lost the security tool -- because six hand-kept copies of the
security vocabulary had drifted and nothing ran the eval cases as a test.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from app import tool_catalog
from app.nodes import research as research_mod
from app.provider_registry import get_provider_router
from app.routing import intent_router, lexicon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "routing_eval"))

MINT = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


@pytest.fixture(scope="module", autouse=True)
def _snapshot():
    # An app started earlier in the suite (any TestClient) binds the knowledge
    # tool to its event loop, which is closed by now; warming through that
    # binding fails silently and every knowledge case then misses. Warm on a
    # loop of our own, then hand the binding back.
    from app import db
    from app.knowledge import store as kb_store, tool as kb_tool
    from harness import warm_knowledge_snapshot
    bound = getattr(kb_tool, "_main_loop", None)
    held_store, held_pool = kb_store._store, db._pg_pool      # both may belong to a loop that is already closed
    kb_tool._main_loop, kb_store._store, db._pg_pool = None, None, None
    try:
        result = warm_knowledge_snapshot()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
        if kb_tool.snapshot() is None or len(kb_tool.snapshot()) == 0:
            # No knowledge base in this run (CI has no database): the anchor
            # is built from a fixture of the ingested entities -- names,
            # aliases, types -- so the gate judges the same vocabulary the
            # app resolves against. Refresh with scripts/kb_fixture.py.
            import json as _json
            import time as _time
            from app.knowledge.entities import EntityResolver
            from app.knowledge.models import Entity
            rows = _json.loads((ROOT / "tests" / "fixtures" / "kb_entities.json").read_text())
            kb_tool._resolver_cache = (_time.monotonic() + 3600, EntityResolver([Entity(**r) for r in rows]))
        assert kb_tool.snapshot() is not None and len(kb_tool.snapshot()) > 0, "the knowledge snapshot must be warm for the gate to mean anything"
        yield
    finally:
        kb_tool._main_loop, kb_store._store, db._pg_pool = bound, held_store, held_pool


def test_every_router_case_routes_to_its_expected_tool_and_never_to_a_forbidden_one(capsys):
    from harness import run_router_mode
    cases = json.load(open(ROOT / "scripts" / "routing_eval" / "cases.json"))
    cases = cases if isinstance(cases, list) else cases["cases"]
    report = run_router_mode(cases, k=3, semantic=False)
    capsys.readouterr()   # the harness prints its table; the assertion is what matters here
    misses = [row for row in report["rows"] if row[1] == "MISS"]
    assert not misses, "misrouted cases (id, verdict, top-1, recall, mrr, note): " + "; ".join(str(m) for m in misses)
    assert report["metrics"]["forbidden_at_1"] == 0


# Every way people ask whether a token is safe. Each must be a security ask to
# the Layer-1 rule, the research intercept, the catalog's dimension and the
# Solana security tool's own matcher -- from the same pattern.
SECURITY_PHRASINGS = [
    "is {t} safe", "audit report on {t}", "is {t} audited", "was {t} audited?", "security check for {t}", "security review of {t}",
    "rug check {t}", "is {t} a honeypot", "is {t} legit", "can I sell {t}", "is {t} a scam", "does {t} have mint authority",
    "any warnings on {t}", "is {t} freezable", "is {t} risky", "has {t} been exploited",
]


@pytest.mark.parametrize("phrasing", SECURITY_PHRASINGS)
def test_every_security_phrasing_is_known_to_every_layer(phrasing):
    text = phrasing.format(t="ANSEM")
    assert lexicon.SECURITY.search(text), f"lexicon: {text}"
    assert research_mod._SECURITY_ASK.search(text), f"research intercept: {text}"
    assert "security" in tool_catalog.asked_dimensions(text), f"catalog dimension: {text}"
    with_mint = phrasing.format(t=MINT) + " on solana"
    router = get_provider_router()
    tool = next(t for t in router._tools if t.name == "solana_token_security")
    assert tool.matches(with_mint), f"solana_token_security matcher: {with_mint}"
    plan = [t.name for t in router.plan_across(with_mint, ("token_security", "token_discovery", "market_data"), ("solana",))]
    assert "solana_token_security" in plan, f"plan for {with_mint!r} was {plan}"


@pytest.mark.parametrize("phrasing", ["audit report on {t}", "is {t} audited", "security check for {t}", "rug check {t}", "is {t} a honeypot", "is it audited?"])
def test_security_jargon_reaches_the_token_security_rule(phrasing):
    route = intent_router.route_capabilities(phrasing.format(t="ANSEM"))
    assert route is not None and "token_security" in route.capabilities, phrasing

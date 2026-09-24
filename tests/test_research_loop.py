"""The bounded evidence loop for open research (app/research_loop.py), with
the research tier faked: plan, gap review and example-support answers come
from a script, the tools from a replay router. The brief:
docs/engineering/claude-code-evidence-loop.md."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import evidence_pipeline, research_loop
from app.contracts import plan_by_rules
from tests.test_contract_pipeline import FakeRouter

WEB1 = ("# From the web (dated, with sources)\n**Query**: q1\n\nVenice locks sVVV to mint DIEM, a tradable ERC-20. [1]\n\n"
        "Sources:\n[1] [Venice docs](https://venice.ai/docs) · 2026-09-09")
WEB2 = ("# From the web (dated, with sources)\n**Query**: q2\n\nSynthetix locks SNX to mint sUSD, which trades freely. [1] Aave mints aTokens as deposit receipts. [2]\n\n"
        "Sources:\n[1] [Synthetix docs](https://docs.synthetix.io) · 2026-09-09\n[2] [Aave docs](https://docs.aave.com) · 2026-09-01")


def _script(plan_queries: str, reviews: list[str], support=("Synthetix", "Aave", "none")):
    """A fake research tier: answers by program, in order."""
    state = {"reviews": list(reviews)}

    async def call(program, **kw):
        name = type(program.signature).__name__ if hasattr(program, "signature") else ""
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice VVV/DIEM", question="which other projects lock their own token to mint a tradable second token",
                                   constraints="own token as collateral; second token tradable", required_facts="named projects\nmechanism per project\nfirst-party source",
                                   capabilities="web_discovery", queries=plan_queries)
        if "calls_made" in fields:
            nxt = state["reviews"].pop(0) if state["reviews"] else "stop"
            return SimpleNamespace(missing="named projects" if nxt != "stop" else "none", next_call=nxt, reason="test")
        if "answer" in fields and "evidence" in fields:
            if "not a match" in kw["answer"]:                       # the rewrite relabelled the flagged name
                return SimpleNamespace(supported=support[0], related_but_different="none", not_established="none")
            return SimpleNamespace(supported=support[0], related_but_different=support[1], not_established=support[2])
        return SimpleNamespace(summary="**Taken together**\n\nsummary")
    return call


@pytest.fixture
def loop(monkeypatch):
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    monkeypatch.setattr(evidence_pipeline.settings, "discovery_first_research", True)
    monkeypatch.setattr(evidence_pipeline.settings, "research_loop_enabled", True)
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)

    async def synth(request, cards, trajectory, advice=False, research=False):
        if "state each of them only as such" in request:          # the one rewrite after the support check flagged a name
            return f"**Taken together**\n\nVenice and Synthetix lock their own token. Aave is a receipt-token design, not a match.\n\n---\n\n{cards}"
        return f"**Taken together**\n\nVenice and Synthetix lock their own token; Aave issues receipts.\n\n---\n\n{cards}"

    monkeypatch.setattr(evidence_pipeline.composition, "synthesize", synth)
    monkeypatch.setattr(research_loop.composition, "synthesize", synth)
    return monkeypatch


def _run(monkeypatch, fake_lm, outputs, request="which other projects follow lock collateral → mint a tradable second token"):
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "_call_research_lm", fake_lm)
    monkeypatch.setattr(runtime, "_call_research_loop_lm", fake_lm)
    router = FakeRouter(outputs)
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, request, ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    return out, router


def test_the_loop_answers_open_research_when_the_flag_is_on(loop):
    class Rotating(FakeRouter):
        def invoke(self, name, request, chains=()):
            self.calls.append(name)
            out = WEB1 if len(self.calls) == 1 else WEB2
            return SimpleNamespace(output=out, tool=name, provider="fake")
    fake = _script("web_discovery: projects that lock their own token to mint a tradable token", ["web_discovery: first-party descriptions of lock-to-mint designs adapted from Venice"])
    from app.nodes import runtime
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Rotating({})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert out["pipeline"] == "research_loop"
    assert router.calls == ["perplexity_web_search", "perplexity_web_search"]        # the plan's query, then the gap review's second axis
    record = out["trajectory"]["research_loop"]
    assert len(record["calls"]) == 2 and record["constraints"].startswith("own token")
    assert "Aave is a receipt-token design, not a match" in out["answer"] and "withheld" not in out["answer"]      # rewritten once, then it stands
    assert "Synthetix" in out["answer"] and "Venice docs" in out["answer"]


def test_the_loop_stops_when_the_review_says_stop(loop):
    fake = _script("web_discovery: q1", ["stop"])
    out, router = _run(loop, fake, {"perplexity_web_search": WEB1})
    assert out["pipeline"] == "research_loop" and router.calls == ["perplexity_web_search"]


def test_a_state_read_needs_a_resolved_contract(loop):
    fake = _script("market_state: DIEM pools\nweb_discovery: q1", ["stop"])
    out, router = _run(loop, fake, {"perplexity_web_search": WEB1, "dexscreener_token_pairs": "# DEX pairs\n| DIEM/WETH |"})
    assert "dexscreener_token_pairs" not in router.calls and "birdeye_token_overview" not in router.calls
    assert any("needs a contract address" in s for s in out["trajectory"]["research_loop"]["skipped"])


def test_the_loop_never_exceeds_its_call_budget(loop):
    fake = _script("web_discovery: q1\nweb_discovery: q2", ["web_discovery: q3", "web_discovery: q4", "web_discovery: q5", "web_discovery: q6"])
    out, router = _run(loop, fake, {"perplexity_web_search": WEB1})
    assert len(router.calls) <= research_loop.MAX_CALLS


def test_a_failed_plan_falls_back_to_the_fixed_sequence(loop):
    async def down(program, **kw):
        raise RuntimeError("model down")
    out, router = _run(loop, down, {"perplexity_web_search": WEB1})
    assert out is not None and out.get("pipeline") == "contract" and router.calls == ["perplexity_web_search"]


def test_the_flag_off_keeps_the_fixed_sequence(monkeypatch):
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    monkeypatch.setattr(evidence_pipeline.settings, "discovery_first_research", True)
    monkeypatch.setattr(evidence_pipeline.settings, "research_loop_enabled", False)
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)

    async def synth(request, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nsummary\n\n---\n\n{cards}"

    async def never(program, **kw):
        raise AssertionError("the loop must not run with the flag off")

    monkeypatch.setattr(evidence_pipeline.composition, "synthesize", synth)
    out, router = _run(monkeypatch, never, {"perplexity_web_search": WEB1})
    assert out["pipeline"] == "contract"


def test_tool_for_applies_the_deterministic_boundary():
    contract = plan_by_rules("which other projects follow lock collateral → mint a tradable second token")
    enabled = {"perplexity_web_search", "dexscreener_token_pairs", "mobula_wallet_portfolio", "search_verified_tokens"}
    assert research_loop.tool_for("web_discovery", contract, enabled) == ("perplexity_web_search", "eligible")
    assert research_loop.tool_for("market_state", contract, enabled)[0] is None
    assert research_loop.tool_for("wallet_state", contract, enabled)[0] is None
    assert research_loop.tool_for("token_identity", contract, enabled) == ("search_verified_tokens", "eligible")
    assert research_loop.tool_for("execution_quotes", contract, enabled)[0] is None
    resolved = contract.model_copy(update={"subject": contract.subject.model_copy(update={"kind": "token", "id": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "chain": "solana"})})
    assert research_loop.tool_for("market_state", resolved, enabled | {"birdeye_token_overview"}) == ("birdeye_token_overview", "eligible")
    assert research_loop.tool_for("market_state", resolved, {"coingecko_gainers_losers"})[0] is None      # a ranking tool serves rankings, not research


def test_the_capability_catalog_is_derived_from_the_tool_catalog():
    caps = research_loop.capabilities()
    assert "perplexity_web_search" in caps["web_discovery"]["tools"] and "mobula_token_holders" in caps["on_chain_ownership"]["tools"]
    assert caps["execution_quotes"]["tools"] == []
    assert "web_discovery" in research_loop.catalog_summary()


def test_candidates_are_checked_from_their_pages_and_the_trail_says_why(loop):
    from app.nodes import runtime
    pages = {"https://docs.synthetix.io": "Stakers lock SNX as collateral to mint sUSD, which trades on open markets.",
             "https://docs.aave.com": "Depositors receive aTokens representing their supplied assets."}
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "docs", "text": pages.get(url, ""), "provenance": "page" if url in pages else "unreadable", "fetched_at": "2026-09-24 12:00 UTC"})
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)
    state = {"reviews": ["stop"]}

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="which other projects lock their own token to mint a tradable second token",
                                   constraints="own token as collateral; second token tradable", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "candidates" not in fields and "answer" not in fields:
            return SimpleNamespace(candidates="Synthetix | https://docs.synthetix.io\nAave | https://docs.aave.com")
        if "page" in fields:
            if kw["candidate"] == "Synthetix":
                return SimpleNamespace(verdict="qualifies", conditions_met="own token as collateral; second token tradable", conditions_failed="none", quote="lock SNX as collateral to mint sUSD")
            return SimpleNamespace(verdict="related_but_different", conditions_met="none", conditions_failed="own token as collateral", quote="aTokens representing their supplied assets")
        if "answer" in fields:
            return SimpleNamespace(supported="Synthetix", related_but_different="Aave", not_established="none")
        return SimpleNamespace()

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": WEB2})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    rec = out["trajectory"]["research_loop"]
    assert [v["verdict"] for v in rec["verdicts"]] == ["qualifies", "related_but_different"]
    assert "**Research trail**" in out["answer"] and "Synthetix (https://docs.synthetix.io; page read, 2026-09-24 12:00 UTC): qualifies" in out["answer"]
    assert "Aave (https://docs.aave.com; page read, 2026-09-24 12:00 UTC): related but a different design: own token as collateral" in out["answer"]
    assert {"plan", "retrieval", "inspect", "synthesis", "total"} <= set(rec["timings"])



def test_a_snippet_that_asserts_what_the_page_does_not_is_not_established(loop):
    # The search result says Nova Labs locks its token to mint ASTRA; the page says nothing of the kind.
    from app.nodes import runtime
    snippet = ("# From the web (dated, with sources)\n**Query**: q\n\nNova Labs locks NOVA to mint tradable ASTRA credits. [1] Venice locks sVVV to mint DIEM. [2]\n\n"
               "Sources:\n[1] [Nova Labs blog](https://novalabs.example/blog) · 2026-09-10\n[2] [Venice docs](https://venice.ai/docs) · 2026-09-09")
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "Nova Labs", "text": "Nova Labs announces ASTRA, a token for its compute marketplace. Details of the token model will follow.", "provenance": "page", "fetched_at": "2026-09-24 12:00 UTC"}
                 if "novalabs" in url else {"url": url, "title": "", "text": "", "provenance": "unreadable", "fetched_at": ""})
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="which other projects lock their own token to mint a tradable second token",
                                   constraints="own token as collateral; second token tradable", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            return SimpleNamespace(candidates="Nova Labs | https://novalabs.example/blog")
        if "page" in fields:
            assert "compute marketplace" in kw["page"]                 # the page, not the snippet, is what is checked
            return SimpleNamespace(verdict="not_established", conditions_met="none", conditions_failed="none", quote="none")
        if "answer" in fields:
            return SimpleNamespace(supported="none", related_but_different="none", not_established="Nova Labs")
        return SimpleNamespace()

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": snippet})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert out["trajectory"]["research_loop"]["verdicts"][0]["verdict"] == "not_established"
    assert "I withheld the written summary" in out["answer"] and "Nova Labs" in out["answer"]      # the claim never stands, the trail shows why
    assert "Nova Labs (https://novalabs.example/blog; page read" in out["answer"] and "not established by its page" in out["answer"]


def test_an_unreadable_page_is_a_lead_not_evidence(loop):
    from app.nodes import runtime
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "", "text": "", "provenance": "unreadable", "fetched_at": "2026-09-24 12:00 UTC"})
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="own token as collateral", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            return SimpleNamespace(candidates="Synthetix | https://docs.synthetix.io")
        if "page" in fields:
            raise AssertionError("an unreadable page is never checked")
        if "answer" in fields:
            return SimpleNamespace(supported="none", related_but_different="none", not_established="Synthetix")
        return SimpleNamespace()

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": WEB2})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    v = out["trajectory"]["research_loop"]["verdicts"][0]
    assert v["verdict"] == "not_established" and v["provenance"] == "unreadable"
    assert "page unreadable" in out["answer"] and "search lead" in out["answer"] and "I withheld the written summary" in out["answer"]


def test_an_unsettled_candidate_gets_one_first_party_read(loop):
    from app.nodes import runtime
    pages = {"https://www.gate.com/blog/synthetix": "Synthetix is a derivatives protocol. It has a token called SNX.",
             "https://docs.synthetix.io/staking": "Stakers lock SNX as collateral to mint sUSD, an ERC-20 that trades on open markets; burning sUSD releases the SNX."}
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "t", "text": pages.get(url, ""), "provenance": "page" if url in pages else "unreadable", "fetched_at": "replay"})
    loop.setattr(research_loop, "first_party_url", lambda name, conditions: "https://docs.synthetix.io/staking" if name == "Synthetix" else None)
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)
    cited_blog = WEB2 + "\n[3] [Synthetix on Gate](https://www.gate.com/blog/synthetix) · 2026-09-02"

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="own token as collateral; second token tradable", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            return SimpleNamespace(candidates="Synthetix | https://www.gate.com/blog/synthetix")
        if "page" in fields:
            ok = "mint sUSD" in kw["page"]
            return SimpleNamespace(verdict="qualifies" if ok else "not_established", conditions_met="own token as collateral; second token tradable" if ok else "none", conditions_failed="none", quote="lock SNX as collateral to mint sUSD" if ok else "none")
        if "answer" in fields:
            return SimpleNamespace(supported="Synthetix", related_but_different="none", not_established="none")
        return SimpleNamespace()

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": cited_blog})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    v = out["trajectory"]["research_loop"]["verdicts"][0]
    assert v["verdict"] == "qualifies" and v["url"] == "https://docs.synthetix.io/staking" and "first-party page found by a follow-up search" in v["note"]
    assert "Synthetix (https://docs.synthetix.io/staking; page read, replay): qualifies" in out["answer"]


def test_a_candidate_named_without_a_url_gets_its_page_from_a_search(loop):
    from app.nodes import runtime
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "t", "text": "Stakers lock SNX as collateral to mint sUSD, which trades on open markets.", "provenance": "page", "fetched_at": "replay"})
    loop.setattr(research_loop, "first_party_url", lambda name, conditions: "https://docs.synthetix.io/staking" if name == "Synthetix" else None)
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="own token as collateral; second token tradable", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            return SimpleNamespace(candidates="- Synthetix\n- none")
        if "page" in fields:
            return SimpleNamespace(verdict="qualifies", conditions_met="own token as collateral; second token tradable", conditions_failed="none", quote="lock SNX as collateral to mint sUSD")
        if "answer" in fields:
            return SimpleNamespace(supported="Synthetix", related_but_different="none", not_established="none")
        return SimpleNamespace()

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": WEB2})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    v = out["trajectory"]["research_loop"]["verdicts"][0]
    assert v["name"] == "Synthetix" and v["url"] == "https://docs.synthetix.io/staking" and v["verdict"] == "qualifies"



def test_the_reader_refuses_private_and_local_destinations():
    from app import url_reader
    for bad in ("http://127.0.0.1/", "http://localhost:8000/x", "http://10.0.0.5/admin", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd", "http://[::1]/"):
        assert not url_reader.public_destination(bad), bad
    assert url_reader.public_destination("https://docs.synthetix.io/staking")


def test_a_fetch_connects_to_the_address_that_passed_the_check(monkeypatch):
    from app import url_reader
    connected = []

    def resolve(url):
        return ("public.example", 443, "203.0.113.7", "https") if "public.example" in url else None

    def get(url, host, port, address, scheme):
        connected.append((host, address))
        return 200, {"content-type": "text/html; charset=utf-8"}, "<html><title>t</title><body>" + "<p>Stakers lock SNX as collateral to mint sUSD.</p>" * 40 + "</body></html>"

    monkeypatch.setattr(url_reader, "resolve_public", resolve)
    monkeypatch.setattr(url_reader, "_get_pinned", get)
    got = url_reader.fetch("https://public.example/page")
    assert got and got[0] == "t" and "lock SNX" in got[1]
    assert connected == [("public.example", "203.0.113.7")]          # the request went to the checked address, no second lookup


def test_a_redirect_to_a_private_destination_is_refused(monkeypatch):
    from app import url_reader
    seen = []

    def resolve(url):
        return ("public.example", 443, "203.0.113.7", "https") if "public.example" in url else None

    def get(url, host, port, address, scheme):
        seen.append(url)
        return 302, {"location": "http://127.0.0.1/secret"}, ""

    monkeypatch.setattr(url_reader, "resolve_public", resolve)
    monkeypatch.setattr(url_reader, "_get_pinned", get)
    assert url_reader.fetch("https://public.example/page") is None
    assert seen == ["https://public.example/page"]                  # the private hop was never requested


def test_the_reader_fallback_never_gets_a_rejected_url(monkeypatch):
    from app import perplexity_tools, url_reader
    monkeypatch.setattr(url_reader, "resolve_public", lambda url: None)
    monkeypatch.setattr(url_reader, "fetch", lambda url: None)
    monkeypatch.setattr(perplexity_tools, "perplexity_available", lambda: True)
    monkeypatch.setattr(perplexity_tools, "perplexity_fetch_url", lambda url: (_ for _ in ()).throw(AssertionError("fallback must not run")))
    page = research_loop.read_page("http://169.254.169.254/latest")
    assert page["provenance"] == "unreadable" and page["note"] == "not a public destination"


def test_only_cited_urls_are_read_and_the_page_budget_holds(loop):
    from app.nodes import runtime
    reads = []
    loop.setattr(research_loop, "read_page", lambda url: (reads.append(url) or {"url": url, "title": "t", "text": "Depositors receive aTokens representing their supplied assets.", "provenance": "page", "fetched_at": "replay"}))
    loop.setattr(research_loop, "first_party_url", lambda name, conditions: None)
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 2, raising=False)

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="own token as collateral", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            # one cited URL, one URL the model made up, and a name with none
            return SimpleNamespace(candidates="Aave | https://docs.aave.com\nEvil | http://169.254.169.254/latest\nSynthetix | https://docs.synthetix.io\nNova | none")
        if "page" in fields:
            return SimpleNamespace(verdict="not_established", conditions_met="none", conditions_failed="none", quote="none")
        if "answer" in fields:
            return SimpleNamespace(supported="none", related_but_different="none", not_established="none")
        return SimpleNamespace()

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": WEB2})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert "http://169.254.169.254/latest" not in reads                    # a URL the model wrote is never fetched
    assert len(reads) <= 2 and all(u in ("https://docs.aave.com", "https://docs.synthetix.io") for u in reads)


def test_a_failed_support_check_withholds_the_summary(loop):
    from app.nodes import runtime
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 0, raising=False)

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="none", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "answer" in fields:
            raise RuntimeError("model down")
        return SimpleNamespace()

    out, router = _run(loop, fake, {"perplexity_web_search": WEB2})
    assert out["pipeline"] == "research_loop" and "I withheld the written summary: the example check could not run" in out["answer"]


def test_the_conversation_lock_outlives_a_research_turn(monkeypatch):
    from app import sessions
    seen = {}

    class Lock:
        async def acquire(self):
            return True
        async def release(self):
            return None

    class Redis:
        def lock(self, name, timeout, blocking_timeout):
            seen["timeout"] = timeout
            return Lock()

    async def redis():
        return Redis()

    monkeypatch.setattr(sessions, "get_redis", redis)
    monkeypatch.setattr(sessions.settings, "chat_execution_timeout_seconds", 120.0)
    monkeypatch.setattr(sessions.settings, "research_loop_timeout_seconds", 240.0, raising=False)
    monkeypatch.setattr(sessions.settings, "research_loop_enabled", True, raising=False)
    asyncio.run(sessions.acquire_session_turn("s1"))
    assert seen["timeout"] == 255
    monkeypatch.setattr(sessions.settings, "research_loop_enabled", False, raising=False)
    asyncio.run(sessions.acquire_session_turn("s2"))
    assert seen["timeout"] == 135



def test_a_page_verdict_gates_the_summary_whatever_the_snippet_said(loop):
    # The search snippet says Nova Labs qualifies; its page did not establish it; the support model, reading snippets, sees no problem.
    from app.nodes import runtime
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "Nova", "text": "Nova Labs announces ASTRA. Details will follow.", "provenance": "page", "fetched_at": "replay"})
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)
    snippet = ("# From the web (dated, with sources)\n**Query**: q\n\nNova Labs locks NOVA to mint tradable ASTRA credits. [1]\n\n"
               "Sources:\n[1] [Nova Labs blog](https://novalabs.example/blog) · 2026-09-10")

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="own token as collateral; second token tradable", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            return SimpleNamespace(candidates="Nova Labs | https://novalabs.example/blog")
        if "page" in fields:
            return SimpleNamespace(verdict="not_established", conditions_met="none", conditions_failed="none", quote="none")
        if "answer" in fields:
            return SimpleNamespace(supported="Nova Labs", related_but_different="none", not_established="none")      # the snippet convinced it
        return SimpleNamespace()

    async def synth(request, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nNova Labs locks NOVA to mint tradable ASTRA, a match.\n\n---\n\n{cards}"     # the rewrite does not relabel it either

    loop.setattr(evidence_pipeline.composition, "synthesize", synth)
    loop.setattr(research_loop.composition, "synthesize", synth)

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": snippet})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert "I withheld the written summary" in out["answer"] and "Nova Labs" in out["answer"]
    assert research_loop.unlabelled_names("Nova Labs is related but a different design.", [{"name": "Nova Labs", "verdict": "not_established"}]) == []
    assert any(c.startswith("page_read (url_reader): https://novalabs.example/blog") for c in out["trajectory"]["research_loop"]["calls"])


def test_a_failed_candidate_listing_withholds_the_summary(loop):
    from app.nodes import runtime
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="none", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            raise RuntimeError("model down")
        raise AssertionError("nothing runs after the listing fails")

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": WEB2})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert "I withheld the written summary: the candidate check could not run" in out["answer"]
    assert out["trajectory"]["research_loop"]["withheld"] == "candidate check failed"

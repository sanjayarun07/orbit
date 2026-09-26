"""The bounded evidence loop for open research (first slice, behind
`settings.research_loop_enabled`).

The pipeline's fixed sequence -- choose up to three tools, one repair --
cannot notice that the fourth turn of a research conversation needs a
different search from the third, or that a required fact is still missing
after the first read. This loop makes the contract, the coverage rules and
the fact gate one decision loop:

1. The research tier plans the evidence from the resolved request (the
   objective under it included): the subject, the actual question, the
   constraints, the facts a supported answer needs, and a shortlist of
   capabilities with the first queries.
2. Capabilities are groups of tools by the job they do (`capabilities()`,
   derived from the tool catalog, which stays the source of truth for
   coverage and freshness). The model shortlists capabilities; the
   deterministic boundary decides which tool, if any, may run: a state tool
   needs a resolved contract, the discovery tools take a query.
3. After each round the tier reviews the gaps -- which required fact is
   still missing, which capability can supply it -- and either asks for one
   more call or stops. Bounded by rounds and calls.
4. The synthesis is written on the research brief, then every named example
   or relationship is checked against the cited evidence: supported, related
   but a different design, or not established -- labelled, never passed as
   sourced (the VVV/DIEM case: a project qualifies only when a source shows
   the lock-collateral-to-mint-a-tradable-token mechanism).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlsplit

import dspy

from app import composition, contracts, fact_gate, research_ledger, streaming, tool_catalog, x_news
from app.settings import settings

logger = logging.getLogger(__name__)

MAX_ROUNDS = 2          # the plan's round, then one gap review and its call (a third round put turns past 170 s live, 2026-09-24)
MAX_CALLS = 5           # provider calls per turn, whatever the model asks for
FACT_LANE_PAGES = 4     # cited pages read for an ordinary question (the fact lane): provenance for its claims, not a candidate hunt
MAX_INITIAL = 4         # distinct evidence questions from the plan, run concurrently
MAX_MODEL_CALLS = 18    # includes inspection, audits, synthesis support and one repair


class TurnBudget:
    """One deadline and call ledger shared by every stage of a research turn."""

    def __init__(self, seconds: float, model_limit: int = MAX_MODEL_CALLS):
        self.deadline = time.monotonic() + seconds
        self.model_limit = model_limit
        self.calls: dict[str, int] = {"model": 0, "provider": 0, "page": 0, "synthesis": 0}
        self.exhausted = ""

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def can_run(self, reserve: float = 0) -> bool:
        return self.remaining() > reserve

    async def call(self, kind: str, awaitable, timeout: float, reserve: float = 0):
        limits = {"model": self.model_limit, "provider": 7, "page": 6, "synthesis": 2}
        if self.calls.get(kind, 0) >= limits[kind]:
            self.exhausted = f"{kind} calls"
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError(f"research {kind}-call budget exhausted")
        available = self.remaining() - reserve
        if available <= 0:
            self.exhausted = "deadline"
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError("research deadline exhausted")
        self.calls[kind] = self.calls.get(kind, 0) + 1
        try:
            return await asyncio.wait_for(awaitable, timeout=min(timeout, available))
        except asyncio.TimeoutError:
            if self.remaining() <= reserve + 0.1:
                self.exhausted = "deadline"
            raise

    def record(self) -> dict:
        return {"calls": dict(self.calls), "remaining_seconds": round(self.remaining(), 1), "exhausted": self.exhausted}


class BudgetedRuntime:
    def __init__(self, runtime, budget: TurnBudget):
        self.runtime = runtime
        self.budget = budget

    async def _call_research_loop_lm(self, program, **kwargs):
        return await self.budget.call("model", self.runtime._call_research_loop_lm(program, **kwargs), 45, reserve=12)

# What each capability group does and the inputs it needs: group metadata.
# Which tools belong to a group, and their coverage and freshness, come from
# the tool catalog (`CONTRACT_COVERAGE[tool]["capability"]`), the one source
# of truth; nothing here names a tool.
CAPABILITY_JOBS: dict[str, tuple[str, list[str], str]] = {
    "web_discovery": ("dated, source-linked reads of the open web: mechanisms, announcements, examples, first-party posts", ["query"], ""),
    "finance_discovery": ("equities, filings, exchange listings and tokenized stocks, from finance sources", ["query"], ""),
    "knowledge": ("indexed protocol documentation, governance forums and incident records", ["query"], "registry protocols only; nothing on tokens outside it"),
    "token_identity": ("resolve a symbol to a verified contract and chain", ["symbol"], ""),
    "market_state": ("price, volume, liquidity, pools, rankings and yields as they stand now", ["address", "chain"], "one contract's state needs its address"),
    "on_chain_ownership": ("the largest holders of one contract and their share", ["address", "chain"], ""),
    "wallet_state": ("what a wallet holds now", ["wallet"], "not part of research"),
    "execution_quotes": ("a swap or exit quote at a size", ["amount", "pair"], "not part of research"),
}


def capabilities() -> dict[str, dict]:
    """The capability catalog, derived from the tool catalog at call time."""
    out: dict[str, dict] = {name: {"job": job, "inputs": inputs, "limits": limits, "tools": []} for name, (job, inputs, limits) in CAPABILITY_JOBS.items()}
    for tool, cov in tool_catalog.CONTRACT_COVERAGE.items():
        group = cov.get("capability")
        if group in out:
            out[group]["tools"].append(tool)
    return out


def _tools_of(capability: str) -> list[str]:
    return [t for t, c in tool_catalog.CONTRACT_COVERAGE.items() if c.get("capability") == capability]


def enabled() -> bool:
    return bool(getattr(settings, "research_loop_enabled", False))


def catalog_summary() -> str:
    """The catalog as the planner reads it: one line per capability with the
    tools' declared coverage and freshness."""
    lines = []
    for name, cap in capabilities().items():
        tools = []
        for tool in cap["tools"]:
            cov = tool_catalog.CONTRACT_COVERAGE.get(tool) or {}
            spec = tool_catalog.TOOL_SPECS.get(tool)
            fresh = cov.get("fresh")
            scope = cov.get("scope")
            tools.append(tool + (f" (scope {scope}, fresh {fresh}s)" if cov else (f" ({spec.freshness})" if spec and getattr(spec, "freshness", None) else "")))
        lines.append(f"- {name}: {cap['job']}. Needs: {', '.join(cap['inputs']) or 'nothing'}. Tools: {', '.join(tools) or 'none'}."
                     + (f" Limits: {cap['limits']}." if cap.get("limits") else ""))
    return "\n".join(lines)


class EvidencePlan(dspy.Signature):
    """Plan one research turn from the user's question and the conversation
    notes. State the actual question, its subject, the conditions a correct
    answer must meet, and three to six facts needed to answer it. Preserve
    every user constraint and distinguish event time from publication time,
    venue from ecosystem, and exact mechanism from thematic similarity.
    For a comparison, spell out the reference design's necessary properties
    as conditions on proposed matches rather than accepting projects that
    merely share keywords. Do NOT make verification of the user's reference
    example a condition on a different candidate. Conditions are qualifying
    facts about the proposed match, not instructions to collect sources,
    quotes, publication dates or launch dates. Add time or date as a condition
    only when the user explicitly requests a period or event date. Shortlist
    capabilities by their declared job. Give two targeted first discovery
    queries as `capability: query` lines. For news and market-impact questions,
    separate what the original announcement or data release establishes from
    what a dated market observation shows. Neither a contemporaneous price move
    nor a plausible mechanism alone proves the event caused the move. A follow-up changes the facts to
    verify while keeping its resolved referents. If a state read needs an
    unresolved contract address, put that gap in constraints; do not guess."""

    request: str = dspy.InputField()
    catalog: str = dspy.InputField()
    subject: str = dspy.OutputField()
    question: str = dspy.OutputField()
    constraints: str = dspy.OutputField(desc="only the necessary qualifying conditions for a proposed answer; semicolon-separated, or 'none'; exclude evidence tasks and unrequested dates")
    required_facts: str = dspy.OutputField(desc="one per line")
    capabilities: str = dspy.OutputField(desc="comma-separated capability names from the catalog")
    queries: str = dspy.OutputField(desc='one per line, "capability: query"')


class ExpandedEvidencePlan(dspy.Signature):
    """Plan one research turn from the user's question and conversation notes.
    State the subject, actual question, necessary qualifying conditions and
    three to six required facts. Preserve user constraints and distinguish
    event time from publication time, venue from ecosystem, and exact mechanism
    from thematic similarity. For comparisons, state the reference design's
    necessary properties as conditions on proposed matches. Do not require
    verification of the reference example as a condition on another candidate.
    Conditions describe qualifying facts, not evidence tasks or unrequested
    dates. Shortlist capabilities by their declared job. Return one to four
    independently answerable discovery questions in sub_queries JSON. Each has
    facet, capability, query, topic ('general' or 'news'), days (a positive
    number only for news), and domains (known first-party domains or an explicit
    user restriction, otherwise []). Do not paraphrase the same question across
    facets or invent candidates and domains. One or two facets suffice for a
    simple question. Also supply legacy `capability: query` lines as fallback.
    For news, separate the original event from dated market observations; a
    price move alone does not prove causation. A follow-up keeps resolved
    referents while changing the facts to verify. Never guess an unresolved
    contract address."""

    request: str = dspy.InputField()
    catalog: str = dspy.InputField()
    subject: str = dspy.OutputField()
    question: str = dspy.OutputField()
    constraints: str = dspy.OutputField(desc="only necessary qualifying conditions; semicolon-separated or 'none'; exclude evidence tasks and unrequested dates")
    required_facts: str = dspy.OutputField(desc="one per line")
    capabilities: str = dspy.OutputField(desc="comma-separated capability names from the catalog")
    sub_queries: str = dspy.OutputField(desc='JSON array of up to four objects: {"facet":"distinct fact to establish","capability":"web_discovery","query":"specific search","topic":"general|news","days":3|null,"domains":[]}')
    queries: str = dspy.OutputField(desc='one per line, "capability: query"')


class GapReview(dspy.Signature):
    """After reading the source pages, decide the next call from the verified
    evidence state. Search prose is a lead, never proof. A candidate labelled
    `not_established` still has a gap even if the search card sounds certain.
    Target one missing condition or required fact with a different query, or
    stop when the cited pages settle the question or no capability can close
    the gap. For a discovery query, emphasize the *rarest relationship* in
    the requirements (who owns the collateral, what is minted, where the
    minted asset can move) using natural synonyms for its mechanism. Search
    for the relationship across original documentation, not broad topical
    neighbours. Do not repeat a query or restrict it to a site unless the
    user explicitly requested that site. Never put a hoped-for candidate
    name into the query unless an inspected page already supports part of
    that candidate's relationship."""

    question: str = dspy.InputField()
    required_facts: str = dspy.InputField()
    evidence: str = dspy.InputField()
    calls_made: str = dspy.InputField()
    catalog: str = dspy.InputField()
    missing: str = dspy.OutputField(desc="one per line, or 'none'")
    next_call: str = dspy.OutputField(desc='"capability: query" or "stop"')
    reason: str = dspy.OutputField()


class ExampleSupport(dspy.Signature):
    """Check material examples and relationships in the answer against the
    inspected source-page verdicts. A search snippet is a lead, not support.
    A named entity, event or project is supported only when its page verdict
    establishes the exact claim; otherwise mark it related but different or
    not established. Do not treat an answer's citation marker as proof that
    the linked page says the same thing. Output each list comma-separated,
    or `none`."""

    question: str = dspy.InputField()
    answer: str = dspy.InputField()
    evidence: str = dspy.InputField()
    supported: str = dspy.OutputField()
    related_but_different: str = dspy.OutputField()
    not_established: str = dspy.OutputField()


class ClaimSupport(dspy.Signature):
    """Audit every material factual statement in the written summary against
    the directly fetched, verbatim source-page passages. A plausible inference,
    a search snippet, a project-level verdict, or a citation marker is not
    enough. List each unsupported or overstated claim on its own line, copied
    from the summary. An explicitly labelled unknown is not an assertion.
    Return `none` only when every asserted claim follows from the passages."""

    question: str = dspy.InputField()
    summary: str = dspy.InputField()
    evidence: str = dspy.InputField()
    unsupported_claims: str = dspy.OutputField(desc="one unsupported summary claim per line, or none")


class CandidateCheck(dspy.Signature):
    """Check the named claim or candidate against the fetched pages together.
    Apply the supplied qualifying conditions literally, including identity,
    mechanism, scope, amount and event time when asked. Do not invent extra
    conditions from background search text, the reference example or an
    unrequested documentation date. `qualifies` means
    the page establishes the claim and every required condition;
    `related_but_different` means it explicitly shows a different answer;
    `not_established` means the pages do not settle it. Put every condition
    that is missing or contradicted in `conditions_failed`, so the next
    search can target that gap. Return a short VERBATIM primary quote and
    additional VERBATIM supporting passages, one per line, covering the
    independent conditions the pages establish. Use exact text from the
    fetched pages, not a paraphrase. Map every established condition to an
    exact passage in evidence_map, including partial matches. A qualifying
    verdict must map ALL semicolon-separated conditions, numbered from 1.
    If any condition cannot be mapped, use not_established and name the gap.
    Treat related properties as separate conditions: exchange trading proves
    tradability, but wallet-to-wallet transferability needs a passage about
    transfers between independent addresses or an actual token transfer
    interface. Sending only to one protocol-controlled deposit address does
    not establish unrestricted transfers. Likewise,
    minting does not itself prove redemption, and collateral does not itself
    prove who owns or controls the minted token.
    Merely mentioning a name, a publication date without the event time,
    or a search snippet citing the page does not establish the claim."""

    question: str = dspy.InputField()
    conditions: str = dspy.InputField(desc="the conditions a match must meet, semicolon-separated")
    candidate: str = dspy.InputField()
    page: str = dspy.InputField(desc="the fetched pages' text, with source URLs")
    verdict: str = dspy.OutputField(desc='"qualifies" | "related_but_different" | "not_established"')
    conditions_met: str = dspy.OutputField(desc="semicolon-separated, or 'none'")
    conditions_failed: str = dspy.OutputField(desc="semicolon-separated, or 'none'")
    quote: str = dspy.OutputField(desc="the sentence that settles it, or 'none'")
    supporting_passages: str = dspy.OutputField(desc="one exact page passage per line for the other established conditions, or 'none'")
    evidence_map: str = dspy.OutputField(desc='JSON array [{"condition": 1, "url": "source URL", "quote": "exact page text"}, ...] for every established condition; [] only when none is established')


class ConditionSupportAudit(dspy.Signature):
    """Independently check whether each quoted passage logically proves its
    assigned condition. Use ONLY that condition's quoted page text, not the
    search result or project-level reputation. A quote about exchange trading
    or sending to one protocol-controlled deposit address does not prove
    arbitrary wallet transferability; minting does not prove redemption;
    a current page does not by itself prove historical behavior. List the
    one-based numbers of every unsupported condition, or `none`."""

    conditions: str = dspy.InputField(desc="semicolon-separated conditions, numbered from 1")
    evidence_map: str = dspy.InputField(desc="JSON: exact quote and URL assigned to each condition")
    unsupported_conditions: str = dspy.OutputField(desc="comma-separated one-based numbers, or none")


class CandidateList(dspy.Signature):
    """From the evidence cards, list the material entities, events, examples
    or proposed answers that must be checked for this question. List each
    entity only once under its canonical name: several claims or source
    pages about Synthetix are one Synthetix candidate, not separate rows.
    Include
    near-matches and uncertain leads. Order candidates by likelihood of
    satisfying every requested condition and by strength of first-party
    evidence; place known different designs and speculative names later.
    Use the URL actually cited for each,
    one per line as `name or short claim | url`. Inspection decides whether
    it qualifies. Exclude only a reference example used solely to define a
    comparison. Output `none` only if no specific claim or candidate appears
    in the cards."""

    question: str = dspy.InputField()
    evidence: str = dspy.InputField()
    candidates: str = dspy.OutputField(desc='one per line, "name | url", or "none"')


class NewsSourcePick(dspy.Signature):
    """Choose source pages for two separate facts about a news headline.
    For the event, prefer the original announcement, filing, release, or
    official data on a directly readable HTML page when available. For the market response, choose a dated report with actual
    observed prices or volumes, not commentary about what might happen.
    A source may cover only one side. Choose `event_url` from the
    original-event search and `market_url` from the market-response search.
    Copy URLs exactly from the supplied list; return `none` when that side
    has no relevant page."""

    headline: str = dspy.InputField()
    sources: str = dspy.InputField(desc="URLs, titles and dates discovered in two independent searches")
    event_url: str = dspy.OutputField(desc="one exact URL from sources, or none")
    market_url: str = dspy.OutputField(desc="one exact URL from sources, or none")


class NewsPassages(dspy.Signature):
    """Extract up to four exact, short passages from one directly fetched
    source page for the requested facet. Copy each passage verbatim on its
    own line. For `event`, capture what happened, the event date, who is
    covered, and whether it is a proposal or final action. For `market`,
    capture time-stamped observed prices, volumes or flows, with the asset or
    instrument named in the passage rather than a standalone pronoun, and any distinct
    driver the page actually reports. Exclude bare publication timestamps,
    unrelated historical statistics and market facts from a different region
    or period. Do not turn the page's inference into an observed fact. Return
    `none` when the page does not cover the facet."""

    headline: str = dspy.InputField()
    facet: str = dspy.InputField(desc="event or market")
    page: str = dspy.InputField()
    passages: str = dspy.OutputField(desc="one verbatim page passage per line, or none")


class FollowupSource(dspy.Signature):
    """Pick the most relevant already-cited source page for the missing
    condition. Prefer a page about the exact missing fact over a generic
    home or token page. A page proving another condition does not close this
    gap. The short search context is only for choosing which
    page to read; it is never evidence for the verdict. Return only one URL
    copied from `sources`, or `none`.
    The caller validates the URL and reads the page before using any claim."""

    candidate: str = dspy.InputField()
    missing_condition: str = dspy.InputField()
    sources: str = dspy.InputField(desc="numbered titles, URLs and nearby search context cited by retrieval")
    url: str = dspy.OutputField(desc="one URL exactly as supplied, or none")


_plan_program = dspy.Predict(EvidencePlan)
_expanded_plan_program = dspy.Predict(ExpandedEvidencePlan)
_candidates_program = dspy.Predict(CandidateList)
_news_source_program = dspy.Predict(NewsSourcePick)
_news_passages_program = dspy.Predict(NewsPassages)
_check_program = dspy.Predict(CandidateCheck)
_condition_audit_program = dspy.Predict(ConditionSupportAudit)
_followup_program = dspy.Predict(FollowupSource)
_review_program = dspy.Predict(GapReview)
_support_program = dspy.Predict(ExampleSupport)
_claim_support_program = dspy.Predict(ClaimSupport)


def _parse_calls(text: str) -> list[tuple[str, str]]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        if not line or line.lower() == "stop":
            continue
        m = re.match(r"([a-z_]+)\s*:\s*(.+)$", line, re.I)
        if m and m.group(1).lower() in CAPABILITY_JOBS:
            out.append((m.group(1).lower(), m.group(2).strip().strip('"')))
    return out


@dataclass(frozen=True)
class DiscoveryCall:
    capability: str
    query: str
    facet: str = ""
    topic: str = "general"
    days: int | None = None
    domains: tuple[str, ...] = ()


def _expanded_calls(plan, contract: contracts.QuestionContract, request: str) -> list[DiscoveryCall]:
    """Accept bounded typed discovery, retaining the old planner as fallback."""
    if _news_headline(contract, request):
        return [DiscoveryCall(cap, query, facet="event" if i == 0 else "market", topic="news", days=7)
                for i, (cap, query) in enumerate(_opening_calls(plan, contract, request))]
    if not settings.research_query_expansion_enabled:
        return [DiscoveryCall(cap, query) for cap, query in _opening_calls(plan, contract, request)[:2]]
    try:
        raw = json.loads(getattr(plan, "sub_queries", "") or "")
        if isinstance(raw, dict):
            raw = raw.get("sub_queries")
    except (TypeError, ValueError):
        raw = None
    if not isinstance(raw, list):
        raw = []
    calls: list[DiscoveryCall] = []
    seen: set[tuple[str, str]] = set()
    seen_facets: set[tuple[str, str]] = set()
    for item in raw[:MAX_INITIAL]:
        if not isinstance(item, dict):
            continue
        capability = str(item.get("capability") or "").strip().lower()
        query = re.sub(r"\s+", " ", str(item.get("query") or "")).strip()[:300]
        if capability not in CAPABILITY_JOBS or len(query) < 8:
            continue
        key = capability, query.casefold()
        facet = str(item.get("facet") or "").strip()[:100]
        facet_key = capability, facet.casefold()
        if key in seen or (facet and facet_key in seen_facets):
            continue
        seen.add(key)
        if facet:
            seen_facets.add(facet_key)
        topic = item.get("topic") if item.get("topic") in ("general", "news") else "general"
        days = item.get("days")
        days = max(1, min(30, days)) if topic == "news" and type(days) is int else (7 if topic == "news" else None)
        domains = tuple(dict.fromkeys(str(d).lower().removeprefix("www.") for d in (item.get("domains") or [])[:2]
                                      if isinstance(d, str) and re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,24}", d.lower().removeprefix("www."))))
        calls.append(DiscoveryCall(capability, query, facet, topic, days, domains))
    if calls:
        return calls
    return [DiscoveryCall(cap, query) for cap, query in _opening_calls(plan, contract, request)]


def _opening_calls(plan, contract: contracts.QuestionContract, request: str) -> list[tuple[str, str]]:
    """Open an event investigation on independent source and reaction axes.

    A Home news headline is an open-research question about one event. The
    model may propose only a topical summary search; that cannot establish
    both the announcement and the market response. These two broad searches
    discover leads in parallel. Direct page reads determine what is supported;
    a missing facet gets a bounded fallback to another lead on the same axis.
    Other research keeps the model's planned calls and its existing budget.
    """
    planned = _parse_calls(getattr(plan, "queries", ""))[:MAX_INITIAL]
    headline = _news_headline(contract, request)
    if not headline:
        return planned or [("web_discovery", (getattr(plan, "question", "") or request).strip())]
    return [
        ("web_discovery", f"{headline} original announcement official release or primary data exact event date and terms"),
        ("web_discovery", f"{headline} dated market reaction observed prices volumes affected assets and alternative drivers"),
    ]


def _news_headline(contract: contracts.QuestionContract, request: str) -> str:
    """The Home news event, excluding ordinary research about a named topic."""
    if contract.kind != contracts.OPEN_RESEARCH_KIND:
        return ""
    body, notes = composition.split_notes(request)
    tap = re.match(r"^\s*What does this mean for (?:the market|memecoins):\s*(.+?)\s*$", body or "", re.I | re.S)
    if tap:
        return tap.group(1).strip()
    note = re.search(r'Home news headline "([^"]+)"', notes or "")
    return note.group(1).strip() if note else ""


def _attributed_author(request: str) -> str:
    """An explanation explicitly scoped to what a named social author said."""
    body, _ = composition.split_notes(request)
    if not re.search(r"\b(?:according to|mentioned by|said by|posted by|wrote|what did|what does)\b", body, re.I):
        return ""
    match = re.search(r"(?<!\w)@([A-Za-z0-9_]{1,15})\b", body)
    return match.group(1) if match else ""


def _attributed_passage(page_text: str, request: str, author: str) -> str:
    """Keep the closest topical passage from a long profile/feed page."""
    words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", request)
             if w.lower() not in {"what", "does", "nature", "mentioned", "according", "posted", "exposure", author.lower()}}
    text = re.sub(r"\s+", " ", page_text or "").strip()
    if not text or not words:
        return ""
    best, score = "", 0
    for word in words:
        for match in list(re.finditer(rf"\b{re.escape(word)}\b", text, re.I))[:15]:
            start = max(0, match.start() - 700)
            excerpt = text[start:match.end() + 1800]
            hits = sum(bool(re.search(rf"\b{re.escape(term)}\b", excerpt, re.I)) for term in words)
            if hits > score:
                best, score = excerpt, hits
    return best if score >= min(2, len(words)) else ""


async def _run_attributed(request: str, author: str) -> dict:
    """A bounded source read for a named author's claim, not a candidate audit."""
    from app import perplexity_tools

    unavailable = (f"I could not verify what @{author} said from a readable source right now. "
                   "Please share the post link or retry; I won't infer the claim from the ticker.")
    streaming.emit("status", text=f"Finding @{author}'s original claim")
    try:
        found = await asyncio.wait_for(asyncio.to_thread(
            perplexity_tools.perplexity_search_with_sources,
            f"{request} Find the original post by @{author}; preserve the exact relationship and any caveats."), timeout=45)
    except Exception:
        logger.info("attributed research: source discovery failed", exc_info=True)
        return {"answer": unavailable, "trajectory": None, "pipeline": "attributed_research"}
    sources = [s for s in found.get("sources", []) if author.lower() in
               (str(s.get("title") or "") + " " + str(s.get("url") or "")).lower()]
    for source in sources[:2]:
        url = source.get("url") or ""
        try:
            page = await asyncio.wait_for(asyncio.to_thread(read_page, url), timeout=45)
        except Exception:
            continue
        passage = _attributed_passage(page.get("text") or "", request, author)
        if not passage:
            continue
        label = "original or mirrored author page" if page.get("provenance") == "page" else "indexed author page"
        card = f"# What @{author} wrote\n\n{passage}\n\nSource: [{label}]({url})"
        streaming.emit("status", text="Checking the author's words")
        try:
            composed = await asyncio.wait_for(composition.synthesize(
                request + "\nAnswer in 2-4 sentences from this author's passage only. Distinguish the author's claim from an independently verified fact, and distinguish an asset's trading pair or possible rewards from ownership of the paired asset.",
                card, {"tool_name_0": "attributed_source_read", "tool_args_0": {"url": url}, "observation_0": passage}, research=True), timeout=45)
            summary = composed.split("\n\n---\n\n", 1)[0].removeprefix("**Taken together**").strip()
        except Exception:
            summary = ""
        answer = (summary + f"\n\nSource: [@{author}'s post or author page]({url})" if summary
                  else f"I found the author's passage but could not safely summarize it right now. [Read it here]({url}).")
        return {"answer": answer, "trajectory": {"research_progress": {
            "status": "complete", "kind": "attributed_source",
            "checked_pages": 1 if page.get("provenance") == "page" else 0,
            "sources": [{"name": f"@{author}", "url": url, "provenance": page.get("provenance"), "verdict": "attributed"}]}},
            "pipeline": "attributed_research"}
    return {"answer": unavailable, "trajectory": None, "pipeline": "attributed_research"}


def tool_for(capability: str, contract: contracts.QuestionContract, enabled_tools: set[str]) -> tuple[str | None, str]:
    """The one tool that may run for a capability under the deterministic
    boundary: (tool name, reason). A discovery group takes a query; a group
    that needs an address runs only on a resolved contract; a group that
    needs a wallet or an amount is not research."""
    meta = CAPABILITY_JOBS.get(capability)
    if not meta:
        return None, f"unknown capability {capability}"
    needs = set(meta[1])
    if "wallet" in needs or "amount" in needs:
        return None, f"{capability} is not a research source"
    for tool in _tools_of(capability):
        cov = tool_catalog.CONTRACT_COVERAGE.get(tool) or {}
        if cov.get("kinds") and not tool_catalog.eligible(tool, contract)[0]:
            continue                                     # a contract-kind tool serves only the kinds it declares (a ranking is not research)
        if "address" in set(cov.get("inputs") or []) and not contract.subject.id:
            continue                                     # a state read on a symbol the conversation has not resolved
        if tool in enabled_tools:
            return tool, "eligible"
    if "address" in needs and not contract.subject.id:
        return None, f"{capability} needs a contract address the conversation has not resolved"
    return None, f"no enabled tool for {capability}"


def _compact(cards: str, limit: int = 9000) -> str:
    return cards if len(cards) <= limit else cards[:limit] + "\n…"


def _coverage_brief(verdicts: list[dict] | None, conditions: str, required_facts: str) -> str:
    """The gap planner sees only checked pages and explicit unknowns.

    This is the hand-off between discovery and reasoning. It intentionally
    excludes the provider's answer-shaped search prose, which may assert the
    very relationship we are trying to verify.
    """
    if verdicts is None:
        return "Page inspection failed; no candidate is established."
    brief = research_ledger.CoverageLedger.from_verdicts(conditions, required_facts, verdicts).brief()
    for verdict in verdicts:
        for passage in (verdict.get("passages") or [])[:5]:
            if passage.get("url") and passage.get("quote"):
                brief += f"\nChecked but not necessarily sufficient [{passage['url']}]: {passage['quote']}"
    return brief


def _condition_discovery_query(conditions: str) -> str:
    """Search the qualifying relationship itself, without a speculative name
    or an optional fact from the plan becoming a new search constraint."""
    terms = [part.strip() for part in conditions.split(";") if part.strip()]
    return "historical projects " + "; ".join(terms[:3]) + " original primary documentation examples"


def _verified_partial(ledger: research_ledger.CoverageLedger, verdicts: list[dict]) -> str:
    """A safe, useful answer when there is no time for audited synthesis."""
    lines = ["**Checked source passages so far**"]
    for verdict in verdicts:
        name = verdict.get("name") or "Candidate"
        if ledger.complete(name) and verdict.get("verdict") == "qualifies":
            lines.append(f"- **{name}** has passages mapped to the checked conditions; the final relationship audit did not finish:")
            for row in ledger.records:
                if row.candidate == name and row.status == "supported":
                    lines.append(f"  - {row.requirement}: [source]({row.source_url}) says “{row.passage}”")
        elif verdict.get("provenance") == "page" and verdict.get("passages"):
            passage = verdict["passages"][0]
            missing = ", ".join(ledger.missing(name)) or "the full relationship"
            lines.append(f"- **{name}** remains unverified for {missing}. [Inspected source]({passage['url']}) says “{passage['quote']}”")
    if len(lines) == 1:
        lines.append("No checked source passage yet establishes the requested relationship.")
    return "\n".join(lines)


def _news_sources(result) -> list[dict]:
    """Source metadata is a discovery lead, never a verified citation."""
    found = getattr(result, "structured", None) or {}
    rows = found.get("sources") or []
    if rows:
        return [row for row in rows if isinstance(row, dict) and str(row.get("url") or "").startswith("https://")]
    return [{"title": title, "url": url, "date": ""} for _, title, url in _SOURCE_LINE.findall(result.output or "")]


def _recent_news_lead(row: dict, *, max_age_days: int = 14) -> bool:
    """A dated Home-news lead must belong to the current event window.

    Older coverage can explain the mechanism, but it cannot be cited as the
    observed response to today's headline. Unknown dates remain leads and
    still require a direct page passage to establish their event time.
    """
    from datetime import date, timedelta
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", str(row.get("date") or ""))
    if not match:
        return True
    try:
        published = date.fromisoformat(match.group(1))
    except ValueError:
        return True
    return published >= date.today() - timedelta(days=max_age_days)


def _news_fallbacks(leads: list[dict], axis: int, used: set[str]) -> list[str]:
    """Readable, specific leads before home pages and PDFs on the same axis."""
    ranked = []
    used_domains = {_source_domain(url) for url in used}
    for order, row in enumerate(leads):
        url = row["url"]
        if row["axis"] != axis or url in used:
            continue
        path = urlsplit(url).path.lower().strip("/")
        title = row["title"].lower()
        score = (3 if path and not path.endswith(".pdf") else -3)
        score += 2 if row.get("date") else 0
        score -= 8 if _source_domain(url) in used_domains else 0
        if axis == 0:
            score += 4 if _source_domain(url).endswith(".gov") else 0
            score += 2 if any(word in path for word in ("pressrelease", "press-releases", "announcement", "filing")) else 0
        else:
            score += 2 if any(word in title for word in ("market", "price", "bitcoin", "crypto", "stocks", "trading")) else 0
        ranked.append((-score, order, url))
    return [url for _, _, url in sorted(ranked)]


async def _run_headline(request: str, contract: contracts.QuestionContract, router, chains: tuple[str, ...],
                        plan, runtime: BudgetedRuntime, budget: TurnBudget, timings: dict) -> dict:
    """Broad discovery, then fact-level convergence for a Home news event.

    A regulatory event and a market reaction are separate facts that may live
    on separate pages. The comparison verifier asks one candidate to meet all
    conditions and can reject a correct primary page for not also proving
    market causation. This path checks each facet independently against exact
    source-page passages, then audits the combined answer.
    """
    from app import evidence_pipeline, home_highlights

    headline = _news_headline(contract, request)
    calls = _opening_calls(plan, contract, request)
    made: list[str] = []
    t0 = time.monotonic()
    streaming.research_progress("plan", "Checking the original event separately from the market reaction")

    async def search(capability: str, query: str):
        tool, _ = tool_for(capability, contract, {t.name for t in router.tools() if (router._enabled(t) if hasattr(router, "_enabled") else True)})
        if not tool:
            return None
        return await budget.call("provider", evidence_pipeline._invoke(router, tool, query, chains, contract), 45, reserve=40)

    streaming.research_progress("sources", "Searching for the original event and a dated market response")
    streaming.emit("status", text="Searching the original event and market response")
    results = await asyncio.gather(*(search(cap, query) for cap, query in calls), return_exceptions=True)
    leads: list[dict] = []
    home_card = home_highlights.cached_card_for_prompt(composition.split_notes(request)[0])
    if home_card and _recent_news_lead(home_card):
        leads.append({"axis": 0, "url": home_card["source_url"], "title": home_card.get("title") or headline,
                      "date": home_card.get("date") or "", "home_card": True})
    for axis, ((capability, query), result) in enumerate(zip(calls, results)):
        made.append(f"{capability}: {query}")
        if isinstance(result, Exception) or result is None or not getattr(result, "output", ""):
            continue
        for row in _news_sources(result):
            if not _recent_news_lead(row):
                continue
            url = row["url"]
            if (axis, url) not in {(lead["axis"], lead["url"]) for lead in leads}:
                leads.append({"axis": axis, "url": url, "title": str(row.get("title") or url), "date": str(row.get("date") or "")})
    timings["discovery"] = round(time.monotonic() - t0, 1)
    streaming.research_progress("sources", f"Found {len(leads)} dated source leads", found=len(leads))
    gate = fact_gate.check(contract, [], scope_satisfied=True)
    if not leads:
        # No dated lead to read: the fixed sequence answers from its dated web
        # card and the market pulse card rather than refusing the tap (the
        # loop had withheld headline taps after 100-160 s, review 2026-09-26).
        logger.info("research loop: headline lane found no dated lead; the fixed sequence answers")
        return None

    source_lines = "\n".join(f"{'original-event search' if row['axis'] == 0 else 'market-response search'} | {row['url']} | {row['title']} | {row['date'] or 'date unknown'}" for row in leads[:30])
    try:
        choice = await runtime._call_research_loop_lm(_news_source_program, headline=headline, sources=source_lines)
    except Exception:
        choice = SimpleNamespace(event_url="none", market_url="none")
    allowed = {"event": {row["url"] for row in leads if row["axis"] == 0},
               "market": {row["url"] for row in leads if row["axis"] == 1}}
    selected = {facet: (getattr(choice, facet + "_url", "") or "").strip() for facet in ("event", "market")}
    # A tap starts with the article Orbit itself placed on Home. It remains a
    # lead until read and checked; the model cannot replace it with a broad
    # results page or a different story bearing a similar headline.
    if home_card and any(row.get("home_card") for row in leads):
        selected["event"] = home_card["source_url"]
    # A failed picker does not erase the discovery result. A government page
    # is a reasonable event lead; other facets use the source order only as a
    # lead, and still need direct passages to be accepted.
    for facet in ("event", "market"):
        candidates = [row for row in leads if row["axis"] == (0 if facet == "event" else 1)]
        if selected[facet] not in allowed[facet]:
            fallback = next((row["url"] for row in candidates if facet == "event" and _source_domain(row["url"]).endswith(".gov")), None)
            selected[facet] = fallback or next((row["url"] for row in candidates if urlsplit(row["url"]).path.strip("/")), "")
    # A direct, dated government release is stronger event evidence than a
    # search summary that repeats it. This is source-type priority, not a
    # headline-specific domain allowlist; its page must still be read.
    government_release = next((row["url"] for row in leads if row["axis"] == 0
                               and _source_domain(row["url"]).endswith(".gov")
                               and any(word in urlsplit(row["url"]).path.lower() for word in ("pressrelease", "press-releases", "announcement"))
                               and not urlsplit(row["url"]).path.lower().endswith(".pdf")), None)
    if government_release and not (home_card and selected["event"] == home_card["source_url"]):
        selected["event"] = government_release
    if selected["market"] == selected["event"]:
        selected["market"] = next((row["url"] for row in leads if row["axis"] == 1 and row["url"] != selected["event"]), "")

    t_read = time.monotonic()
    page_cache: dict[str, dict] = {}

    async def fetch(url: str) -> tuple[str, dict]:
        try:
            page = await budget.call("page", asyncio.to_thread(read_page, url), 40, reserve=30)
        except Exception:
            page = {"url": url, "text": "", "provenance": "unreadable", "fetched_at": ""}
        return url, page

    streaming.research_progress("review", "Opening the selected event and market source pages", found=len(leads))
    page_cache.update(await asyncio.gather(*(fetch(url) for url in dict.fromkeys(url for url in selected.values() if url))))
    made.extend(f"page_read: {url}" for url in page_cache)

    async def inspect(facet: str, url: str) -> list[dict]:
        if not url:
            return []
        page = page_cache[url]
        if page.get("provenance") != "page" or not page.get("text"):
            return []
        try:
            extracted = await runtime._call_research_loop_lm(_news_passages_program, headline=headline, facet=facet,
                                                               page=page["text"][:14000])
        except Exception:
            return []
        accepted = []
        for raw in (getattr(extracted, "passages", "") or "").splitlines()[:5]:
            quote = _clean_quote(raw)
            if _verbatim_passage(quote, page["text"][:14000]) and quote not in {r["quote"] for r in accepted}:
                accepted.append({"facet": facet, "url": url, "quote": quote, "fetched_at": page.get("fetched_at", "")})
        return accepted[:4]

    # The two pages and their passage reads run independently. A page can be
    # the source for both facets; the cache prevents a duplicate fetch.
    event_rows, market_rows = await asyncio.gather(inspect("event", selected["event"]), inspect("market", selected["market"]))
    for facet, rows, axis in (("event", event_rows, 0), ("market", market_rows, 1)):
        if rows or not budget.can_run(45):
            continue
        for url in _news_fallbacks(leads, axis, set(page_cache))[:4]:
            found_url, page = await fetch(url)
            page_cache[found_url] = page
            made.append(f"page_read: {url}")
            replacement = await inspect(facet, url)
            if replacement:
                selected[facet] = url
                if facet == "event":
                    event_rows = replacement
                else:
                    market_rows = replacement
                break
            if not budget.can_run(45):
                break
    timings["inspection"] = round(time.monotonic() - t_read, 1)
    checked_pages = sum(page.get("provenance") == "page" for page in page_cache.values())
    streaming.research_progress("review", f"Checked {checked_pages} source page{'s' if checked_pages != 1 else ''} against the event and market claims",
                                found=len(leads), checked=checked_pages)
    accepted = event_rows + market_rows
    coverage = {"event": bool(event_rows), "market": bool(market_rows), "selected": selected,
                "source_leads": leads[:30], "passages": accepted}
    trajectory = {"research_loop": {"question": headline, "calls": made, "coverage": coverage,
                                    "budget": budget.record(), "timings": timings}}
    if not event_rows:
        # The event page could not be read or yielded no passage: the fixed
        # sequence answers from the dated web card, labelled as such.
        logger.info("research loop: headline lane verified no event passage; the fixed sequence answers")
        return None
    card = "# Checked source passages\n\n" + "\n".join(
        f"- **{row['facet']}** · [source]({row['url']}) · fetched {row['fetched_at'] or 'time unknown'}: “{row['quote']}”" for row in accepted)
    note = ("Answer the Home news question directly and briefly. State what the original source confirms, including whether an action is proposed or final. "
            "Then give only market observations supported by checked market passages, with their time. "
            "If no market passage was verified, state that the market response was not verified. "
            "Separate possible transmission mechanisms from observed reactions; timing alone does not prove this event caused a price move. "
            "Do not offer a trade recommendation or use unverified search snippets as evidence.")
    # The market's state now joins the checked passages (the tap fix of
    # 2026-09-26: majors, total cap, fear and greed; the social read for a
    # policy headline), so the read is grounded in state on this lane too.
    state_parts: list = []
    if composition.is_headline_tap(request):
        try:
            state_parts, tap_note = await evidence_pipeline._attach_market_state(router, request, chains, contract, [], {})
        except Exception:
            logger.info("research loop: market state unavailable for the tap", exc_info=True)
            state_parts, tap_note = [], ""
        if tap_note:
            note += "\n" + tap_note
    state_cards = "\n\n---\n\n".join(text for text, _ in state_parts)
    if state_cards:
        card = card + "\n\n---\n\n" + state_cards
        trajectory["research_loop"]["market_state"] = [meta.get("tool_name_0") for _, meta in state_parts]
    t_synth = time.monotonic()
    streaming.research_progress("answer", "Writing from the checked passages")
    try:
        with streaming.muted("delta"):
            answer = await budget.call("synthesis", composition.synthesize(f"{request}\n{note}", card, trajectory, research=True), 55, reserve=12)
    except Exception:
        answer = "**What the original source establishes**\n\n" + "\n".join(f"- [Source]({row['url']}): “{row['quote']}”" for row in event_rows)
        if not market_rows:
            answer += "\n\nI could not verify a market response from a directly read source."
        gate.ok = False
        trajectory["research_loop"]["budget"] = budget.record()
        return {"answer": answer, "trajectory": trajectory, "contract": contract.model_dump(), "gate": gate.model_dump(), "pipeline": "research_loop"}
    timings["synthesis"] = round(time.monotonic() - t_synth, 1)
    unsupported = fact_gate.unsupported_figures(answer.split("\n\n---\n\n", 1)[0], [], evidence_text=card)
    streaming.research_progress("answer", "Checking the draft's claims against the passages")
    claims = await _unsupported_claims(headline, answer, card, runtime) if budget.can_run(14) else None
    if (unsupported or claims) and budget.can_run(25):
        repair_note = (f"{request}\n{note}\nRemove or qualify these claims that the checked passages do not establish: "
                       + "; ".join((claims or [])[:6] + unsupported[:6])
                       + ". Preserve the verified facts and source links. Do not replace the removed claims with new unverified ones.")
        try:
            with streaming.muted("delta"):
                repaired = await budget.call("synthesis", composition.synthesize(repair_note, card, trajectory, research=True), 40, reserve=12)
            repaired_figures = fact_gate.unsupported_figures(repaired.split("\n\n---\n\n", 1)[0], [], evidence_text=card)
            repaired_claims = await _unsupported_claims(headline, repaired, card, runtime) if budget.can_run(12) else None
            if not repaired_figures and repaired_claims == []:
                answer, unsupported, claims = repaired, [], []
        except Exception:
            logger.info("headline answer repair failed; retaining checked facts", exc_info=True)
    coverage["answer_audit"] = {"unsupported_figures": unsupported, "unsupported_claims": claims,
                                "claim_check_available": claims is not None}
    if unsupported or claims is None or claims:
        # A useful verified partial is safer than an unverified causal story.
        answer = "**Checked facts**\n\n" + "\n".join(f"- [Source]({row['url']}): “{row['quote']}”" for row in accepted)
        if not market_rows:
            answer += "\n\nThe market response could not be verified from the pages read."
        if state_cards:
            answer += "\n\n---\n\n" + state_cards
        gate.ok = False
    else:
        gate.ok = True
        gate.missing = []
    timings["total"] = round(time.monotonic() - t0, 1)
    trajectory["research_loop"].update(budget=budget.record(), timings=timings)
    return {"answer": answer, "trajectory": trajectory, "contract": contract.model_dump(), "gate": gate.model_dump(), "pipeline": "research_loop"}


async def run(state: dict, request: str, contract: contracts.QuestionContract, router, chains: tuple[str, ...]) -> dict:
    """Run checked research, with a separate optional X discovery lane for news."""
    author = _attributed_author(request)
    if author and contract.kind == contracts.OPEN_RESEARCH_KIND:
        return await _run_attributed(request, author)
    headline = _news_headline(contract, request)
    if not (headline or contract.kind == "recent_events") or not x_news.enabled():
        return await _run(state, request, contract, router, chains)
    subject = getattr(contract, "subject", None)
    resolved = getattr(subject, "symbol", None) or getattr(subject, "name", None)
    topic = x_news.topic_from_headline(headline) if headline else (str(resolved).strip() if resolved else x_news.topic_from_headline(composition.split_notes(request)[0]))
    if not topic:
        return await _run(state, request, contract, router, chains)
    x_task = asyncio.create_task(x_news.search(topic, hours=min(168, int(contract.window_hours or 48))))
    result = await _run(state, request, contract, router, chains)
    x_result = await x_task
    x_card = x_news.render(x_result)
    if not result or not x_card:
        return result
    trajectory = dict(result.get("trajectory") or {})
    trajectory["x_discovery"] = {"query": x_result.get("query"), "posts": x_result["posts"],
                                  "status": "unverified social posts"}
    return {**result, "answer": result["answer"] + "\n\n---\n\n" + x_card, "trajectory": trajectory}


async def _run(state: dict, request: str, contract: contracts.QuestionContract, router, chains: tuple[str, ...]) -> dict:
    """The loop, returning the pipeline's answer shape."""
    from app import evidence_pipeline
    from app.nodes import runtime

    turn_budget = TurnBudget(max(30.0, min(180.0, float(getattr(settings, "research_loop_timeout_seconds", 240) or 240) - 15.0)))
    runtime = BudgetedRuntime(runtime, turn_budget)

    enabled_tools = {t.name for t in router.tools() if (router._enabled(t) if hasattr(router, "_enabled") else True)}
    catalog = catalog_summary()
    body, notes = composition.split_notes(request)
    if _news_headline(contract, request):
        # The question contract already identifies the Home news event. This
        # fact-based lane does not use the comparison planner's conditions;
        # skipping that call also prevents a planner failure from falling
        # back to unchecked search prose.
        return await _run_headline(request, contract, router, chains, SimpleNamespace(), runtime, turn_budget, {})
    streaming.research_progress("plan", "Identifying the question and evidence needed")
    streaming.emit("status", text="Planning the evidence")
    timings: dict[str, float] = {}
    t_plan = time.monotonic()
    try:
        program = _expanded_plan_program if settings.research_query_expansion_enabled else _plan_program
        plan = await runtime._call_research_loop_lm(program, request=request, catalog=catalog)
    except Exception:
        logger.warning("research loop: plan failed; the pipeline's fixed sequence answers", exc_info=True)
        return None
    timings["plan"] = round(time.monotonic() - t_plan, 1)
    question = (getattr(plan, "question", "") or body.splitlines()[0] if body else request).strip()
    required = (getattr(plan, "required_facts", "") or "").strip()
    constraints = (getattr(plan, "constraints", "") or "").strip()
    # The lane. A comparison with stated conditions ("which other projects
    # lock their own token to mint a second one") is the candidate lane: a
    # name stands only on a page passage that meets every condition, else the
    # summary is withheld. An ordinary question (what an order authorizes, why
    # a token moved, who backs a protocol) states no conditions: it is the
    # fact lane, written from the dated search cards and whatever page
    # passages were read, audited against both, withheld only when a claim
    # still fails after one repair. The verified-passage-or-withhold rule had
    # covered both and withheld ordinary answers after 100-160 s (review of
    # the loop, 2026-09-26); the brief scopes it to comparison questions.
    candidate_lane = bool(constraints) and constraints.lower() != "none"
    lane = "candidate" if candidate_lane else "fact"
    calls: list[DiscoveryCall | tuple[str, str]] = _expanded_calls(plan, contract, request)
    initial_queries = {call.query for call in calls if isinstance(call, DiscoveryCall)}
    streaming.research_progress("sources", f"Searching {len(calls)} evidence angle{'s' if len(calls) != 1 else ''}")
    if _news_headline(contract, request):
        required = "\n".join(filter(None, [required,
            "Original event or primary data source, with event time and proposed versus final status",
            "Dated observed market response, or an explicit statement that it could not be verified",
            "Causal interpretation labelled separately from observed facts"]))
    parts: list[tuple[str, dict]] = []
    fact_rows: list = []
    made: list[str] = []
    skipped: list[str] = []
    started = time.monotonic()
    verdicts: list[dict] | None = []
    page_cache: dict[str, dict] = {}
    check_cache: dict[tuple, dict] = {}
    inspect_seconds = 0.0
    retrieval_count = 0
    retrieval_budget = min(float(getattr(settings, "research_loop_seconds", 75) or 75), turn_budget.remaining() - 25.0)

    async def one(job: DiscoveryCall, tool: str):
        text = f"{job.query}\n{notes}" if notes else job.query
        streaming.research_progress("sources", f"Searching: {job.query[:120]}")
        streaming.emit("status", text=f"Reading {job.capability}: {job.facet or job.query[:80]}")
        options = {"recency_days": job.days, "domains": job.domains} if job.days or job.domains else None
        return await turn_budget.call("provider", evidence_pipeline._invoke(router, tool, text, chains, contract,
                                                                             **({"search_options": options} if options else {})), 45, reserve=25)

    for round_no in range(MAX_ROUNDS):
        batch = []
        for call in calls:
            if retrieval_count + len(batch) >= MAX_CALLS:
                break
            job = call if isinstance(call, DiscoveryCall) else DiscoveryCall(*call)
            tool, reason = tool_for(job.capability, contract, enabled_tools)
            if tool is None:
                skipped.append(f"{job.capability}: {reason}")
                made.append(f"{job.capability}: skipped ({reason})")
                continue
            batch.append((job, tool))
        # Independent eligible calls run together (the brief, step 3).
        results = await asyncio.gather(*(one(job, tool) for job, tool in batch), return_exceptions=True)
        retrieval_count += len(batch)
        for (job, tool), result in zip(batch, results):
            made.append(f"{job.capability} ({tool}){f' [{job.facet}]' if job.facet else ''}: {job.query}")
            if isinstance(result, Exception) or result is None or not result.output:
                continue
            # Search cards are leads until their pages are checked. Emitting
            # them here shows authoritative-looking prose before the gate.
            parts.append((result.output, {"tool_name_0": result.tool, "tool_args_0": {"request": job.query, "facet": job.facet,
                                                                                          "topic": job.topic, "days": job.days, "domains": job.domains},
                                          "observation_0": result.output}))
            fact_rows.extend(evidence_pipeline._facts_of(result, contract.kind))
        streaming.research_progress("sources", f"Collected {len(parts)} search result{'s' if len(parts) != 1 else ''}; checking cited pages",
                                    found=len(parts))
        if parts:
            # Read the sources before deciding what to search next. The old
            # order asked the gap model to judge search snippets, so a fluent
            # but wrong provider summary could prematurely end the research.
            cards_so_far, _ = composition.combine(parts)
            t_inspect = time.monotonic()
            inspected, inspect_calls = await _inspect_candidates(
                question, constraints,
                cards_so_far, runtime, router, page_cache=page_cache,
                followup_reads=(0 if round_no < MAX_ROUNDS - 1 else 4) if candidate_lane else 0,
                budget=turn_budget, check_cache=check_cache,
                **({} if candidate_lane else {"page_limit": FACT_LANE_PAGES}))
            # A later inspection can exhaust the budget after earlier pages
            # were checked. Keep the last accepted evidence state.
            if inspected is not None:
                verdicts = inspected
            elif not verdicts:
                verdicts = None
            inspect_seconds += time.monotonic() - t_inspect
            made.extend(inspect_calls)
            checked_pages = sum(page.get("provenance") == "page" for page in page_cache.values())
            streaming.research_progress("review", f"Checked {checked_pages} original page{'s' if checked_pages != 1 else ''} against the question",
                                        found=len(parts), checked=checked_pages)
        if (not parts or verdicts is None or retrieval_count >= MAX_CALLS
                or round_no == MAX_ROUNDS - 1 or not turn_budget.can_run(30)
                or time.monotonic() - started > retrieval_budget):
            break
        try:
            review = await runtime._call_research_loop_lm(_review_program, question=question, required_facts=required or "none stated",
                                                                       evidence=_compact(_coverage_brief(verdicts, (getattr(plan, "constraints", "") or "").strip() or question, required)),
                                                                       calls_made="\n".join(made), catalog=catalog)
        except Exception:
            logger.info("research loop: gap review failed; stopping", exc_info=True)
            break
        nxt = (getattr(review, "next_call", "") or "stop").strip()
        calls = [c for c in _parse_calls(nxt)[:1] if c[1] not in {m.split(": ", 1)[-1] for m in made}]
        # The second axis comes from observed coverage, rather than another
        # paraphrase of a search card. When no condition has an accepted
        # passage, search broadly for the mechanism; when some conditions are
        # proved, seek the missing condition across first-party publications.
        # The condition-driven second axis (a missing condition across
        # first-party pages, a broad mechanism search when no candidate
        # qualifies) belongs to the candidate lane; the fact lane runs the
        # review's next call as asked.
        if calls and candidate_lane and "site:" not in request.lower():
            coverage = research_ledger.CoverageLedger.from_verdicts((getattr(plan, "constraints", "") or question), required, verdicts)
            supported = [row for row in coverage.records if row.status == "supported"]
            if supported and "site:" in calls[0][1].lower():
                candidate = max(verdicts, key=lambda v: len(v.get("condition_evidence") or []))
                calls = [("web_discovery", f"{candidate['name']} {_missing_conditions((getattr(plan, 'constraints', '') or question), candidate)} official original documentation blog historical")]
            elif not supported:
                unproved = [v.get("name") or "" for v in verdicts]
                query = calls[0][1]
                if "site:" in query.lower() or any(name and name.casefold() in query.casefold() for name in unproved):
                    query = _condition_discovery_query((getattr(plan, "constraints", "") or question))
                    calls = [("web_discovery", query)]
            if supported and any(v.get("verdict") != "qualifies" for v in verdicts):
                # A partially supported candidate deserves a targeted read,
                # but it must not monopolize discovery of other candidates.
                # Run the independent condition query alongside it.
                broad = _condition_discovery_query((getattr(plan, "constraints", "") or question))
                prior = initial_queries | {q for _, q in _parse_calls(getattr(plan, "queries", ""))}
                if broad not in prior and broad not in {q for _, q in calls}:
                    calls = [("web_discovery", broad)] + calls
        elif not calls and round_no == 0 and candidate_lane and verdicts and "site:" not in request.lower():
            coverage = research_ledger.CoverageLedger.from_verdicts((getattr(plan, "constraints", "") or question), required, verdicts)
            if not any(v.get("verdict") == "qualifies" for v in verdicts) and _tools_of("web_discovery"):
                alternate = _condition_discovery_query((getattr(plan, "constraints", "") or question))
                if alternate not in initial_queries | {query for _, query in _parse_calls(getattr(plan, "queries", ""))}:
                    calls = [("web_discovery", alternate)]
        if not calls:
            # A stop decision should not strand a partially checked named
            # candidate when one cited or first-party page may close the
            # specific gap. Spend a small final read allowance, then answer
            # from that verdict; do not return to a broad search.
            if (candidate_lane and verdicts and not any(v.get("verdict") == "qualifies" for v in verdicts)
                    and turn_budget.can_run(30)):
                t_inspect = time.monotonic()
                inspected, inspect_calls = await _inspect_candidates(
                    question, constraints,
                    cards_so_far, runtime, router, page_cache=page_cache,
                    followup_reads=4, budget=turn_budget, check_cache=check_cache)
                if inspected is not None:
                    verdicts = inspected
                elif not verdicts:
                    verdicts = None
                inspect_seconds += time.monotonic() - t_inspect
                made.extend(inspect_calls)
            break
    timings["retrieval"] = round(max(0.0, time.monotonic() - started - inspect_seconds), 1)
    timings["inspect"] = round(inspect_seconds, 1)
    if not parts:
        gate = fact_gate.check(contract, [], scope_satisfied=True)
        text = "The sources for this returned nothing usable right now. " + gate.gap_sentence(contract) + ("\n\n" + "; ".join(skipped) if skipped else "")
        return {"answer": text.strip(), "trajectory": None, "contract": contract.model_dump(), "gate": gate.model_dump(), "pipeline": "research_loop"}
    cards, trajectory = composition.combine(parts)
    gate = fact_gate.check(contract, fact_rows, scope_satisfied=True)
    note = evidence_pipeline._contract_note(contract, gate, fact_rows, None)
    if verdicts is None and not candidate_lane:
        verdicts = []          # the fact lane names no candidates; a failed listing leaves the cards as the evidence
    ledger = research_ledger.CoverageLedger.from_verdicts(constraints if candidate_lane else question, required, verdicts)
    if _news_headline(contract, request):
        note += ("\nFor this news question, first explain the original event and its status from an inspected source page. "
                 "Then describe any observed market response with its timestamp and source. "
                 "A market move around an event does not establish causation; label proposed mechanisms and implications as interpretation. "
                 "If the market response was not verified, say so rather than presenting an impact or trading conclusion as fact.")
    if constraints and constraints.lower() != "none":
        note += f"\nConstraints the user stated, to apply strictly: {constraints}"
    # Inspection already happened at the end of each retrieval round; use
    # that same evidence state for synthesis and the user-visible trail.
    if verdicts is None:
        # The candidate check could not run: nothing named can be verified, so no written summary (review of 0b360f9e).
        timings["total"] = round(time.monotonic() - started, 1)
        withheld = _with_trail(_withheld("the candidate check could not run, so no named example is verified", cards), made, skipped, [], timings)
        trajectory = dict(trajectory or {})
        trajectory["research_loop"] = {"subject": getattr(plan, "subject", ""), "question": question, "constraints": constraints, "required_facts": required, "lane": lane,
                                       "calls": made, "skipped": skipped, "verdicts": [], "timings": timings, "withheld": "candidate check failed"}
        gate.ok = False
        return {"answer": withheld, "trajectory": trajectory, "contract": contract.model_dump(), "gate": gate.model_dump(), "pipeline": "research_loop"}
    if verdicts:
        note += "\nCandidates checked against the conditions from their cited pages (state these verdicts, never upgrade one): " + "; ".join(
            f"{v['name']}: {v['verdict']}" + (f" ({v['conditions_failed']})" if v["verdict"] == "related_but_different" and v.get("conditions_failed") not in (None, "", "none") else "")
            for v in verdicts)
    verified = [v for v in verdicts if v["verdict"] in ("qualifies", "related_but_different")
                and v.get("provenance") == "page" and v.get("quote") not in (None, "", "none")
                and (v["verdict"] != "qualifies" or ledger.complete(v["name"]))]
    if not candidate_lane:
        # Every literal passage read is evidence here, whatever verdict the
        # condition check gave it: the conditions were the question itself.
        verified = [v for v in verdicts if v.get("provenance") == "page" and v.get("quote") not in (None, "", "none")]
    if not verified and candidate_lane:
        # Search prose is a discovery lead. Without one directly inspected
        # passage the research tier cannot turn it into an asserted answer.
        timings["total"] = round(time.monotonic() - started, 1)
        partial = []
        for v in verdicts:
            if v.get("provenance") == "page" and v.get("quote") not in (None, "", "none"):
                gap = v.get("conditions_failed") or ""
                detail = f"; still unverified: {gap}" if gap.lower() != "none" else "; the full requested relationship remains unverified"
                partial.append(f"- **{v['name']}**: [inspected page]({v['url']}) says “{v['quote']}”{detail}.")
        reason = "no cited page passage established every condition needed for a match"
        preface = "**No fully verified match yet.** The inspected pages support only these narrower observations:\n\n" + "\n".join(partial) + "\n\n" if partial else ""
        withheld = _with_trail(preface + _withheld(reason, cards), made, skipped, verdicts, timings)
        trajectory = dict(trajectory or {})
        trajectory["research_loop"] = {"subject": getattr(plan, "subject", ""), "question": question, "constraints": constraints, "required_facts": required, "lane": lane,
                                       "calls": made, "skipped": skipped, "verdicts": verdicts, "coverage": ledger.as_dict(),
                                       "budget": turn_budget.record(), "timings": timings, "withheld": "no verified page passage"}
        gate.ok = False
        return {"answer": withheld, "trajectory": trajectory, "contract": contract.model_dump(), "gate": gate.model_dump(), "pipeline": "research_loop"}
    verified_lines = []
    for v in verified:
        passages = v.get("passages") or [{"url": v["url"], "quote": _clean_quote(v["quote"]), "fetched_at": v.get("fetched_at", "")}]
        for passage in passages:
            verified_lines.append(f"- **{v['name']}** — {v['verdict']} · [page]({passage['url']}) · "
                                  f"fetched {passage.get('fetched_at') or 'time unknown'}: “{passage['quote']}”")
    verified_cards = ("# Reviewed source-page passages\n\n" + "\n".join(verified_lines)) if verified_lines else ""
    if candidate_lane:
        note += "\nWrite factual claims from the reviewed page passages below, not from the unverified search snippets. An unverified candidate may be mentioned only as not established."
    else:
        # The fact lane's evidence: the dated, sourced search cards and the
        # passages read from their pages; a passage outranks a snippet.
        verified_cards = (verified_cards + "\n\n---\n\n" if verified_cards else "") + cards
        note += ("\nWrite from the dated search cards and the reviewed page passages below, nothing beyond them; cite each claim with its source, "
                 "and prefer a reviewed page passage over a search snippet where both speak. Say what the sources do not establish.")
    if not turn_budget.can_run(35):
        turn_budget.exhausted = turn_budget.exhausted or "insufficient time for audited synthesis"
        final = fact_gate.check(contract, fact_rows, scope_satisfied=True)
        final.ok = False
        answer = _with_trail(_verified_partial(ledger, verdicts), made, skipped, verdicts, timings)
        trajectory = dict(trajectory or {})
        trajectory["research_loop"] = {"question": question, "calls": made, "verdicts": verdicts,
                                       "coverage": ledger.as_dict(), "budget": turn_budget.record(), "timings": timings}
        return {"answer": answer, "trajectory": trajectory, "contract": contract.model_dump(), "gate": final.model_dump(), "pipeline": "research_loop"}
    t_synth = time.monotonic()
    streaming.research_progress("answer", "Writing the answer from reviewed passages")
    try:
        with streaming.muted("delta"):
            synthesized = await turn_budget.call("synthesis", composition.synthesize(f"{request}\n{note}", verified_cards, trajectory, research=True), 65, reserve=12)
    except TimeoutError:
        final = fact_gate.check(contract, fact_rows, scope_satisfied=True)
        final.ok = False
        trajectory = dict(trajectory or {})
        trajectory["research_loop"] = {"question": question, "calls": made, "verdicts": verdicts,
                                       "coverage": ledger.as_dict(), "budget": turn_budget.record()}
        return {"answer": _with_trail(_verified_partial(ledger, verdicts), made, skipped, verdicts, timings),
                "trajectory": trajectory, "contract": contract.model_dump(), "gate": final.model_dump(), "pipeline": "research_loop"}
    timings["synthesis"] = round(time.monotonic() - t_synth, 1)
    streaming.research_progress("answer", "Checking the answer's figures and claims against the passages")
    final = fact_gate.check(contract, fact_rows, synthesized, scope_satisfied=True, evidence_text=verified_cards)
    # Source snippets and their parsed facts are not evidence for a figure in
    # the written answer. Only the inspected passage may support that figure.
    final.unsupported = fact_gate.unsupported_figures(synthesized.split("\n\n---\n\n", 1)[0], [], evidence_text=verified_cards)
    final.ok = final.ok and not final.unsupported
    if final.unsupported or final.contradictions:
        # Repair once (the brief, step 4), then withhold what still fails.
        redo = f"{request}\n{note}\n"
        if final.unsupported:
            redo += f"Do not state these figures, no fact card carries them: {', '.join(final.unsupported)}. State only figures that appear in the cards, or describe without the number. "
        if final.contradictions:
            redo += f"These comparisons contradict their own numbers, rewrite them correctly or drop them: {'; '.join(final.contradictions)}."
        with streaming.muted("delta"):
            rewritten = await turn_budget.call("synthesis", composition.synthesize(redo, verified_cards, trajectory, research=True), 45) if turn_budget.can_run(12) else ""
        if rewritten:
            again = fact_gate.check(contract, fact_rows, rewritten, scope_satisfied=True, evidence_text=verified_cards)
            again.unsupported = fact_gate.unsupported_figures(rewritten.split("\n\n---\n\n", 1)[0], [], evidence_text=verified_cards)
            again.ok = again.ok and not again.unsupported
            if len(again.unsupported) + len(again.contradictions) < len(final.unsupported) + len(final.contradictions):
                synthesized, final = rewritten, again
    if final.unsupported or final.contradictions:
        what = ("figures no source card carries (" + ", ".join(final.unsupported[:6]) + ")") if final.unsupported else ("a comparison the numbers deny (" + "; ".join(final.contradictions[:3]) + ")")
        synthesized = _withheld(f"it stated {what}", verified_cards)
    else:
        t_support = time.monotonic()
        if not turn_budget.can_run(20):
            turn_budget.exhausted = turn_budget.exhausted or "insufficient time for claim audit"
            synthesized = _verified_partial(ledger, verdicts)
            final.ok = False
        else:
            if candidate_lane:
                synthesized = await _verify_examples(question, synthesized, verified_cards, request, note, trajectory, runtime, verdicts)
            if not turn_budget.can_run(12):
                synthesized = _verified_partial(ledger, verdicts)
                final.ok = False
        if not synthesized.startswith(("**I withheld", "**Checked source passages so far")):
            unsupported = await _unsupported_claims(question, synthesized, verified_cards, runtime)
            if unsupported is None:
                synthesized = _withheld("the claim-to-passage check could not run", verified_cards)
            elif unsupported:
                redo = (f"{request}\n{note}\nThe following claims are not established by the reviewed page passages: "
                        + "; ".join(unsupported[:8])
                        + ". Remove them or explicitly say they are not established. Write only what the quoted passages prove.")
                with streaming.muted("delta"):
                    rewritten = await turn_budget.call("synthesis", composition.synthesize(redo, verified_cards, trajectory, research=True), 45) if turn_budget.can_run(12) else ""
                again = await _unsupported_claims(question, rewritten, verified_cards, runtime) if rewritten else None
                figures = fact_gate.unsupported_figures(rewritten.split("\n\n---\n\n", 1)[0], [], evidence_text=verified_cards) if rewritten else []
                synthesized = rewritten if again == [] and not figures else _withheld("the summary still asserted claims its reviewed passages do not establish", verified_cards)
        timings["support"] = round(time.monotonic() - t_support, 1)
    # Discovery cards are retained in the trajectory for review, but their
    # provider-written prose must not appear below a page-checked answer or
    # abstention. It can contradict the verdict and reads like a second,
    # authoritative answer in the chat UI.
    if synthesized.startswith("**I withheld"):
        final.ok = False
    timings["total"] = round(time.monotonic() - started, 1)
    synthesized = _with_trail(synthesized, made, skipped, verdicts, timings)
    trajectory = dict(trajectory or {})
    trajectory["research_loop"] = {"subject": getattr(plan, "subject", ""), "question": question, "constraints": constraints, "required_facts": required, "lane": lane,
                                   "calls": made, "skipped": skipped, "verdicts": verdicts, "coverage": ledger.as_dict(),
                                   "budget": turn_budget.record(), "timings": timings}
    logger.info("research loop record: %s", trajectory["research_loop"])
    return {"answer": synthesized, "trajectory": trajectory, "contract": contract.model_dump(), "gate": final.model_dump(), "pipeline": "research_loop"}


_LABELLED = re.compile(r"not[_ ]established|different[_ ]design|does not qualify|not a match|not verified|not confirmed|related[_ ]but|no first-party|cannot be confirmed"
                       r"|not documented|unverified|receipt(?:s| token| design)?|wrapped|non-transferable|external collateral|does not meet|fails the|not the same mechanism|differs", re.I)


def unlabelled_names(summary: str, verdicts: list[dict]) -> list[str]:
    """Names whose page verdict is not "qualifies" and which the summary
    mentions in a sentence that does not say so: the page verdicts are the
    hard gate, whatever the search snippet said (review of 0b360f9e)."""
    prose = summary.split("\n\n---\n\n", 1)[0]
    out = []
    for v in verdicts:
        if v.get("verdict") == "qualifies":
            continue
        name = v.get("name") or ""
        if not name:
            continue
        # Per clause, not per sentence: "Nova Labs is an exact match, but Aave
        # is not established" labels Aave only (review of f603a871).
        for clause in re.split(r"(?<=[.!?])\s+|\n+|[;:,]\s+|\s+(?:while|whereas|although|though|however)\s+", prose, flags=re.I):     # a comma already splits ", but"; "related but a different design" stays one clause
            if name.lower() in clause.lower() and not _LABELLED.search(clause):
                out.append(name)
                break
    return out


def _new_named_examples(groups: tuple[str, str], known: set[str]) -> list[dict]:
    """The example checker may return unsupported claims in its name fields.
    Leave those to the claim audit; only compact names need an entity gate."""
    out = []
    for group in groups:
        for raw in group.split(","):
            name = raw.strip()
            words = name.split()
            first = words[0] if words else ""
            if (name and name.casefold() not in known and 1 <= len(words) <= 4
                    and (first[0].isupper() or bool(re.match(r"^[a-z][A-Z]", first)))
                    and not any(char in name for char in ":;?!")):
                out.append({"name": name, "verdict": "not_established"})
    return out


async def _verify_examples(question: str, synthesized: str, cards: str, request: str, note: str, trajectory: dict, runtime, verdicts: list[dict] | None = None) -> str:
    """The central promise: a name the cited evidence does not establish never
    stands as a claim. The page verdicts gate first, deterministically: a
    name whose page did not establish the mechanism may appear only in a
    sentence that says so. Then the support check runs over the cards and
    the verdicts; a name either flags is removed or relabelled by one
    rewrite, checked again; whatever still fails, or a check that cannot
    run, withholds the written summary."""
    verdicts = verdicts or []
    gated = unlabelled_names(synthesized, verdicts)
    checked = await _support_check(question, synthesized, cards, runtime, verdicts)
    if checked is None:
        return _withheld("the example check could not run, so no named example is verified", cards)
    related, missing = checked
    # The support model classifies exclusions as related/not established even
    # when the prose has already labelled them honestly. That classification
    # is not a failure; only an unlabelled use as a positive example is.
    known = {v.get("name", "").casefold() for v in verdicts}
    extra = _new_named_examples((related, missing), known)
    gated.extend(unlabelled_names(synthesized, extra))
    if not gated:
        return synthesized
    redo = (f"{request}\n{note}\nThe cited sources do not establish these names as examples of the mechanism -- "
            + f"unlabelled names: {', '.join(gated)}; "
            + "state each of them only as such, or leave it out; never present it as a match.")
    with streaming.muted("delta"):
        rewritten = await composition.synthesize(redo, cards, trajectory, research=True)
    if rewritten and not unlabelled_names(rewritten, verdicts + extra):
        again = await _support_check(question, rewritten, cards, runtime, verdicts)
        if again is not None:
            fresh = _new_named_examples(again, set())
            if not unlabelled_names(rewritten, verdicts + fresh):
                return rewritten
    return _withheld("it presented as examples names the cited sources do not establish (" + ", ".join(gated) + ")", cards)


def _withheld(reason: str, cards: str) -> str:
    reviewed = []
    if cards.startswith("# Reviewed source-page passages"):
        reviewed = [line for line in cards.splitlines() if line.startswith("- **")][:4]
    observed = ("\n\n**Directly checked passages:**\n\n" + "\n".join(reviewed)
                + "\n\nThese passages establish only what they say; the full requested conclusion remains unverified.") if reviewed else ""
    return (f"**I withheld the written summary: {reason}. The research trail shows what was checked "
            f"and how each candidate fared.**{observed}")


async def _unsupported_claims(question: str, answer: str, cards: str, runtime) -> list[str] | None:
    summary = answer.split("\n\n---\n\n", 1)[0]
    try:
        result = await asyncio.wait_for(runtime._call_research_loop_lm(
            _claim_support_program, question=question, summary=summary[:6500], evidence=_compact(cards, 14000)), timeout=45)
    except Exception:
        logger.info("research loop: claim support check failed", exc_info=True)
        return None
    raw = (getattr(result, "unsupported_claims", "") or "none").strip()
    return [] if raw.lower() == "none" else [line.strip().lstrip("-* ") for line in raw.splitlines() if line.strip()]


async def _support_check(question: str, synthesized: str, cards: str, runtime, verdicts: list[dict] | None = None) -> tuple[str, str] | None:
    """(related-but-different names, not-established names), each '' when
    none; None when the check could not run. The page verdicts lead the
    evidence the check reads: a page verdict outranks a search snippet."""
    evidence = _compact(cards)
    if verdicts:
        evidence = ("Page verdicts (each candidate's own cited page checked against the conditions; a page verdict outranks any search snippet): "
                    + "; ".join(f"{v['name']}: {v['verdict']}" for v in verdicts) + "\n\n" + evidence)
    try:
        support = await asyncio.wait_for(runtime._call_research_loop_lm(_support_program, question=question, answer=synthesized[:6000], evidence=evidence), timeout=60)
    except Exception:
        logger.info("research loop: example support check failed", exc_info=True)
        return None
    related = (getattr(support, "related_but_different", "") or "").strip()
    missing = (getattr(support, "not_established", "") or "").strip()
    return ("" if related.lower() == "none" else related, "" if missing.lower() == "none" else missing)


async def _label_examples(question: str, synthesized: str, cards: str, runtime) -> str:
    """Append the support labels for the named examples; never silently drop
    a name, never pass an unsupported one as sourced."""
    try:
        support = await asyncio.wait_for(runtime._call_research_loop_lm(_support_program, question=question, answer=synthesized[:6000], evidence=_compact(cards)), timeout=60)
    except Exception:
        logger.info("research loop: example support check failed", exc_info=True)
        return synthesized
    related = (getattr(support, "related_but_different", "") or "").strip()
    missing = (getattr(support, "not_established", "") or "").strip()
    lines = []
    if related and related.lower() != "none":
        lines.append(f"**Related but a different design (per the cited sources)**: {related}")
    if missing and missing.lower() != "none":
        lines.append(f"**Not established by the sources fetched**: {missing} -- named without a source showing the mechanism; verify before relying on them")
    if not lines:
        return synthesized
    head, sep, tail = synthesized.partition("\n\n---\n\n")
    return head.rstrip() + "\n\n" + "\n".join(lines) + (sep + tail if sep else "")


_URL = re.compile(r"https?://[^\s)\]]+")
_SOURCE_LINE = re.compile(r"(?m)^\s*\[(\d{1,3})\]\s+\[([^\]]{3,160})\]\((https?://[^)\s]+)\)")


def _cited_source_leads(cards: str) -> list[tuple[str, str]]:
    """Cited pages to inspect when the model supplies no candidate names.

    Only numbered URLs present in the actual evidence are eligible. A search
    card's unnumbered URL or a model-invented URL never becomes a page read.
    """
    leads: list[tuple[str, str]] = []
    for card in cards.split("\n\n---\n\n"):
        section = re.split(r"(?m)^\s*Sources:\s*$", card, maxsplit=1)
        if len(section) != 2:
            continue
        body, sources = section
        cited = {int(n) for n in re.findall(r"\[(\d{1,3})\]", body)}
        for number, title, url in _SOURCE_LINE.findall(sources):
            if int(number) in cited and url not in {u for _, u in leads}:
                leads.append((title.strip(), url))
    return leads


def _cited_source_context(cards: str) -> dict[str, str]:
    """Nearby search prose guides page selection; only page reads prove facts."""
    context: dict[str, str] = {}
    for card in cards.split("\n\n---\n\n"):
        section = re.split(r"(?m)^\s*Sources:\s*$", card, maxsplit=1)
        if len(section) != 2:
            continue
        body, sources = section
        for number, _, url in _SOURCE_LINE.findall(sources):
            marker = re.search(r"\[" + re.escape(number) + r"\]", body)
            if marker:
                snippet = re.sub(r"\s+", " ", body[max(0, marker.start() - 220):marker.end() + 80]).strip()
                if len(snippet) > len(context.get(url, "")):
                    context[url] = snippet
    return context


def _verbatim_passage(quote: str, page: str) -> bool:
    """A verdict cannot be verified by an invented or paraphrased quotation."""
    compact = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
    passage = compact(quote.strip().strip('"\'“”‘’'))
    return len(passage) >= 20 and "..." not in passage and passage in compact(page)


def _clean_quote(quote: str) -> str:
    return re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", quote).strip().strip('"\'“”‘’')


def _validated_condition_map(raw: str, conditions: str, pages: list[dict]) -> list[dict] | None:
    """Validate every supplied condition-to-page passage, including partial maps."""
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(items, list):
        return None
    needed = len([part for part in conditions.split(";") if part.strip()]) or 1
    seen: set[int] = set()
    passages = []
    by_url = {page["url"]: page for page in pages}
    for item in items:
        if not isinstance(item, dict) or type(item.get("condition")) is not int:
            return None
        number, url, quote = item["condition"], item.get("url"), _clean_quote(str(item.get("quote") or ""))
        page = by_url.get(url)
        if number < 1 or number > needed or number in seen or not page or not _verbatim_passage(quote, page["text"][:7000]):
            return None
        seen.add(number)
        passages.append({"condition": number, "url": url, "quote": quote, "fetched_at": page.get("fetched_at", "")})
    return passages


def _mapped_passages(raw: str, conditions: str, pages: list[dict]) -> list[dict] | None:
    """A qualifying verdict needs a validated passage for every condition."""
    passages = _validated_condition_map(raw, conditions, pages)
    needed = len([part for part in conditions.split(";") if part.strip()]) or 1
    return passages if passages is not None and {p["condition"] for p in passages} == set(range(1, needed + 1)) else None


def _missing_conditions(conditions: str, verdict: dict) -> str:
    """Aim the next read at an unmet fact, not the whole original question."""
    parts = [part.strip() for part in conditions.split(";") if part.strip()]
    covered = set(verdict.get("supported_condition_indices") or [])
    if covered:
        missing = [part for index, part in enumerate(parts, 1) if index not in covered]
        if missing:
            return "; ".join(missing)
    failed = (verdict.get("conditions_failed") or "").strip()
    if failed and failed.lower() != "none":
        return failed
    met = {part.strip().casefold() for part in (verdict.get("conditions_met") or "").split(";") if part.strip()}
    missing = [part.strip() for part in conditions.split(";") if part.strip() and part.strip().casefold() not in met]
    return "; ".join(missing) or conditions


def _source_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return ".".join(host.split(".")[-2:])


def _candidate_first_party_url(name: str, cited: list[tuple[str, str]]) -> str | None:
    """Prefer a candidate's own cited page over an aggregator's cited page."""
    key = re.sub(r"[^a-z0-9]", "", re.split(r"\s+[—–-]\s+|\s*/\s*", name, maxsplit=1)[0].lower())[:12]
    if len(key) < 4:
        return None
    return next((url for _, url in cited if key in (_source_domain(url).split(".")[0]).replace("-", "")), None)


def _group_focused_candidates(question: str, candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge claim rows about the same protocol named in the question."""
    question_key = re.sub(r"[^a-z0-9]", "", question.lower())
    grouped: list[tuple[str, str]] = []
    focused_domains: set[str] = set()
    for name, url in candidates:
        domain = _source_domain(url)
        root = domain.split(".")[0]
        if len(root) >= 4 and root in question_key:
            if domain in focused_domains:
                continue
            focused_domains.add(domain)
            grouped.append((root.capitalize(), url))
        else:
            grouped.append((name, url))
    return grouped


def read_page(url: str) -> dict:
    """Discovery proposes URLs; inspection reads them. The page itself first
    (title, text, fetched-at: passage-level provenance), then one bounded
    alternative reader with weaker provenance (a search-index view), else
    unreadable -- a lead, never verified evidence (the brief, step 3).
    Module-level so replay and tests can supply pages."""
    from datetime import datetime, timezone
    from app import perplexity_tools, url_reader
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if url_reader.resolve_public(url) is None:
        return {"url": url, "title": "", "text": "", "provenance": "unreadable", "fetched_at": fetched_at, "note": "not a public destination"}
    got = url_reader.fetch(url)
    if got:
        title, text = got
        return {"url": url, "title": title, "text": text, "provenance": "page", "fetched_at": fetched_at}
    if perplexity_tools.perplexity_available():
        try:
            text = perplexity_tools.perplexity_fetch_url(url)
            if text and "cannot be accessed" not in text.lower():
                return {"url": url, "title": "", "text": text, "provenance": "reader", "fetched_at": fetched_at}
        except Exception:
            logger.info("reader fallback failed for %s", url[:120], exc_info=True)
    return {"url": url, "title": "", "text": "", "provenance": "unreadable", "fetched_at": fetched_at}


async def _inspect_candidates(question: str, constraints: str, cards: str, runtime, router,
                              page_cache: dict[str, dict] | None = None,
                              followup_reads: int | None = None,
                              budget: TurnBudget | None = None,
                              check_cache: dict[tuple, dict] | None = None,
                              page_limit: int | None = None) -> tuple[list[dict] | None, list[str]]:
    """Read the cited page of each candidate the cards name and check it
    against the conditions; bounded by `research_loop_inspect_pages`.
    (verdicts, the calls made): verdicts is None when the candidate listing
    could not run, which withholds the answer; [] when the cards discuss no
    candidate."""
    calls: list[str] = []
    page_cache = page_cache if page_cache is not None else {}
    check_cache = check_cache if check_cache is not None else {}
    limit = int(getattr(settings, "research_loop_inspect_pages", 4) or 0)
    if page_limit:
        limit = min(limit, page_limit)
    if limit <= 0 or not cards.strip():
        return [], calls
    streaming.research_progress("review", "Identifying cited pages that can answer the question")
    conditions = constraints if constraints and constraints.lower() != "none" else question
    try:
        listed = await asyncio.wait_for(runtime._call_research_loop_lm(_candidates_program, question=question, evidence=_compact(cards)), timeout=45)
    except Exception:
        logger.info("research loop: candidate listing failed", exc_info=True)
        return None, calls
    # The safety boundary on what is read: a URL the evidence cards cite, or
    # one a first-party search returned; never a URL the model wrote on its
    # own (review, 2026-09-24). The reader refuses non-public destinations.
    cited = {u.rstrip(".,;)") for u in _URL.findall(cards)}
    candidates: list[tuple[str, str]] = []
    named_only: list[str] = []
    for line in (getattr(listed, "candidates", "") or "").splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        if not line or line.lower() == "none":
            continue
        name, _, url = (part.strip() for part in line.partition("|"))
        found = _URL.search(url or "")
        chosen = found.group(0).rstrip(".,;") if found else None
        if name and chosen and chosen in cited:
            candidates.append((name, chosen))
        elif name and name.lower() != "none":
            if chosen:
                logger.info("research loop: candidate URL not in the cited sources, looked up instead: %s", chosen[:120])
            named_only.append(name)                     # no cited URL for it: its page comes from a first-party search's results
    cited_leads = _cited_source_leads(cards)
    cited_context = _cited_source_context(cards)
    candidates = [(name, _candidate_first_party_url(name, cited_leads) or url) for name, url in candidates]
    # Do this before and after named-only URLs are resolved: several claim
    # rows can otherwise spend separate searches and page slots on one site.
    candidates = _group_focused_candidates(question, candidates)
    initial_cap = min((3 if followup_reads else 2) if budget else 4, max(1, limit - 2))
    if not candidates and not named_only:
        candidates = _cited_source_leads(cards)
    all_cited = candidates[:]
    candidates = candidates[:initial_cap]
    attempted_named = named_only[: max(0, initial_cap - len(candidates))]
    for name in attempted_named:
        try:
            url = await (budget.call("provider", asyncio.to_thread(first_party_url, name, conditions), 30, reserve=25)
                         if budget else asyncio.to_thread(first_party_url, name, conditions))
        except TimeoutError:
            url = None
        calls.append(f"first_party_search (perplexity_web_search): {name}")
        if url:
            candidates.append((name, url))
    candidates = _group_focused_candidates(question, candidates)
    listed_all = _group_focused_candidates(question, all_cited + candidates) + [(n, "") for n in named_only[len(attempted_named):]]
    # A candidate the budget leaves unread is not established, explicitly:
    # the gate then covers a claim about it (review of f603a871).
    inspected_names = {n for n, _ in candidates}
    uninspected = [{"name": n, "url": u, "verdict": "not_established", "provenance": "uninspected", "fetched_at": "",
                    "conditions_met": "none", "conditions_failed": "none", "quote": "none", "note": "not inspected: page budget reached; a search lead, not verified evidence"}
                   for n, u in listed_all if n not in inspected_names]
    if not candidates:
        logger.info("research loop: no candidates listed from the cards")
        return uninspected, calls
    streaming.emit("status", text=f"Reading {len(candidates)} cited page{'s' if len(candidates) != 1 else ''} to check the candidates")
    streaming.research_progress("review", f"Reading {len(candidates)} cited page{'s' if len(candidates) != 1 else ''} against the requested conditions")

    async def check(name: str, url: str, prior: dict | None = None) -> dict:
        cache_key = (name, url, tuple(p.get("url") for p in (prior.get("_pages", []) if prior else [])))
        if cache_key in check_cache:
            return check_cache[cache_key].copy()
        base = {"name": name, "url": url, "conditions_met": "none", "conditions_failed": "none", "quote": "none"}
        cached = url in page_cache
        if cached:
            page = page_cache[url]
        else:
            try:
                streaming.research_progress("review", f"Opening the cited page at {_source_domain(url)}")
                page = await (budget.call("page", asyncio.to_thread(read_page, url), 40, reserve=20)
                              if budget else asyncio.wait_for(asyncio.to_thread(read_page, url), timeout=40))
            except Exception as exc:
                page = {"url": url, "title": "", "text": "", "provenance": "unreadable", "fetched_at": "", "note": type(exc).__name__}
            page_cache[url] = page
        pages = [*(prior.get("_pages", []) if prior else []), page]
        direct = [p for p in pages if p.get("provenance") == "page" and p.get("text")]
        base.update(provenance=page["provenance"], title=page.get("title", ""), fetched_at=page.get("fetched_at", ""), _pages=pages)
        if not cached:
            calls.append(f"page_read ({'url_reader' if page['provenance'] == 'page' else 'perplexity_fetch_url' if page['provenance'] == 'reader' else 'unreadable'}): {url}")
        if not direct:
            return {**base, "verdict": "not_established", "note": page.get("note") or "page unreadable: a search lead, not verified evidence"}
        # A mechanism may be documented across complementary first-party
        # pages. Keep the source boundary explicit; never promote reader text.
        page_text = "\n\n".join(f"Source: {p['url']}\n{p['text'][:7000]}" for p in direct)
        try:
            # Keep every fetched page in the check. The former 22k cap cut
            # off the fourth source just when a later page supplied the
            # missing transfer condition; the answer then withheld despite
            # having read the decisive first-party document.
            result = await asyncio.wait_for(runtime._call_research_loop_lm(_check_program, question=question, conditions=conditions, candidate=name, page=page_text[:85000]), timeout=45)
        except Exception as exc:
            return {**base, "verdict": "not_established", "note": f"check failed ({type(exc).__name__})"}
        verdict = (getattr(result, "verdict", "") or "not_established").strip().strip('"').lower().replace(" ", "_")
        if verdict not in ("qualifies", "related_but_different", "not_established"):
            verdict = "not_established"
        quote = _clean_quote((getattr(result, "quote", "") or "none"))
        raw_passages = [quote, *((getattr(result, "supporting_passages", "") or "").splitlines())]
        passages = []
        for raw in raw_passages:
            clean = _clean_quote(raw)
            if not clean or clean.lower() == "none":
                continue
            source = next((p for p in direct if _verbatim_passage(clean, p["text"][:7000])), None)
            if source and clean not in {item["quote"] for item in passages}:
                passages.append({"url": source["url"], "quote": clean, "fetched_at": source.get("fetched_at", "")})
        raw_map = getattr(result, "evidence_map", None)
        validated = _validated_condition_map(raw_map, conditions, direct) if raw_map is not None else None
        if validated is not None:
            base["supported_condition_indices"] = [item["condition"] for item in validated]
            base["condition_evidence"] = validated
        mapped = _mapped_passages(raw_map, conditions, direct) if raw_map is not None else None
        if verdict == "qualifies" and raw_map is not None and mapped is None:
            return {**base, "verdict": "not_established",
                    "conditions_met": (getattr(result, "conditions_met", "") or "none").strip(),
                    "conditions_failed": (getattr(result, "conditions_failed", "") or "none").strip(),
                    "quote": quote, "passages": passages,
                    "note": "not every qualifying condition has a validated page passage"}
        if mapped:
            passages = list({(item["url"], item["quote"]): item for item in mapped}.values())
            quote = passages[0]["quote"]
        if verdict == "qualifies" and mapped:
            try:
                audit = await asyncio.wait_for(runtime._call_research_loop_lm(
                    _condition_audit_program, conditions=conditions,
                    evidence_map=json.dumps(mapped, ensure_ascii=False)), timeout=45)
                raw_unsupported = (getattr(audit, "unsupported_conditions", "") or "").strip().lower()
                if raw_unsupported == "none":
                    unsupported_indices: list[int] = []
                else:
                    tokens = [part.strip() for part in raw_unsupported.split(",")]
                    if not tokens or any(not token.isdigit() for token in tokens):
                        raise ValueError("invalid condition audit")
                    unsupported_indices = [int(token) for token in tokens]
                    if any(index < 1 or index > len(mapped) for index in unsupported_indices):
                        raise ValueError("invalid condition index")
            except Exception:
                unsupported_indices = [item["condition"] for item in mapped]
            if unsupported_indices:
                parts = [part.strip() for part in conditions.split(";") if part.strip()]
                return {**base, "verdict": "not_established", "conditions_met": "none",
                        "conditions_failed": "; ".join(parts[index - 1] for index in unsupported_indices),
                        "quote": quote, "passages": passages,
                        "supported_condition_indices": [index for index in base.get("supported_condition_indices", []) if index not in unsupported_indices],
                        "condition_evidence": [row for row in base.get("condition_evidence", []) if row["condition"] not in unsupported_indices],
                        "note": "independent quote-to-condition check rejected the claimed support"}
        quoted_page = next((p for p in direct if _verbatim_passage(quote, p["text"][:7000])), None)
        if verdict != "not_established" and quoted_page is None:
            return {**base, "verdict": "not_established", "conditions_met": "none", "conditions_failed": "none", "quote": "none",
                    "note": "no direct page passage verifies the claimed relationship"}
        if quoted_page:
            base.update(url=quoted_page["url"], provenance="page", fetched_at=quoted_page.get("fetched_at", ""))
        accepted = {**base, "verdict": verdict, "conditions_met": (getattr(result, "conditions_met", "") or "none").strip(),
                    "conditions_failed": (getattr(result, "conditions_failed", "") or "none").strip(), "quote": quote,
                    "passages": passages}
        check_cache[cache_key] = accepted
        return accepted.copy()

    verdicts = list(await asyncio.gather(*(check(n, u) for n, u in candidates)))
    # A candidate whose cited page does not settle the conditions gets one
    # more read: a search for its first-party documentation of the mechanism,
    # then that page is checked (the search's URL was a blog or a deposit
    # guide, not the mechanism page, live 2026-09-24). Bounded by the page limit.
    remaining = max(0, limit - len(candidates))
    if followup_reads is not None:
        remaining = min(remaining, max(0, followup_reads))
    attempts: dict[str, int] = {}
    for _ in range(min(7, limit, remaining)):
        unresolved = [v for v in verdicts if v["verdict"] == "not_established" and not v.get("_followup_exhausted")]
        # Sequential reads preserve the chance to complete a relationship
        # spread across several original pages. Rotate after an attempt so a
        # weak early candidate cannot consume every remaining read.
        unresolved = sorted(unresolved, key=lambda v: (-len(v.get("condition_evidence") or []),
                                                       attempts.get(v["name"], 0),
                                                       -sum(p.get("provenance") == "page" for p in v.get("_pages", []))))[:1]
        if not unresolved:
            break
        streaming.research_progress("review", f"Looking for original documentation on {', '.join(v['name'] for v in unresolved)}")
        streaming.emit("status", text=f"Looking for first-party documentation of {', '.join(v['name'] for v in unresolved)}")

        async def follow_up(v: dict) -> tuple[dict, bool]:
            target = _missing_conditions(conditions, v)
            already = {p["url"] for p in v.get("_pages", [])}
            # Search already returned dated, cited source URLs. Ask which of
            # those addresses the missing fact, then verify the choice against
            # the allowlist before fetching it. Only search again if the cited
            # set contains no usable page for this candidate.
            domain = _source_domain(v["url"])
            leads = [(title, source_url) for title, source_url in _cited_source_leads(cards)
                     if source_url not in already and domain and _source_domain(source_url) == domain][:20]
            url = None
            if leads:
                try:
                    picked = await asyncio.wait_for(runtime._call_research_loop_lm(
                        _followup_program, candidate=v["name"], missing_condition=target,
                        sources="\n".join(f"{title} | {source_url} | search context: {cited_context.get(source_url, '')}"
                                          for title, source_url in leads)), timeout=30)
                    proposed = (getattr(picked, "url", "") or "").strip()
                    if proposed in {source_url for _, source_url in leads}:
                        url = proposed
                        calls.append(f"cited_source_selection: {v['name']} → {url}")
                except Exception:
                    logger.info("research loop: cited follow-up selection failed for %s", v["name"], exc_info=True)
            if not url:
                try:
                    url = await (budget.call("provider", asyncio.to_thread(first_party_url, v["name"], target, already), 30, reserve=20)
                                 if budget else asyncio.to_thread(first_party_url, v["name"], target, already))
                except TimeoutError:
                    url = None
                calls.append(f"first_party_search (perplexity_web_search): {v['name']}")
            if not url or url in already:
                return {**v, "_followup_exhausted": True}, False
            again = await check(v["name"], url, v)
            again["note"] = "first-party page found by a follow-up search" + (f"; the cited page ({v['url']}) did not settle it" if again["verdict"] != "not_established" else "")
            result = again if again["verdict"] != "not_established" else {**again, "note": (v.get("note") or "not settled by the cited page") + f"; a follow-up read of {url} did not settle it either"}
            return result, True

        replaced = await asyncio.gather(*(follow_up(v) for v in unresolved))
        for v in unresolved:
            attempts[v["name"]] = attempts.get(v["name"], 0) + 1
        remaining -= sum(read for _, read in replaced)
        by_name = {r["name"]: r for r, _ in replaced}
        verdicts = [by_name.get(v["name"], v) for v in verdicts]
    verdicts = [{k: value for k, value in v.items() if k not in ("_pages", "_followup_exhausted")} for v in verdicts] + uninspected
    logger.info("research loop: inspected %d candidates: %s", len(verdicts), [(v["name"], v["verdict"], v.get("provenance")) for v in verdicts])
    return verdicts, calls


def first_party_url(name: str, conditions: str, exclude: set[str] | None = None) -> str | None:
    """The URL of a candidate's own documentation of the mechanism, from one
    discovery search; None when the search returns nothing that looks
    first-party. Rank the returned first-party URLs by the unresolved fact,
    not provider order: a generic staking page often precedes the page about
    the transfer or redemption condition that prompted this follow-up."""
    from app import perplexity_tools
    if not perplexity_tools.perplexity_available():
        return None
    exclude = exclude or set()
    try:
        found = perplexity_tools.perplexity_search_with_sources(f"{name} official documentation: {conditions[:300]}")
    except Exception:
        logger.info("first-party search failed for %s", name, exc_info=True)
        return None
    key = re.sub(r"[^a-z0-9]", "", name.lower())[:8]
    stop = {"first", "party", "official", "source", "sources", "document", "documentation", "protocol",
            "historical", "current", "condition", "conditions", "token", "tokens", "whether", "which", "against"}
    wanted = {word[:5] for word in re.findall(r"[a-z]{5,}", conditions.lower()) if word not in stop}
    ranked: list[tuple[int, int, str]] = []
    for source in found.get("sources") or []:
        url = source.get("url") or ""
        host = re.sub(r"^https?://", "", url).split("/")[0].lower()
        path = urlsplit(url).path
        if url not in exclude and key and (key in host.replace("-", "").replace(".", "")
                                       or (host.startswith("docs.") and key in re.sub(r"[^a-z0-9]", "", path.lower()))):
            title = str(source.get("title") or "")
            subject = (title + " " + path).lower()
            terms = {word[:5] for word in re.findall(r"[a-z]{5,}", subject)}
            score = len(wanted & terms)
            # More specific paths break ties against a site home page.
            ranked.append((score, len(urlsplit(url).path.strip("/")), url))
    return max(ranked)[2] if ranked else None


def _with_trail(answer: str, made: list[str], skipped: list[str], verdicts: list[dict], timings: dict | None = None) -> str:
    """The research trail under the summary: what was read and why, how
    each candidate fared against the conditions from its own page, and
    where the time went."""
    lines = ["**Research trail**"]
    if timings:
        lines.append("- time: " + " · ".join(f"{k} {v:g}s" for k, v in timings.items()))
    for call in made:
        lines.append(f"- {call}")
    for v in verdicts:
        why = {"qualifies": "qualifies: " + (v.get("conditions_met") or ""), "related_but_different": "related but a different design: " + (v.get("conditions_failed") or ""),
               "not_established": "not established by its page" + (f" ({v['note']})" if v.get("note") else "")}[v["verdict"]]
        quote = f' -- "{v["quote"]}"' if v.get("quote") and v["quote"].lower() != "none" else ""
        prov = {"page": "page read", "reader": "search-index view, weaker provenance", "unreadable": "page unreadable", "uninspected": "not inspected"}.get(v.get("provenance"), "")
        when = f", {v['fetched_at']}" if v.get("fetched_at") else ""
        lines.append(f"- {v['name']} ({v['url']}; {prov}{when}): {why}{quote}")
    head, sep, tail = answer.partition("\n\n---\n\n")
    return head.rstrip() + "\n\n" + "\n".join(lines) + (sep + tail if sep else "")

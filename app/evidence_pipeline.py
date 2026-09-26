"""Plan the evidence, gather it, prove coverage, then answer.

The replacement for "rank tools, accept the first usable response" for the
four contract kinds where the Home run recorded failures: market rankings,
holders, recent events and yields. One statement of the ask (the question
contract) drives tool eligibility, fact normalisation and the fact gate;
the writer sees only cards whose facts passed, and the gate names every
gap and every figure it could not trace.

Bounded on purpose: at most three tool calls, one repair call, one
synthesis, one check. Latency is spent only on a named uncertainty.
"""
from __future__ import annotations

import asyncio
import re
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import composition, contracts, evidence, fact_gate, facts as facts_mod, perplexity_tools, research_loop, streaming, tool_catalog
from app.provider_registry import get_provider_router
from app.settings import settings

logger = logging.getLogger(__name__)

MAX_TOOLS = 3
EXACT_STATE_KINDS = ("market_ranking", "holders", "yields", "portfolio")     # figures must come from a state card, never a web card


def _contract_note(contract: contracts.QuestionContract, gate: fact_gate.GateResult, fact_rows: list[facts_mod.Fact], scope_note: str | None) -> str:
    if contract.kind == contracts.OPEN_RESEARCH_KIND:
        # A research brief, not the contract's internals: a stronger model
        # follows the note as written and led with "the requested window is
        # '-'" and "25 event-and-source facts" (the Sol runs, 2026-09-24).
        lines = ["Brief for the answer: answer the question above in service of the conversation's research objective when one is stated; "
                 "organise by mechanism, not by source; keep the [n] markers next to the claims they support; name a project or example only "
                 "with the source that describes it; apply the question's constraints strictly -- list a project as meeting a mechanism only "
                 "when the source shows it does, and name the candidates the sources show do not meet it as such; give each figure its source "
                 "and date; say plainly what the sources do not settle, in one sentence, and never fill it from memory. Do not mention this "
                 "brief, any contract, window or fact count."]
        if gate.missing:
            lines.append("Say first that the sources leave open: " + "; ".join(gate.missing))
        if contract.answer_requirements:
            lines.append("Resolve these points from cited sources, or say which remain unverified: " + "; ".join(contract.answer_requirements))
        return "\n".join(lines)
    hours = contract.window_hours
    window = "-" if not hours else (f"{hours / 24:g} days" if hours >= 48 else f"{hours:g} hours")     # "24 days" was written up as "the last 24 hours" (2026-09-24)
    lines = [f"Question contract: {contract.label()} · metric {contract.metric or '-'} · scope {contract.scope} · window asked: {window} "
             f"(write the window as asked, '{window}', never as 24 hours when days were asked; when a card covers less than that, say the covered span and that it is less) · filters {contract.filters or '{}'}",
             f"Accepted facts: {len(fact_rows)} ({', '.join(sorted({f.kind for f in fact_rows})) or 'none'}).",
             "Instructions for the answer: state only figures present in the cards; name the source and its time for each; keep the [n] "
             "source markers next to the claims they support when the cards carry them; if the cards do not satisfy the contract, say exactly "
             "what is missing in the first sentence, and never fill it from memory."]
    if scope_note:
        lines.append(f"Scope: {scope_note}")
    if contract.answer_requirements:
        lines.append("Resolve these points from cited sources, or say which remain unverified: " + "; ".join(contract.answer_requirements))
    if gate.missing:
        lines.append("Gaps to state first: " + "; ".join(gate.missing))
    if gate.stale:
        lines.append("Stale sources to name: " + "; ".join(gate.stale))
    return "\n".join(lines)


# Capitalised names (not tickers: the subject checks cover those), one or two words.
_NAME_LIKE = re.compile(r"\b([A-Z][a-z][A-Za-z0-9]+(?:\s+[A-Z][a-z][A-Za-z0-9]+)?)\b")
_NOT_NAMES = {"The", "This", "That", "These", "Those", "However", "Additionally", "Other", "Another", "Overall", "Finally", "First", "Second", "Third",
              "Taken", "Together", "According", "Source", "Sources", "Not", "None", "Evidence", "Yes", "No", "Per", "For", "In", "On", "At", "With",
              "Both", "Each", "Most", "Some", "Many", "Several", "Unlike", "Like", "Also", "Instead", "Meanwhile", "Note", "Summary"}


_OBJECTIVE_LINE = re.compile(r"^Research objective of this conversation:\s*(.+?)\.?\s*Answer the current question", re.I | re.M)


def research_query(request: str) -> str:
    """What the web search is asked: the question itself with the
    conversation's objective as a short context, or the market-scoped
    question when there is none. An instruction block under the question
    brought back "no information" on questions the same search answered
    plainly (the dual-token runs, 2026-09-24)."""
    from app.routing.subject_probe import market_scoped
    body, notes = composition.split_notes(request)
    rolling = re.search(r"\b(?:last|past|previous)\s+(\d{1,3})\s*(?:h|hours?)\b", body or "", re.I)
    as_of = (f" As of {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}, test event times against the exact previous {rolling.group(1)} hours; publication time is separate."
             if rolling else "")
    prior_items = re.search(r'"those" are the items the previous answer listed -- (.*?) -- answer about exactly these', notes or "", re.S)
    if prior_items:
        return f"{market_scoped(body)} (Verify only these previously listed items in order: {prior_items.group(1)}. Do not replace them with new items.{as_of})"
    objective = _OBJECTIVE_LINE.search(notes or "")
    if objective:
        # Every line the user wrote is the question ("Only include transferable
        # tokens" / "Exclude wrapped assets" on later lines had been dropped,
        # review of c03378b1); only the notes fold into the context.
        question = " ".join(line.strip() for line in (body or request).splitlines() if line.strip())
        return f"{question} (Context: this continues research on {objective.group(1).strip()}; answer for that context, not the general case.{as_of})"
    if contracts.is_schedule_request(body or request):
        return " ".join(line.strip() for line in (body or request).splitlines() if line.strip()) + as_of
    if (contracts.is_direct_explanation(body or request)
            and not re.search(r"\b\$?[A-Z][A-Z0-9]{1,9}\b", body or request)):
        # A term in ordinary prose is not an asset ticker. Preserve the
        # question and its conversation context; market_scoped() would turn
        # "max pain" into a search for a token named MAX.
        return request
    # "PUMP revenue" came back as ProPetro (NYSE: PUMP): the ticker reads as a crypto asset first.
    return market_scoped(request) + as_of


def scheduled_time_notice(request: str, cards: str, *, now: datetime | None = None) -> str:
    """Prevent a dated search snapshot from presenting a passed deadline as upcoming."""
    if not re.search(r"\b(?:today|this morning|this afternoon)\b", request, re.I):
        return ""
    if not re.search(r"\b(?:expir(?:e|es|ed|ing|y)|settle[ds]?|release[ds]?|launch(?:es|ed)?|scheduled)\b", request, re.I):
        return ""
    times = set(re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*UTC\b", cards, re.I))
    asked_times = set(re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*UTC\b", request, re.I))
    selected = asked_times & times if asked_times else times
    if len(selected) != 1:
        return ""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_today = now.astimezone().date()
    if (local_today.isoformat() not in cards
            and local_today.strftime("%B %d, %Y").replace(" 0", " ") not in cards):
        return ""
    today_candidates = [now.astimezone(timezone.utc).date() + timedelta(days=shift) for shift in (-1, 0, 1)]
    hour, minute = map(int, next(iter(selected)))
    matched = [datetime.combine(day, datetime.min.time(), timezone.utc).replace(hour=hour, minute=minute)
               for day in today_candidates if datetime.combine(day, datetime.min.time(), timezone.utc).replace(hour=hour, minute=minute).astimezone().date() == local_today]
    if len(matched) != 1 or now <= matched[0]:
        return ""
    at = matched[0].strftime("%H:%M UTC")
    return (f"**Timing:** The cited {at} scheduled time was earlier today. A pre-event notional or forecast is historical context; "
            "these sources do not establish the final settled amount or make the event an upcoming volatility catalyst.")


def unsourced_examples(summary: str, cards: str) -> list[str]:
    """Capitalised names the summary uses that appear nowhere in the cards:
    examples named from general knowledge, not from the evidence fetched."""
    haystack = (cards or "").lower()
    out: list[str] = []
    for m in _NAME_LIKE.finditer(summary or ""):
        name = m.group(1)
        words = name.split()
        if words[0] in _NOT_NAMES:
            name = " ".join(words[1:])
            if not name or name in _NOT_NAMES:
                continue
        if name.lower() in haystack:
            continue                     # the full name, never its first word ("Nova Labs" is not sourced by "Nova market index", review of c03378b1)
        if name not in out:
            out.append(name)
    return out[:8]


def _near_tools(contract: contracts.QuestionContract, ranked: list[tuple[str, str]]) -> list[str]:
    """Tools that fail the contract on scope or venue ONLY: the honest
    fallback, shown as such ("I can only verify the global ranking"). A tool
    that also fails the metric or the window is not near; it is wrong."""
    relaxed = contract.model_copy(update={"scope": "any", "venue": None})
    # A source bound to particular venues (the Tequity feed: Aster and
    # Hyperliquid) is not near an ask about another venue: Binance movers
    # ran the Hyperliquid ledger and reported "nothing usable" (2026-09-24).
    return [name for name, reason in ranked if reason != "eligible" and tool_catalog.eligible(name, relaxed)[0]
            and not (contract.venue and (tool_catalog.CONTRACT_COVERAGE.get(name) or {}).get("venues"))]


# A headline about policy: the social read joins the market pulse, because
# the market's reaction to a rule is the thing the tap asks about.
_POLICY_HEADLINE = re.compile(r"\b(?:SEC|CFTC|Fed|Federal\s+Reserve|FOMC|Treasury|Congress|Senate|House|White\s+House|regulat\w*|rules?|law|bill|act|ban|bans|"
                              r"sanction\w*|tariff\w*|court|ruling|lawsuit|subpoena|approv\w+|licen[cs]\w*|GENIUS|MiCA|ETF)\b")


_HEADLINE_STOP = {"market", "markets", "crypto", "stocks", "today", "level", "since", "highest", "lowest", "tougher", "stays", "volatile",
                  "proposes", "rules", "after", "amid", "while", "against", "record", "hits", "falls", "rises", "pressuring", "surges", "drops"}


def _about_headline(headline: str, card: str) -> bool:
    """Whether a market-wide social card is about this headline: at least one
    of the headline's distinctive words (five letters or more, not market
    filler) appears in the card."""
    words = {w.lower() for w in re.findall(r"[A-Za-z]{5,}", headline or "")} - _HEADLINE_STOP
    text = (card or "").lower()
    return any(w in text for w in words)


async def _attach_market_state(router, request: str, chains: tuple[str, ...], contract, parts: list, enabled: dict) -> tuple[list, str]:
    """A Home headline tap asks what a story means for the market: the
    market's state now is a card of its own (majors, total cap, fear and
    greed, DEX volume, TVL), and for a policy headline the market-wide
    social read joins it, so the written read is grounded in state and not
    in the story's prose alone (2026-09-25: Minara read the majors and
    sentiment before answering; the tap read the story only)."""
    from app import market_overview
    attached: list[str] = []
    try:
        pulse = await asyncio.wait_for(asyncio.to_thread(market_overview.market_pulse_card), timeout=20)
    except Exception:
        logger.info("headline tap: market pulse unavailable", exc_info=True)
        pulse = ""
    if pulse:
        streaming.emit("card", markdown=pulse, tool="market_pulse")
        parts.append((pulse, {"tool_name_0": "market_pulse", "tool_args_0": {"request": request}, "observation_0": pulse}))
        attached.append("the market pulse card")
    headline = composition.split_notes(request)[0].split(":", 1)[-1]
    if _POLICY_HEADLINE.search(headline):
        # The social tool is a router tool, not a contract-coverage entry; an unknown or disabled tool returns None from _invoke.
        # Its read is market-wide: it joins only when it is about the headline (live 2026-09-26: the Fed stablecoin tap got
        # posts about PAID and XPL unlocks, and the summary repeated them). Off-topic posts are dropped, never shown.
        result = await _invoke(router, "x_social_trending", request, chains, contract)
        if result is not None and result.output and _about_headline(headline, result.output):
            streaming.emit("card", markdown=result.output, tool=result.tool)
            parts.append((result.output, {"tool_name_0": result.tool, "tool_args_0": {"request": request}, "observation_0": result.output}))
            attached.append("the social read")
    if not attached:
        return parts, ""
    note = ("This is a Home headline tap. Say what the story is and when it happened (the event date from the web card, never its publication date), "
            "then what it means for the market grounded in " + " and ".join(attached) + ": state the majors' moves, total market cap and fear and greed "
            "exactly as the card states them and say whether they are consistent with the headline. Posts show what accounts said, never a fact. "
            "No prediction, no target, no advice.")
    return parts, note


async def _invoke(router, name: str, request: str, chains: tuple[str, ...], contract=None, *, search_options: dict | None = None):
    """One tool. The web discovery tool runs through the structured search so
    claims keep their [n] markers and numbered, dated sources; everything
    else through the router (quota, cache, breaker as usual)."""
    if name in ("perplexity_web_search", "perplexity_finance_search") and contract is not None and perplexity_tools.perplexity_available() and not getattr(router, "replay", False):
        try:
            # Open research without an explicit window includes historical
            # primary documents. A default 30-day search filter made the
            # decisive original protocol pages much harder to retrieve.
            days = (max(1, int((contract.window_hours or 24 * 30) / 24))
                    if contract.kind == "recent_events" else
                    max(1, int(contract.window_hours / 24))
                    if contract.kind == "open_research" and contract.window_hours else None)
            if search_options and search_options.get("recency_days"):
                days = search_options["recency_days"]
            finance = name == "perplexity_finance_search"
            query = research_query(request)
            domains = tuple((search_options or {}).get("domains") or ())
            domain_clause = " OR ".join(f"site:{domain}" for domain in domains)
            search_query = query + (f" ({domain_clause})" if domains else "")
            found = await asyncio.to_thread(perplexity_tools.perplexity_search_with_sources, search_query, recency_days=days, finance=finance)
            if domains:
                from urllib.parse import urlsplit
                found = {**found, "sources": [source for source in found.get("sources") or []
                         if any((host := (urlsplit(str(source.get("url") or "")).hostname or "").lower()) == domain
                                or host.endswith("." + domain) for domain in domains)]}
                if not found["sources"]:
                    return SimpleNamespace(output="", tool=name, provider="perplexity", structured=found, query=search_query)
            card = perplexity_tools.render_search_card(query, found, title="From the web (finance, dated, with sources)" if finance else "From the web (dated, with sources)")
            result = SimpleNamespace(output=card, tool=name, provider="perplexity", structured=found, query=search_query)
            return result
        except Exception:
            logger.info("structured web search failed; plain call", exc_info=True)
    for attempt in (0, 1):
        try:
            result = await asyncio.to_thread(router.invoke, name, request, chains)
        except KeyError:
            return None
        except Exception:
            logger.info("contract tool %s failed", name, exc_info=True)
            result = None
        if result is not None or getattr(router, "replay", False):
            return result
        await asyncio.sleep(1.5)                                          # a provider's per-second budget: one pause, one retry
    return None


_TICK_SPAN = re.compile(r"\*\*From tick\*\*: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC · \*\*To tick\*\*: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC")
_COVERAGE_HOURS = re.compile(r"the means below cover ([\d.]+) hours")


def _covered_hours(cards: str) -> float | None:
    """How many hours the ledger cards actually cover, from their own From/To
    ticks or Coverage line; None when no card states a span."""
    spans = []
    for a, b in _TICK_SPAN.findall(cards or ""):
        try:
            spans.append((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600)
        except ValueError:
            continue
    spans += [float(h) for h in _COVERAGE_HOURS.findall(cards or "")]
    return max(spans) if spans else None


def _facts_of(result, kind: str) -> list:
    """The facts a tool's result carries. A fact read live from a state
    source that stamps no time is as fresh as the call that just fetched it,
    so it gets the fetch time; a passage from the knowledge base or a source
    with no date stays undated, and the gate treats undated as not current."""
    found = getattr(result, "structured", None)
    if found:
        return facts_mod.facts_from_search(found, result.tool)
    rows = facts_mod.facts_from_card(result.tool, result.output, kind)
    cov = tool_catalog.CONTRACT_COVERAGE.get(result.tool) or {}
    if not cov.get("discovery") and not cov.get("background"):
        fetched = datetime.now(timezone.utc).isoformat()
        for f in rows:
            if not f.observed_at:
                f.observed_at = fetched
    return rows


def prove(contract: contracts.QuestionContract, answer_text: str, cards: list[str], *, wallet: str | None = None, extra_evidence: str = "") -> dict:
    """The gate over an answer a node wrote itself (the connected wallet's
    holdings, a swap quote): facts read from the node's own cards, the
    requirement checked, the prose's figures traced to the cards, and the
    written part withheld when a figure traces to nothing. Returns the
    fields a contract answer carries; the caller merges them into its result."""
    if wallet and contract.subject.id is None:
        contract = contract.model_copy(update={"subject": contract.subject.model_copy(update={"id": wallet})})
    evidence_text = "\n\n---\n\n".join(c for c in cards if c)
    fact_rows = []
    for card in cards:
        fact_rows.extend(facts_mod.facts_from_card("node_card", card, contract.kind))
    fetched = datetime.now(timezone.utc).isoformat()
    for f in fact_rows:
        if not f.observed_at:
            f.observed_at = fetched                                  # the node fetched these live in this turn
    # `extra_evidence` is data the node fetched but does not show as a card (the
    # snapshot's exact figures behind a rounded table); it supports the prose
    # and is never displayed.
    gate = fact_gate.check(contract, fact_rows, answer_text, evidence_text=evidence_text + ("\n\n" + extra_evidence if extra_evidence else ""))
    text = answer_text
    lead = gate.gap_sentence(contract)
    if gate.unsupported:
        text = ("**I withheld the written summary: it stated figures no card carries (" + ", ".join(gate.unsupported) +
                "). The cards below are the evidence as fetched.**\n\n---\n\n" + evidence_text)
    elif not gate.ok:
        # A requirement the cards do not meet (the holdings shown are another
        # wallet's, no quote row): the written answer is withheld, never shown
        # under a warning (review of 360c88f9: "**Your portfolio** holds $100"
        # stood unchanged over a wrong-wallet card).
        text = f"**{lead or 'The cards do not establish what was asked.'}** The written answer is withheld; the cards below are the evidence as fetched.\n\n---\n\n" + evidence_text
    elif lead:
        text = f"**{lead}**\n\n{text}"
    return {"answer": text, "contract": contract.model_dump(), "gate": gate.model_dump(), "facts": [f.label() for f in fact_rows[:40]], "pipeline": "contract"}


async def answer(state: dict, request: str, chains: tuple[str, ...], *, context: str = "") -> dict | None:
    """The contract-pipeline answer, or None when the ask is not one of the
    four kinds (the legacy path answers it)."""
    if not settings.contract_pipeline_enabled:
        return None
    contract = await contracts.plan(request, context)
    if contract.kind == contracts.OPEN_RESEARCH_KIND and not settings.discovery_first_research:
        return None
    if contract.kind not in contracts.CONTRACT_KINDS and contract.kind != contracts.OPEN_RESEARCH_KIND:
        return None
    if contract.ambiguity:
        return {"answer": contract.ambiguity, "trajectory": None, "contract": contract.model_dump(), "pipeline": "contract"}
    streaming.research_progress("plan", "Matching the question to evidence that covers its scope")
    router = get_provider_router()
    if (contract.kind == contracts.OPEN_RESEARCH_KIND and contract.research_depth == "deep"
            and research_loop.enabled() and not contracts.is_schedule_request(request)):
        # The bounded evidence loop (docs/engineering/claude-code-evidence-loop.md);
        # None when its plan fails, and the fixed sequence below answers.
        looped = await research_loop.run(state, request, contract, router, chains)
        if looped is not None:
            return looped
    enabled = {t.name: t for t in router.tools() if router._enabled(t)} if hasattr(router, "_enabled") else {t.name: t for t in router.tools()}
    ranked = tool_catalog.eligible_tools(contract)
    eligible = [name for name, reason in ranked if reason == "eligible" and name in enabled]
    cov = tool_catalog.CONTRACT_COVERAGE
    state_tools = [n for n in eligible if not cov[n].get("discovery") and not cov[n].get("background")]
    discovery_tools = [n for n in eligible if cov[n].get("discovery")]
    if contract.filters.get("stocks_only"):
        # A stock ask leads with the finance-tuned search; otherwise the finance search never leads.
        discovery_tools = [n for n in discovery_tools if cov[n].get("stocks")] + [n for n in discovery_tools if not cov[n].get("stocks")]
    else:
        discovery_tools = [n for n in discovery_tools if not cov[n].get("stocks")]
    background_tools = [n for n in eligible if cov[n].get("background")]
    # Among the eligible, the catalogue's fit and the tool's own priority order them.
    def rank(name: str) -> float:
        tool = enabled[name]
        spec = getattr(tool, "spec", None)
        return (spec.fit(request) if spec else 0.0) + float(getattr(tool, "priority", 0.0)) / 100.0 + (1.0 if tool.matches(request) else 0.0)
    state_tools.sort(key=rank, reverse=True)
    if contract.kind == contracts.OPEN_RESEARCH_KIND:
        if contracts.is_schedule_request(request) or (contract.window_hours is not None and contract.window_hours <= 168):
            # A factual schedule or time-bounded market event needs current
            # reporting, not an unrelated protocol knowledge-base passage.
            chosen = discovery_tools[:1]
        else:
            # Web first, then up to two targeted tools the router would plan for
            # this ask (holders, security, market data), then the check.
            planned = []
            try:
                planned = [t.name for t in await asyncio.to_thread(router.plan_across, request, ("market_data", "token_security", "token_discovery", "defi_data", "knowledge"), chains, 2)]
            except Exception:
                logger.info("open research: router plan failed", exc_info=True)
            # Open research is discovery: the web and the knowledge base. A token
            # tool joins only when the ask names a contract address -- "research
            # projects with two tokens like Venice VVV and DIEM" had led with
            # DIEM's trading pools, and irrelevant evidence cannot be repaired.
            chosen = discovery_tools[:1] + [n for n in planned if n not in discovery_tools and (contract.subject.id or n == "knowledge_base_search")][:2]
    elif contract.evidence_order == "discovery_first":
        if contract.kind == "recent_events" and not contract.subject.id:
            # A dated report about a named asset is not an on-chain event for
            # a resolved contract. Search current reporting without adding
            # unrelated token-state or protocol-index passages.
            chosen = discovery_tools[:1]
        else:
            chosen = discovery_tools[:1] + state_tools[:1] + background_tools[:1]
    else:
        # One state source, plus a second only when it covers different ground
        # (another scope), never two readings of the same feed.
        chosen = state_tools[:1]
        for name in state_tools[1:]:
            if cov[name]["scope"] != cov[chosen[0]]["scope"]:
                chosen.append(name)
                break
        chosen += discovery_tools[:1] if contract.kind == "recent_events" else []
    chosen = chosen[:MAX_TOOLS]
    scope_satisfied, scope_note = True, None
    if not chosen:
        near = [n for n in _near_tools(contract, ranked) if n in enabled]
        near.sort(key=rank, reverse=True)
        if not near:
            covered_but_down = [name for name, reason in ranked if reason == "eligible" and name not in enabled]
            covered_but_down += [name for name in _near_tools(contract, ranked) if name not in enabled and name not in covered_but_down]   # the nearest source, paused (CoinGecko during the fifth frozen run)
            if covered_but_down:
                # A source covers this; it is paused right now (a provider's
                # rate limit or breaker). Say that, not "nothing covers this"
                # (PEPE holders during a Mobula cooldown read as uncovered,
                # frozen run 2026-09-24).
                gate = fact_gate.check(contract, [], scope_satisfied=True)
                gate.missing = [f"the source that covers this is unavailable right now ({', '.join(covered_but_down[:3])})"]
                gate.ok = False
                text = (f"The source that covers this ({', '.join(covered_but_down[:3])}) is unavailable right now, usually a provider's rate limit "
                        f"or a short outage, so I have nothing verified for {contract.describe()}. Ask again in a minute; I will not answer it from the web.")
                return {"answer": text, "trajectory": None, "contract": contract.model_dump(), "gate": gate.model_dump(), "pipeline": "contract"}
            gate = fact_gate.check(contract, [], scope_satisfied=False)
            if contract.kind == "holders":
                text = (f"I cannot verify {contract.describe()} with the available holder-data providers. "
                        "No holder source was queried, so I cannot identify or rank the wallets for this token. "
                        "A contract on a supported chain is a different asset; I will not substitute its holders.")
            else:
                text = (f"No source I have covers this exactly ({contract.describe()}). " + gate.gap_sentence(contract) +
                        " Name a venue or chain I do cover, or ask for the global ranking instead.")
            return {"answer": text, "trajectory": None, "contract": contract.model_dump(), "gate": gate.model_dump(), "pipeline": "contract"}
        chosen = near[:1]
        scope_satisfied = False
        cov_scope = cov[chosen[0]]["scope"]
        scope_note = (f"the ask needs {contract.scope.replace('_', ' ')}; the only source available is {chosen[0]} whose scope is {cov_scope.replace('_', ' ')} "
                      f"-- shown as the nearest verifiable ranking, not as the ask itself")
    calls = [(name, request) for name in chosen]
    source_action = ("Searching current reporting" if discovery_tools and any(name in discovery_tools for name, _ in calls)
                     else "Reading live market and on-chain data")
    streaming.research_progress("sources", f"{source_action} for this question")
    streaming.emit("status", text=f"Contract: {contract.label()} · tools: {', '.join(n for n, _ in calls)}")
    results = await asyncio.gather(*(_invoke(router, name, text, chains, contract) for name, text in calls))
    chosen = [name for name, _ in calls]
    parts, fact_rows = [], []
    for name, result in zip(chosen, results):
        if result is None or not result.output:
            continue
        streaming.emit("card", markdown=result.output, tool=result.tool)
        parts.append((result.output, {"tool_name_0": result.tool, "tool_args_0": {"request": request}, "observation_0": result.output}))
        fact_rows.extend(_facts_of(result, contract.kind))
    streaming.research_progress("sources", f"Received {len(parts)} evidence card{'s' if len(parts) != 1 else ''}", found=len(parts))
    if not parts:
        # The chosen source returned nothing usable: the next eligible source
        # answers instead (Mobula had no ANSEM holders three times over while
        # the Solana RPC read was eligible, 2026-09-24).
        for name in sorted([n for n, reason in ranked if reason == "eligible" and n in enabled and n not in chosen], key=rank, reverse=True):
            result = await _invoke(router, name, request, chains, contract)
            chosen = chosen + [name]
            if result is not None and result.output:
                streaming.emit("card", markdown=result.output, tool=result.tool)
                parts.append((result.output, {"tool_name_0": result.tool, "tool_args_0": {"request": request}, "observation_0": result.output}))
                fact_rows.extend(_facts_of(result, contract.kind))
                break
    streaming.research_progress("review", "Checking source coverage and the required facts", found=len(parts))
    gate = fact_gate.check(contract, fact_rows, scope_satisfied=scope_satisfied)
    if not gate.ok and gate.missing and discovery_tools and not any(n in chosen for n in discovery_tools):
        # One repair: a discovery tool for the gap the state tools left.
        repair = discovery_tools[0]
        result = await _invoke(router, repair, f"{request} -- {'; '.join(gate.missing)}", chains, contract)
        if result is not None and result.output:
            streaming.emit("card", markdown=result.output, tool=result.tool)
            parts.append((result.output, {"tool_name_0": result.tool, "tool_args_0": {"request": request}, "observation_0": result.output}))
            fact_rows.extend(_facts_of(result, contract.kind))
            gate = fact_gate.check(contract, fact_rows, scope_satisfied=scope_satisfied)
            chosen = chosen + [repair]
    if not parts:
        gate = fact_gate.check(contract, [], scope_satisfied=scope_satisfied)
        text = f"The sources for this ({', '.join(chosen)}) returned nothing usable right now. " + gate.gap_sentence(contract)
        return {"answer": text.strip(), "trajectory": None, "contract": contract.model_dump(), "gate": gate.model_dump(), "pipeline": "contract"}
    tap_note = ""
    if composition.is_headline_tap(request):
        parts, tap_note = await _attach_market_state(router, request, chains, contract, parts, enabled)
    cards, trajectory = composition.combine(parts)
    covered = _covered_hours(cards)
    if covered is not None and contract.window_hours and covered < 0.8 * contract.window_hours:
        # The cards cover less of the window than was asked: said first, in
        # numbers, whatever the summary calls it (a 5h40 ledger span was
        # written up as "24 hours", expanded UI review 2026-09-24).
        asked = f"{contract.window_hours / 24:g} days" if contract.window_hours >= 48 else f"{contract.window_hours:g} hours"
        gate.notes.append(f"covers {covered:.1f} hours of the {asked} asked")
        scope_note = (scope_note + "; " if scope_note else "") + f"the cards cover {covered:.1f} hours of the {asked} asked, say so"
    elif covered is not None and contract.window_hours and covered > 1.25 * contract.window_hours:
        # The cards span more than the window asked (a 102-minute tick span
        # presented as the hourly change, review of bc40f724): the span is
        # said first and the change is never called the window's.
        asked = f"{contract.window_hours / 24:g} days" if contract.window_hours >= 48 else f"{contract.window_hours:g} hours"
        gate.notes.append(f"spans {covered:.1f} hours for the {asked} asked (nearest stored ticks)")
        scope_note = (scope_note + "; " if scope_note else "") + f"the cards span {covered:.1f} hours for the {asked} asked, say the span and never call the change a {asked} change"
    note = _contract_note(contract, gate, fact_rows, scope_note)
    if tap_note:
        note += "\n" + tap_note
    timing = scheduled_time_notice(request, cards)
    if timing:
        note += "\n" + timing + " Use past tense for the scheduled time and do not describe it as upcoming."
    if contract.kind == contracts.OPEN_RESEARCH_KIND and contract.research_depth == "direct":
        note += "\nGive a short, plain-language answer to the one concept asked. Do not expand into unrelated figures or a broader market report."
    research = contract.kind == contracts.OPEN_RESEARCH_KIND
    streaming.research_progress("answer", "Writing from the evidence collected", found=len(parts))
    synthesized = await composition.synthesize(f"{request}\n{note}", cards, trajectory, research=research)
    if research and contract.research_depth == "deep":
        unsourced = unsourced_examples(synthesized, cards)
        if unsourced:
            # A named example the cards do not carry is general knowledge, said as such (never silently dropped, never passed as sourced).
            synthesized = synthesized.rstrip() + "\n\n**Not in the sources fetched**: " + ", ".join(unsourced) + " -- named from general knowledge; verify before relying on them."

    lead = gate.gap_sentence(contract)
    # For an exact-state kind (holders, rankings, yields) a discovery card is
    # context, never a source of figures: ten web-derived holder percentages
    # were written under a line saying no holder source was queried (live UI
    # test 2026-09-25). The figure check reads state cards only.
    # Exact-state kinds take their figures from state cards only (a web card
    # is context); an events or open-research answer is discovery by design,
    # so its web card is its evidence (regression run 2026-09-25: "What
    # happened to Solana in the last 24 hours" was withheld over figures its
    # own web card carried, two runs of three).
    state_cards = ("\n\n".join(text for text, traj in parts if not (cov.get(traj.get("tool_name_0")) or {}).get("discovery"))
                   if contract.kind in EXACT_STATE_KINDS else cards)
    final = fact_gate.check(contract, fact_rows, synthesized, scope_satisfied=scope_satisfied, evidence_text=state_cards)
    if final.unsupported or final.contradictions:
        # The claim check found figures no fact carries, or a comparison the
        # numbers deny ("7,719 below a prior 7,706"): one rewrite without them,
        # then the check again; whatever remains withholds the summary.
        redo = f"{request}\n{note}\n"
        if final.unsupported:
            redo += f"Do not state these figures, no fact card carries them: {', '.join(final.unsupported)}. State only figures that appear in the cards, or describe without the number. "
        if final.contradictions:
            redo += f"These comparisons contradict their own numbers, rewrite them correctly or drop them: {'; '.join(final.contradictions)}."
        rewritten = await composition.synthesize(redo, cards, trajectory)
        if rewritten:
            again = fact_gate.check(contract, fact_rows, rewritten, scope_satisfied=scope_satisfied, evidence_text=state_cards)
            if len(again.unsupported) + len(again.contradictions) < len(final.unsupported) + len(final.contradictions):
                synthesized, final = rewritten, again
    answer_text = synthesized
    if final.unsupported or final.contradictions:
        # Still untraceable or self-contradicting after the rewrite: the
        # written summary is withheld, never shown with a warning under it
        # (second review, 2026-09-23). The cards are the evidence as fetched;
        # the reader gets those and the reason, and the gate stays failed.
        reasons = ([f"it stated figures no source card carries ({', '.join(final.unsupported)})"] if final.unsupported else []) + \
                  ([f"it compared numbers wrongly ({'; '.join(final.contradictions)})"] if final.contradictions else [])
        answer_text = ("**I withheld the written summary: " + " and ".join(reasons) +
                       ". The cards below are the evidence as fetched; ask for one figure and I will read it from a card.**\n\n---\n\n" + cards)
    if lead:
        answer_text = f"**{lead}**\n\n{answer_text}"
    if timing and not answer_text.startswith(timing):
        answer_text = timing + "\n\n" + answer_text
    evidence.keep(evidence.Evidence(tool="contract_pipeline", status="complete" if final.ok else "partial",
                                    subject={"kind": contract.subject.kind, "id": contract.subject.id, "chain": contract.subject.chain, "symbol": contract.subject.symbol},
                                    data={"contract": contract.model_dump(), "facts": len(fact_rows), "tools": chosen, "scope_satisfied": scope_satisfied},
                                    sources=[{"provider": "orbit", "endpoint": "contract_pipeline", "as_of": datetime.now(timezone.utc).isoformat()}],
                                    coverage={"attempted": len(chosen), "successful": len(parts), "missing": list(final.missing)}))
    return {"answer": answer_text, "trajectory": {"thought_0": f"Contract {contract.label()}: {', '.join(chosen)} gathered, {len(fact_rows)} facts, gate {'ok' if final.ok else 'gaps'}.", **trajectory},
            "contract": contract.model_dump(), "gate": final.model_dump(), "facts": [f.label() for f in fact_rows[:40]], "pipeline": "contract"}

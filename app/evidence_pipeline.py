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
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from app import composition, contracts, evidence, fact_gate, facts as facts_mod, perplexity_tools, streaming, tool_catalog
from app.provider_registry import get_provider_router
from app.settings import settings

logger = logging.getLogger(__name__)

MAX_TOOLS = 3


def _contract_note(contract: contracts.QuestionContract, gate: fact_gate.GateResult, fact_rows: list[facts_mod.Fact], scope_note: str | None) -> str:
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
    if gate.missing:
        lines.append("Gaps to state first: " + "; ".join(gate.missing))
    if gate.stale:
        lines.append("Stale sources to name: " + "; ".join(gate.stale))
    return "\n".join(lines)


def _near_tools(contract: contracts.QuestionContract, ranked: list[tuple[str, str]]) -> list[str]:
    """Tools that fail the contract on scope or venue ONLY: the honest
    fallback, shown as such ("I can only verify the global ranking"). A tool
    that also fails the metric or the window is not near; it is wrong."""
    relaxed = contract.model_copy(update={"scope": "any", "venue": None})
    return [name for name, reason in ranked if reason != "eligible" and tool_catalog.eligible(name, relaxed)[0]]


async def _invoke(router, name: str, request: str, chains: tuple[str, ...], contract=None):
    """One tool. The web discovery tool runs through the structured search so
    claims keep their [n] markers and numbered, dated sources; everything
    else through the router (quota, cache, breaker as usual)."""
    if name == "perplexity_web_search" and contract is not None and perplexity_tools.perplexity_available() and not getattr(router, "replay", False):
        try:
            days = int((contract.window_hours or 24 * 30) / 24) or 1 if contract.kind in ("recent_events", "open_research") else None
            found = await asyncio.to_thread(perplexity_tools.perplexity_search_with_sources, request, recency_days=days)
            card = perplexity_tools.render_search_card(request, found)
            result = SimpleNamespace(output=card, tool=name, provider="perplexity", structured=found)
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
    router = get_provider_router()
    enabled = {t.name: t for t in router.tools() if router._enabled(t)} if hasattr(router, "_enabled") else {t.name: t for t in router.tools()}
    ranked = tool_catalog.eligible_tools(contract)
    eligible = [name for name, reason in ranked if reason == "eligible" and name in enabled]
    cov = tool_catalog.CONTRACT_COVERAGE
    state_tools = [n for n in eligible if not cov[n].get("discovery") and not cov[n].get("background")]
    discovery_tools = [n for n in eligible if cov[n].get("discovery")]
    background_tools = [n for n in eligible if cov[n].get("background")]
    # Among the eligible, the catalogue's fit and the tool's own priority order them.
    def rank(name: str) -> float:
        tool = enabled[name]
        spec = getattr(tool, "spec", None)
        return (spec.fit(request) if spec else 0.0) + float(getattr(tool, "priority", 0.0)) / 100.0 + (1.0 if tool.matches(request) else 0.0)
    state_tools.sort(key=rank, reverse=True)
    if contract.kind == contracts.OPEN_RESEARCH_KIND:
        # Web first, then up to two targeted tools the router would plan for
        # this ask (holders, security, market data), then the check.
        planned = []
        try:
            planned = [t.name for t in await asyncio.to_thread(router.plan_across, request, ("market_data", "token_security", "token_discovery", "defi_data", "knowledge"), chains, 2)]
        except Exception:
            logger.info("open research: router plan failed", exc_info=True)
        chosen = discovery_tools[:1] + [n for n in planned if n not in discovery_tools][:2]
    elif contract.evidence_order == "discovery_first":
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
                        f"or a short outage, so I have nothing verified for {contract.label()}. Ask again in a minute; I will not answer it from the web.")
                return {"answer": text, "trajectory": None, "contract": contract.model_dump(), "gate": gate.model_dump(), "pipeline": "contract"}
            gate = fact_gate.check(contract, [], scope_satisfied=False)
            text = (f"No source I have covers this exactly ({contract.label()}). " + gate.gap_sentence(contract) +
                    " Name a venue or chain I do cover, or ask for the global ranking instead.")
            return {"answer": text, "trajectory": None, "contract": contract.model_dump(), "gate": gate.model_dump(), "pipeline": "contract"}
        chosen = near[:1]
        scope_satisfied = False
        cov_scope = cov[chosen[0]]["scope"]
        scope_note = (f"the ask needs {contract.scope.replace('_', ' ')}; the only source available is {chosen[0]} whose scope is {cov_scope.replace('_', ' ')} "
                      f"-- shown as the nearest verifiable ranking, not as the ask itself")
    streaming.emit("status", text=f"Contract: {contract.label()} · tools: {', '.join(chosen)}")
    results = await asyncio.gather(*(_invoke(router, name, request, chains, contract) for name in chosen))
    parts, fact_rows = [], []
    for name, result in zip(chosen, results):
        if result is None or not result.output:
            continue
        streaming.emit("card", markdown=result.output, tool=result.tool)
        parts.append((result.output, {"tool_name_0": result.tool, "tool_args_0": {"request": request}, "observation_0": result.output}))
        fact_rows.extend(_facts_of(result, contract.kind))
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
    cards, trajectory = composition.combine(parts)
    note = _contract_note(contract, gate, fact_rows, scope_note)
    synthesized = await composition.synthesize(f"{request}\n{note}", cards, trajectory)
    lead = gate.gap_sentence(contract)
    final = fact_gate.check(contract, fact_rows, synthesized, scope_satisfied=scope_satisfied, evidence_text=cards)
    if final.unsupported:
        # The claim check found figures no fact carries: one rewrite without
        # them, then the check again; whatever remains is named under the answer.
        redo = (f"{request}\n{note}\nDo not state these figures, no fact card carries them: {', '.join(final.unsupported)}. "
                "State only figures that appear in the cards, or describe without the number.")
        rewritten = await composition.synthesize(redo, cards, trajectory)
        if rewritten:
            again = fact_gate.check(contract, fact_rows, rewritten, scope_satisfied=scope_satisfied, evidence_text=cards)
            if len(again.unsupported) < len(final.unsupported):
                synthesized, final = rewritten, again
    answer_text = synthesized
    if final.unsupported:
        # Still untraceable after the rewrite: the written summary is withheld,
        # never shown with a warning under it (second review, 2026-09-23). The
        # cards are the evidence as fetched; the reader gets those and the
        # reason, and the gate stays failed.
        answer_text = ("**I withheld the written summary: it stated figures no source card carries (" + ", ".join(final.unsupported) +
                       "). The cards below are the evidence as fetched; ask for one figure and I will read it from a card.**\n\n---\n\n" + cards)
    if lead:
        answer_text = f"**{lead}**\n\n{answer_text}"
    evidence.keep(evidence.Evidence(tool="contract_pipeline", status="complete" if final.ok else "partial",
                                    subject={"kind": contract.subject.kind, "id": contract.subject.id, "chain": contract.subject.chain, "symbol": contract.subject.symbol},
                                    data={"contract": contract.model_dump(), "facts": len(fact_rows), "tools": chosen, "scope_satisfied": scope_satisfied},
                                    sources=[{"provider": "orbit", "endpoint": "contract_pipeline", "as_of": datetime.now(timezone.utc).isoformat()}],
                                    coverage={"attempted": len(chosen), "successful": len(parts), "missing": list(final.missing)}))
    return {"answer": answer_text, "trajectory": {"thought_0": f"Contract {contract.label()}: {', '.join(chosen)} gathered, {len(fact_rows)} facts, gate {'ok' if final.ok else 'gaps'}.", **trajectory},
            "contract": contract.model_dump(), "gate": final.model_dump(), "facts": [f.label() for f in fact_rows[:40]], "pipeline": "contract"}

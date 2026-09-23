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

from app import composition, contracts, evidence, fact_gate, facts as facts_mod, streaming, tool_catalog
from app.provider_registry import get_provider_router
from app.settings import settings

logger = logging.getLogger(__name__)

MAX_TOOLS = 3


def _contract_note(contract: contracts.QuestionContract, gate: fact_gate.GateResult, fact_rows: list[facts_mod.Fact], scope_note: str | None) -> str:
    lines = [f"Question contract: {contract.label()} · metric {contract.metric or '-'} · scope {contract.scope} · window {contract.window_hours or '-'}h · filters {contract.filters or '{}'}",
             f"Accepted facts: {len(fact_rows)} ({', '.join(sorted({f.kind for f in fact_rows})) or 'none'}).",
             "Instructions for the answer: state only figures present in the cards; name the source and its time for each; "
             "if the cards do not satisfy the contract, say exactly what is missing in the first sentence, and never fill it from memory."]
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


async def _invoke(router, name: str, request: str, chains: tuple[str, ...]):
    try:
        return await asyncio.to_thread(router.invoke, name, request, chains)
    except KeyError:
        return None
    except Exception:
        logger.info("contract tool %s failed", name, exc_info=True)
        return None


async def answer(state: dict, request: str, chains: tuple[str, ...], *, context: str = "") -> dict | None:
    """The contract-pipeline answer, or None when the ask is not one of the
    four kinds (the legacy path answers it)."""
    if not settings.contract_pipeline_enabled:
        return None
    contract = await contracts.plan(request, context)
    if contract.kind not in contracts.CONTRACT_KINDS:
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
    if contract.evidence_order == "discovery_first":
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
    results = await asyncio.gather(*(_invoke(router, name, request, chains) for name in chosen))
    parts, fact_rows = [], []
    for name, result in zip(chosen, results):
        if result is None or not result.output:
            continue
        streaming.emit("card", markdown=result.output, tool=result.tool)
        parts.append((result.output, {"tool_name_0": result.tool, "tool_args_0": {"request": request}, "observation_0": result.output}))
        fact_rows.extend(facts_mod.facts_from_card(result.tool, result.output, contract.kind))
    gate = fact_gate.check(contract, fact_rows, scope_satisfied=scope_satisfied)
    if not gate.ok and gate.missing and discovery_tools and not any(n in chosen for n in discovery_tools):
        # One repair: a discovery tool for the gap the state tools left.
        repair = discovery_tools[0]
        result = await _invoke(router, repair, f"{request} -- {'; '.join(gate.missing)}", chains)
        if result is not None and result.output:
            streaming.emit("card", markdown=result.output, tool=result.tool)
            parts.append((result.output, {"tool_name_0": result.tool, "tool_args_0": {"request": request}, "observation_0": result.output}))
            fact_rows.extend(facts_mod.facts_from_card(result.tool, result.output, contract.kind))
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
    final = fact_gate.check(contract, fact_rows, synthesized, scope_satisfied=scope_satisfied)
    answer_text = synthesized
    if lead:
        answer_text = f"**{lead}**\n\n{answer_text}"
    if final.unsupported:
        answer_text += "\n\n_Figures in the summary I could not trace to a source card: " + ", ".join(final.unsupported) + "._"
    evidence.keep(evidence.Evidence(tool="contract_pipeline", status="complete" if final.ok else "partial",
                                    subject={"kind": contract.subject.kind, "id": contract.subject.id, "chain": contract.subject.chain, "symbol": contract.subject.symbol},
                                    data={"contract": contract.model_dump(), "facts": len(fact_rows), "tools": chosen, "scope_satisfied": scope_satisfied},
                                    sources=[{"provider": "orbit", "endpoint": "contract_pipeline", "as_of": datetime.now(timezone.utc).isoformat()}],
                                    coverage={"attempted": len(chosen), "successful": len(parts), "missing": list(final.missing)}))
    return {"answer": answer_text, "trajectory": {"thought_0": f"Contract {contract.label()}: {', '.join(chosen)} gathered, {len(fact_rows)} facts, gate {'ok' if final.ok else 'gaps'}.", **trajectory},
            "contract": contract.model_dump(), "gate": final.model_dump(), "facts": [f.label() for f in fact_rows[:40]], "pipeline": "contract"}

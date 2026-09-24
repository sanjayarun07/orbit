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
import logging
import re
import time
from types import SimpleNamespace

import dspy

from app import composition, contracts, fact_gate, streaming, tool_catalog
from app.settings import settings

logger = logging.getLogger(__name__)

MAX_ROUNDS = 2          # the plan's round, then one gap review and its call (a third round put turns past 170 s live, 2026-09-24)
MAX_CALLS = 5           # provider calls per turn, whatever the model asks for
MAX_INITIAL = 2         # queries from the plan itself
MAX_MODEL_CALLS = 6     # plan + reviews + support check + one repair

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
    """Plan the evidence for one research turn. Read the request and the
    notes under it (the conversation's research objective, the resolved
    subject). State the subject, the user's actual question in one sentence,
    the conditions a correct answer must meet (each a short phrase), and the
    facts a supported answer needs (each a short phrase; three to six). The
    conditions are the user's stated constraints plus the ones a comparison
    implies: when the question asks for projects like a reference design,
    spell out each property of that design a match must share -- whose asset
    is the collateral (the project's own native token, not a third-party
    asset deposited by users), what is minted, whether the minted token is
    separately transferable, what redemption does -- so that receipt tokens,
    wrapped assets, stablecoins against external collateral and
    non-transferable credits fail a condition explicitly. Then shortlist
    the capabilities from the catalog that can supply those facts -- never by
    matching words, by the job each does -- and write the first search
    queries as natural questions, one per line in the form
    "capability: query", at most two. A follow-up in a conversation shares
    the topic but changes the facts needed: "how does it work in Akash" needs
    Akash's own mechanism; "which other projects use this exact mechanism"
    needs named projects with first-party sources. When the request needs a
    contract address the conversation has not resolved, say so in
    `constraints` rather than planning a state read."""

    request: str = dspy.InputField()
    catalog: str = dspy.InputField()
    subject: str = dspy.OutputField()
    question: str = dspy.OutputField()
    constraints: str = dspy.OutputField(desc="the conditions a match must meet, precise about whose asset and what is minted; semicolon-separated, or 'none'")
    required_facts: str = dspy.OutputField(desc="one per line")
    capabilities: str = dspy.OutputField(desc="comma-separated capability names from the catalog")
    queries: str = dspy.OutputField(desc='one per line, "capability: query"')


class GapReview(dspy.Signature):
    """After a round of retrieval, decide the next call. Read the facts the
    answer needs, the evidence gathered so far (cards with sources), and the
    calls already made. List the required facts still missing or only
    partly supported. Then either name one more call -- "capability: query",
    a different query from the ones made, aimed at the most important
    missing fact -- or write "stop" when the facts are supported or the
    remaining gap cannot be resolved by any capability in the catalog (say
    which in `reason`). Never repeat a query already made."""

    question: str = dspy.InputField()
    required_facts: str = dspy.InputField()
    evidence: str = dspy.InputField()
    calls_made: str = dspy.InputField()
    catalog: str = dspy.InputField()
    missing: str = dspy.OutputField(desc="one per line, or 'none'")
    next_call: str = dspy.OutputField(desc='"capability: query" or "stop"')
    reason: str = dspy.OutputField()


class ExampleSupport(dspy.Signature):
    """Check the named examples and relationships in a research answer
    against the evidence cards it cites. For each project, token or entity
    the answer names as an example of the mechanism or relationship the
    question asks about, decide from the cited cards only: supported (a
    source establishes that specific mechanism for it), related but a
    different design (the source shows a different mechanism), or not
    established (no cited source shows it). Names used only as contrasts or
    non-matches are not examples. Output each list comma-separated, or
    "none"."""

    question: str = dspy.InputField()
    answer: str = dspy.InputField()
    evidence: str = dspy.InputField()
    supported: str = dspy.OutputField()
    related_but_different: str = dspy.OutputField()
    not_established: str = dspy.OutputField()


class CandidateCheck(dspy.Signature):
    """Check one candidate against the explicit conditions of the question,
    from the text of its cited source page only. State for each condition
    whether the page shows it met, shows it not met, or does not state it,
    with a short quote where the page settles it. Read each condition
    literally: "the project's own token as collateral" is not met by a
    receipt for a third-party asset users deposit (liquid staking, wrapped
    assets, vault shares), and "separately transferable" is not met by a
    non-transferable credit. The verdict is "qualifies" only when the page
    shows every condition met; "related_but_different" when the page shows
    the candidate's design differs on a condition; "not_established" when
    the page does not settle the conditions. A page that merely mentions the
    candidate proves nothing."""

    question: str = dspy.InputField()
    conditions: str = dspy.InputField(desc="the conditions a match must meet, semicolon-separated")
    candidate: str = dspy.InputField()
    page: str = dspy.InputField(desc="the cited page's text, as fetched")
    verdict: str = dspy.OutputField(desc='"qualifies" | "related_but_different" | "not_established"')
    conditions_met: str = dspy.OutputField(desc="semicolon-separated, or 'none'")
    conditions_failed: str = dspy.OutputField(desc="semicolon-separated, or 'none'")
    quote: str = dspy.OutputField(desc="the sentence that settles it, or 'none'")


class CandidateList(dspy.Signature):
    """From the evidence cards, list every candidate the cards discuss for
    the mechanism or relationship asked about -- projects, tokens or
    entities named as examples, near-matches, proposals or partial matches;
    inspection decides, not this list -- each with the URL of the cited
    source that describes it, one per line as "name | url". Exclude only the
    reference project the question compares against. Output "none" only when
    the cards discuss no candidate at all."""

    question: str = dspy.InputField()
    evidence: str = dspy.InputField()
    candidates: str = dspy.OutputField(desc='one per line, "name | url", or "none"')


_plan_program = dspy.Predict(EvidencePlan)
_candidates_program = dspy.Predict(CandidateList)
_check_program = dspy.Predict(CandidateCheck)
_review_program = dspy.Predict(GapReview)
_support_program = dspy.Predict(ExampleSupport)


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


async def run(state: dict, request: str, contract: contracts.QuestionContract, router, chains: tuple[str, ...]) -> dict:
    """The loop, returning the pipeline's answer shape."""
    from app import evidence_pipeline
    from app.nodes import runtime

    enabled_tools = {t.name for t in router.tools() if (router._enabled(t) if hasattr(router, "_enabled") else True)}
    catalog = catalog_summary()
    body, notes = composition.split_notes(request)
    streaming.emit("status", text="Planning the evidence")
    timings: dict[str, float] = {}
    t_plan = time.monotonic()
    try:
        plan = await asyncio.wait_for(runtime._call_research_loop_lm(_plan_program, request=request, catalog=catalog), timeout=60)
    except Exception:
        logger.warning("research loop: plan failed; the pipeline's fixed sequence answers", exc_info=True)
        return None
    timings["plan"] = round(time.monotonic() - t_plan, 1)
    question = (getattr(plan, "question", "") or body.splitlines()[0] if body else request).strip()
    required = (getattr(plan, "required_facts", "") or "").strip()
    calls = _parse_calls(getattr(plan, "queries", ""))[:MAX_INITIAL] or [("web_discovery", question)]
    parts: list[tuple[str, dict]] = []
    fact_rows: list = []
    made: list[str] = []
    skipped: list[str] = []
    model_calls = 1
    started = time.monotonic()
    # The retrieval budget: the loop's own setting, capped so the whole turn
    # (plan, reads, synthesis, support check, objective update) fits the chat timeout.
    budget = min(float(getattr(settings, "research_loop_seconds", 75) or 75), max(30.0, float(getattr(settings, "chat_execution_timeout_seconds", 120) or 120) - 60.0))

    async def one(capability: str, query: str, tool: str):
        text = f"{query}\n{notes}" if notes else query
        streaming.emit("status", text=f"Reading {capability}: {query[:80]}")
        return await evidence_pipeline._invoke(router, tool, text, chains, contract)

    for round_no in range(MAX_ROUNDS):
        batch = []
        for capability, query in calls:
            if len(made) + len(batch) >= MAX_CALLS:
                break
            tool, reason = tool_for(capability, contract, enabled_tools)
            if tool is None:
                skipped.append(f"{capability}: {reason}")
                made.append(f"{capability}: skipped ({reason})")
                continue
            batch.append((capability, query, tool))
        # Independent eligible calls run together (the brief, step 3).
        results = await asyncio.gather(*(one(c, q, t) for c, q, t in batch), return_exceptions=True)
        for (capability, query, tool), result in zip(batch, results):
            made.append(f"{capability} ({tool}): {query}")
            if isinstance(result, Exception) or result is None or not result.output:
                continue
            streaming.emit("card", markdown=result.output, tool=result.tool)
            parts.append((result.output, {"tool_name_0": result.tool, "tool_args_0": {"request": query}, "observation_0": result.output}))
            fact_rows.extend(evidence_pipeline._facts_of(result, contract.kind))
        if len(made) >= MAX_CALLS or round_no == MAX_ROUNDS - 1 or model_calls >= MAX_MODEL_CALLS - 2 or time.monotonic() - started > budget:
            break
        cards_so_far, _ = composition.combine(parts) if parts else ("", {})
        try:
            model_calls += 1
            review = await asyncio.wait_for(runtime._call_research_loop_lm(_review_program, question=question, required_facts=required or "none stated",
                                                                       evidence=_compact(cards_so_far) or "nothing yet", calls_made="\n".join(made), catalog=catalog), timeout=60)
        except Exception:
            logger.info("research loop: gap review failed; stopping", exc_info=True)
            break
        nxt = (getattr(review, "next_call", "") or "stop").strip()
        calls = [c for c in _parse_calls(nxt)[:1] if c[1] not in {m.split(": ", 1)[-1] for m in made}]
        if not calls:
            break
    timings["retrieval"] = round(time.monotonic() - started - timings.get("plan", 0), 1)
    if not parts:
        gate = fact_gate.check(contract, [], scope_satisfied=True)
        text = "The sources for this returned nothing usable right now. " + gate.gap_sentence(contract) + ("\n\n" + "; ".join(skipped) if skipped else "")
        return {"answer": text.strip(), "trajectory": None, "contract": contract.model_dump(), "gate": gate.model_dump(), "pipeline": "research_loop"}
    cards, trajectory = composition.combine(parts)
    gate = fact_gate.check(contract, fact_rows, scope_satisfied=True)
    note = evidence_pipeline._contract_note(contract, gate, fact_rows, None)
    constraints = (getattr(plan, "constraints", "") or "").strip()
    if constraints and constraints.lower() != "none":
        note += f"\nConstraints the user stated, to apply strictly: {constraints}"
    # Source inspection (the brief, step 4; the user's increment 1): the
    # candidates the cards name are checked against the conditions from
    # their cited pages, not from the search snippet that mentioned them.
    t_inspect = time.monotonic()
    verdicts, inspect_calls = await _inspect_candidates(question, constraints, cards, runtime, router)
    made.extend(inspect_calls)                          # every page read and follow-up search counts as a provider call
    timings["inspect"] = round(time.monotonic() - t_inspect, 1)
    if verdicts is None:
        # The candidate check could not run: nothing named can be verified, so no written summary (review of 0b360f9e).
        timings["total"] = round(time.monotonic() - started, 1)
        withheld = _with_trail(_withheld("the candidate check could not run, so no named example is verified", cards), made, skipped, [], timings)
        trajectory = dict(trajectory or {})
        trajectory["research_loop"] = {"subject": getattr(plan, "subject", ""), "question": question, "constraints": constraints, "required_facts": required,
                                       "calls": made, "skipped": skipped, "verdicts": [], "timings": timings, "withheld": "candidate check failed"}
        return {"answer": withheld, "trajectory": trajectory, "contract": contract.model_dump(), "gate": gate.model_dump(), "pipeline": "research_loop"}
    if verdicts:
        note += "\nCandidates checked against the conditions from their cited pages (state these verdicts, never upgrade one): " + "; ".join(
            f"{v['name']}: {v['verdict']}" + (f" ({v['conditions_failed']})" if v["verdict"] == "related_but_different" and v.get("conditions_failed") not in (None, "", "none") else "")
            for v in verdicts)
    t_synth = time.monotonic()
    synthesized = await composition.synthesize(f"{request}\n{note}", cards, trajectory, research=True)
    timings["synthesis"] = round(time.monotonic() - t_synth, 1)
    final = fact_gate.check(contract, fact_rows, synthesized, scope_satisfied=True, evidence_text=cards)
    if final.unsupported or final.contradictions:
        # Repair once (the brief, step 4), then withhold what still fails.
        redo = f"{request}\n{note}\n"
        if final.unsupported:
            redo += f"Do not state these figures, no fact card carries them: {', '.join(final.unsupported)}. State only figures that appear in the cards, or describe without the number. "
        if final.contradictions:
            redo += f"These comparisons contradict their own numbers, rewrite them correctly or drop them: {'; '.join(final.contradictions)}."
        rewritten = await composition.synthesize(redo, cards, trajectory, research=True)
        if rewritten:
            again = fact_gate.check(contract, fact_rows, rewritten, scope_satisfied=True, evidence_text=cards)
            if len(again.unsupported) + len(again.contradictions) < len(final.unsupported) + len(final.contradictions):
                synthesized, final = rewritten, again
    if final.unsupported or final.contradictions:
        what = ("figures no source card carries (" + ", ".join(final.unsupported[:6]) + ")") if final.unsupported else ("a comparison the numbers deny (" + "; ".join(final.contradictions[:3]) + ")")
        synthesized = (f"**I withheld the written summary: it stated {what}. The cards below are the evidence as fetched; ask for one figure and I will read it from a card.**\n\n---\n\n{cards}")
    else:
        t_support = time.monotonic()
        synthesized = await _verify_examples(question, synthesized, cards, request, note, trajectory, runtime, verdicts)
        timings["support"] = round(time.monotonic() - t_support, 1)
    timings["total"] = round(time.monotonic() - started, 1)
    synthesized = _with_trail(synthesized, made, skipped, verdicts, timings)
    trajectory = dict(trajectory or {})
    trajectory["research_loop"] = {"subject": getattr(plan, "subject", ""), "question": question, "constraints": constraints, "required_facts": required,
                                   "calls": made, "skipped": skipped, "verdicts": verdicts, "timings": timings}
    logger.info("research loop record: %s", trajectory["research_loop"])
    return {"answer": synthesized, "trajectory": trajectory, "contract": contract.model_dump(), "gate": final.model_dump(), "pipeline": "research_loop"}


_LABELLED = re.compile(r"not established|different design|does not qualify|not a match|not verified|not confirmed|related but|no first-party|cannot be confirmed|not documented|unverified", re.I)


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
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", prose):
            if name.lower() in sentence.lower() and not _LABELLED.search(sentence):
                out.append(name)
                break
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
    if gated:
        missing = ", ".join(x for x in [missing, *gated] if x)
    if not related and not missing:
        return synthesized
    redo = (f"{request}\n{note}\nThe cited sources do not establish these names as examples of the mechanism -- "
            + (f"related but a different design: {related}; " if related else "") + (f"not established: {missing}; " if missing else "")
            + "state each of them only as such, or leave it out; never present it as a match.")
    rewritten = await composition.synthesize(redo, cards, trajectory, research=True)
    if rewritten and not unlabelled_names(rewritten, verdicts):
        again = await _support_check(question, rewritten, cards, runtime, verdicts)
        if again is not None and not again[0] and not again[1]:
            return rewritten
    return _withheld("it presented as examples names the cited sources do not establish (" + ", ".join(x for x in (related, missing) if x) + ")", cards)


def _withheld(reason: str, cards: str) -> str:
    return (f"**I withheld the written summary: {reason}. The cards below are the evidence as fetched; the research trail shows what was read "
            f"and how each candidate fared.**\n\n---\n\n{cards}")


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


async def _inspect_candidates(question: str, constraints: str, cards: str, runtime, router) -> tuple[list[dict] | None, list[str]]:
    """Read the cited page of each candidate the cards name and check it
    against the conditions; bounded by `research_loop_inspect_pages`.
    (verdicts, the calls made): verdicts is None when the candidate listing
    could not run, which withholds the answer; [] when the cards discuss no
    candidate."""
    calls: list[str] = []
    limit = int(getattr(settings, "research_loop_inspect_pages", 4) or 0)
    if limit <= 0 or not cards.strip():
        return [], calls
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
    initial_cap = max(1, limit - 2)                     # room for follow-up reads inside the same page budget
    candidates = candidates[:initial_cap]
    for name in named_only[: max(0, initial_cap - len(candidates))]:
        url = await asyncio.to_thread(first_party_url, name, conditions)
        calls.append(f"first_party_search (perplexity_web_search): {name}")
        if url:
            candidates.append((name, url))
    if not candidates:
        logger.info("research loop: no candidates listed from the cards")
        return [], calls
    streaming.emit("status", text=f"Reading {len(candidates)} cited page{'s' if len(candidates) != 1 else ''} to check the candidates")

    async def check(name: str, url: str) -> dict:
        base = {"name": name, "url": url, "conditions_met": "none", "conditions_failed": "none", "quote": "none"}
        try:
            page = await asyncio.wait_for(asyncio.to_thread(read_page, url), timeout=40)
        except Exception as exc:
            page = {"url": url, "title": "", "text": "", "provenance": "unreadable", "fetched_at": "", "note": type(exc).__name__}
        base.update(provenance=page["provenance"], title=page.get("title", ""), fetched_at=page.get("fetched_at", ""))
        calls.append(f"page_read ({'url_reader' if page['provenance'] == 'page' else 'perplexity_fetch_url' if page['provenance'] == 'reader' else 'unreadable'}): {url}")
        if page["provenance"] == "unreadable" or not page.get("text"):
            return {**base, "verdict": "not_established", "note": page.get("note") or "page unreadable: a search lead, not verified evidence"}
        try:
            result = await asyncio.wait_for(runtime._call_research_loop_lm(_check_program, question=question, conditions=conditions, candidate=name, page=page["text"][:6000]), timeout=45)
        except Exception as exc:
            return {**base, "verdict": "not_established", "note": f"check failed ({type(exc).__name__})"}
        verdict = (getattr(result, "verdict", "") or "not_established").strip().strip('"').lower().replace(" ", "_")
        if verdict not in ("qualifies", "related_but_different", "not_established"):
            verdict = "not_established"
        return {**base, "verdict": verdict, "conditions_met": (getattr(result, "conditions_met", "") or "none").strip(),
                "conditions_failed": (getattr(result, "conditions_failed", "") or "none").strip(), "quote": (getattr(result, "quote", "") or "none").strip()}

    verdicts = list(await asyncio.gather(*(check(n, u) for n, u in candidates)))
    # A candidate whose cited page does not settle the conditions gets one
    # more read: a search for its first-party documentation of the mechanism,
    # then that page is checked (the search's URL was a blog or a deposit
    # guide, not the mechanism page, live 2026-09-24). Bounded by the page limit.
    unresolved = [v for v in verdicts if v["verdict"] == "not_established"][: max(0, limit - len(candidates))]     # inside the page budget, never beyond it
    if unresolved:
        streaming.emit("status", text=f"Looking for first-party documentation of {', '.join(v['name'] for v in unresolved)}")

        async def follow_up(v: dict) -> dict:
            url = await asyncio.to_thread(first_party_url, v["name"], conditions)
            calls.append(f"first_party_search (perplexity_web_search): {v['name']}")
            if not url or url == v["url"]:
                return v
            again = await check(v["name"], url)
            again["note"] = "first-party page found by a follow-up search" + (f"; the cited page ({v['url']}) did not settle it" if again["verdict"] != "not_established" else "")
            return again if again["verdict"] != "not_established" else {**v, "note": (v.get("note") or "not settled by the cited page") + f"; a follow-up read of {url} did not settle it either"}

        replaced = await asyncio.gather(*(follow_up(v) for v in unresolved))
        by_name = {r["name"]: r for r in replaced}
        verdicts = [by_name.get(v["name"], v) for v in verdicts]
    logger.info("research loop: inspected %d candidates: %s", len(verdicts), [(v["name"], v["verdict"], v.get("provenance")) for v in verdicts])
    return verdicts, calls


def first_party_url(name: str, conditions: str) -> str | None:
    """The URL of a candidate's own documentation of the mechanism, from one
    discovery search; None when the search returns nothing that looks
    first-party (the candidate's name in the host, or a docs host)."""
    from app import perplexity_tools
    if not perplexity_tools.perplexity_available():
        return None
    try:
        found = perplexity_tools.perplexity_search_with_sources(f"{name} official documentation: {conditions[:300]}")
    except Exception:
        logger.info("first-party search failed for %s", name, exc_info=True)
        return None
    key = re.sub(r"[^a-z0-9]", "", name.lower())[:8]
    for source in found.get("sources") or []:
        url = source.get("url") or ""
        host = re.sub(r"^https?://", "", url).split("/")[0].lower()
        if key and (key in host.replace("-", "").replace(".", "") or host.startswith("docs.")):
            return url
    return None


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
        prov = {"page": "page read", "reader": "search-index view, weaker provenance", "unreadable": "page unreadable"}.get(v.get("provenance"), "")
        when = f", {v['fetched_at']}" if v.get("fetched_at") else ""
        lines.append(f"- {v['name']} ({v['url']}; {prov}{when}): {why}{quote}")
    head, sep, tail = answer.partition("\n\n---\n\n")
    return head.rstrip() + "\n\n" + "\n".join(lines) + (sep + tail if sep else "")

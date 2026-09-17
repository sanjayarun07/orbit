"""Routing evaluation harness for Orbit.

Two modes, one labeled dataset (cases.json):

- `router` (default): evaluates the DETERMINISTIC routing layer offline, fast and
  free. For each case it derives (intent, capabilities, chains) from Layer-1
  `route_capabilities`, then ranks the union of ProviderRouter candidates across
  those capabilities by the router's own `_score`. Reports top-1, recall@k, MRR,
  and forbidden@1 over that ranking. This is the surface you'd tune when changing
  regex weights, priorities, or (later) adding RRF/reranking -- run it before and
  after a change to prove the change helps and hurts nothing. Cases flagged
  `"router": false` (symbol resolution / interceptors / execution intents that
  live in research_node above the router) are skipped here.

- `chat`: end-to-end ground truth through the running /chat server -- captures the
  full path (interceptors, symbol resolution, ASK behavior, the LLM's own tool
  choice on the ReAct fallback). Reports hit@1, recall, forbidden-rate,
  ask-accuracy, and intent-accuracy. Costs LLM calls; run it less often.

The point of the harness is a stable, labeled baseline: any routing change is
measured against it instead of eyeballed. Extend cases.json from real failures.

Usage:
  .venv/bin/python scripts/routing_eval/harness.py --mode router
  .venv/bin/python scripts/routing_eval/harness.py --mode chat   # server on :8000
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

CASES_PATH = Path(__file__).with_name("cases.json")


# ---------------------------------------------------------------- router mode

def _ranked_tools(query: str, semantic: bool, llm_select: bool = False):
    """(intent, caps, chains, [tool_name ranked]) from the deterministic layer,
    or None when Layer-1 abstains (the embedding/LLM classifier would decide).
    llm_select additionally applies app.routing.tool_selector's model
    arbitration on top of the same ranked union production uses, so this can
    be A/B'd against the deterministic-only ranking without a live server."""
    from app.capability_router import route_capabilities
    from app.provider_registry import get_provider_router
    from app.nodes.research import _BACKSTOP_CAPABILITIES, _DIRECT_CAPABILITY_ORDER
    from app.routing.tool_selector import select_tool

    route = route_capabilities(query)
    if route is None:
        return None
    caps = tuple(route.capabilities or ())
    chains = tuple(route.chains or ())
    router = get_provider_router()
    # Mirror research_node's reachability backstop: a tool whose own matcher fires
    # makes its capability eligible even when Layer-1 didn't emit it (e.g.
    # goldrush_hyperliquid_positions for "hyperliquid positions for 0x...").
    tool_matched = router.matched_capabilities(query, chains)
    caps = caps + tuple(
        c for c in _DIRECT_CAPABILITY_ORDER
        if c in _BACKSTOP_CAPABILITIES and c in tool_matched and c not in caps
    )
    union = {}
    for cap in caps:
        for tool in router.candidates(query, cap, chains, allow_semantic_fallback=semantic):
            union.setdefault(tool.name, tool)
    ranked = sorted(union.values(), key=lambda t: router._score(t, query, chains), reverse=True)
    if llm_select:
        ranked = select_tool(query, ranked, chains)
    return route.intent, caps, chains, [t.name for t in ranked]


def _warm_knowledge_snapshot() -> None:
    """The knowledge tool's matcher is I/O-free and reads a resolver snapshot
    the API keeps warm; outside the server we build it once here (needs
    DATABASE_URL with an ingested registry) so kb-* cases can route."""
    try:
        import asyncio
        from app.knowledge import tool as kb_tool

        resolver = asyncio.run(kb_tool.resolver(force=True))
        print(f"knowledge snapshot: {len(resolver)} entities")
    except Exception as exc:  # no database, empty registry: kb-* cases will abstain
        print(f"knowledge snapshot unavailable ({exc}); kb-* cases will abstain")


def run_router_mode(cases, k: int, semantic: bool, llm_select: bool = False):
    rows, agg = [], {"top1": 0, "recall_sum": 0.0, "mrr_sum": 0.0, "forbidden": 0, "n": 0, "abstain": 0}
    _warm_knowledge_snapshot()
    for c in cases:
        if not c.get("router", True):
            continue
        agg["n"] += 1
        expected = set(c.get("expected_tools") or [])
        forbidden = set(c.get("forbidden_tools") or [])
        result = _ranked_tools(c["query"], semantic, llm_select)
        if result is None:
            agg["abstain"] += 1
            rows.append((c["id"], "ABSTAIN", "-", "-", "-", "-"))
            continue
        intent, caps, chains, ranked = result
        topk = ranked[:k]
        top1 = bool(expected) and ranked[:1] and ranked[0] in expected
        recall = (len(expected & set(topk)) / len(expected)) if expected else 1.0
        mrr = 0.0
        for i, name in enumerate(ranked):
            if name in expected:
                mrr = 1.0 / (i + 1)
                break
        forbidden_hit = bool(ranked) and ranked[0] in forbidden
        agg["top1"] += int(bool(top1))
        agg["recall_sum"] += recall
        agg["mrr_sum"] += mrr
        agg["forbidden"] += int(forbidden_hit)
        rows.append((c["id"], "OK" if (not expected or top1) and not forbidden_hit else "MISS",
                     ranked[0] if ranked else "-", f"{recall:.2f}", f"{mrr:.2f}",
                     "FORBIDDEN" if forbidden_hit else ""))
    n = max(1, agg["n"] - agg["abstain"])
    print("\n=== ROUTER MODE (deterministic, semantic_fallback=%s, llm_select=%s) ===" % (semantic, llm_select))
    print(f"{'case':28} {'verdict':8} {'top-1 tool':32} {'recall':>6} {'mrr':>5} note")
    for r in rows:
        print(f"{r[0]:28} {r[1]:8} {str(r[2]):32} {r[3]:>6} {r[4]:>5} {r[5]}")
    print("\n-- router metrics --")
    print(f"cases evaluated : {agg['n']} (abstain {agg['abstain']})")
    print(f"top-1 accuracy  : {agg['top1']}/{n} = {agg['top1']/n:.1%}")
    print(f"recall@{k}       : {agg['recall_sum']/n:.1%}")
    print(f"MRR             : {agg['mrr_sum']/n:.3f}")
    print(f"forbidden@1     : {agg['forbidden']}/{n} = {agg['forbidden']/n:.1%}  (target 0)")
    return {"mode": "router", "metrics": {
        "cases": agg["n"], "abstain": agg["abstain"], "top1": agg["top1"] / n,
        "recall_at_k": agg["recall_sum"] / n, "mrr": agg["mrr_sum"] / n,
        "forbidden_at_1": agg["forbidden"] / n}, "rows": rows}


# ------------------------------------------------------------------ chat mode

def _chat(query: str, base: str, sid: str):
    body = json.dumps({"message": query, "session_id": sid}).encode()
    req = urllib.request.Request(base, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def _did_ask(answer: str) -> bool:
    # A clarification, detected by its markers -- NOT by "?" (the chain-ambiguity
    # ask is phrased "...which one you mean:" + "Reply with the chain", no "?").
    head = answer.lower()[:400]
    return any(w in head for w in (
        "which chain", "several chains", "which one you mean", "don't want to guess",
        "reply with the chain", "connect your wallet", "which token", "paste the exact contract"))


def run_chat_mode(cases, base: str):
    rows = []
    agg = {"reached": 0, "scored": 0, "forbidden": 0, "cache": 0, "ask_ok": 0, "ask_n": 0,
           "intent_ok": 0, "intent_n": 0, "n": 0, "err": 0}
    for i, c in enumerate(cases):
        agg["n"] += 1
        expected = c.get("expected_tools") or []
        forbidden = set(c.get("forbidden_tools") or [])
        try:
            d = _chat(c["query"], base, f"reval-{i}")
        except Exception as e:
            agg["err"] += 1
            rows.append((c["id"], "ERR", str(e)[:40], "", ""))
            continue
        traj = d.get("trajectory") or {}
        tools = [v for kk, v in traj.items() if "tool_name" in kk and isinstance(v, str)]
        called = set(tools)
        intent = d.get("intent")
        asked = _did_ask(d.get("answer") or "")
        forbidden_hit = bool(called & forbidden)
        ask_ok = ("expect_ask" not in c) or (bool(c["expect_ask"]) == asked)
        intent_ok = ("expected_intent" not in c) or (c["expected_intent"] == intent)
        if "expect_ask" in c:
            agg["ask_n"] += 1; agg["ask_ok"] += int(ask_ok)
        if "expected_intent" in c:
            agg["intent_n"] += 1; agg["intent_ok"] += int(intent_ok)
        # A cache hit hides the underlying tool -> inconclusive on tool-reach, so
        # exclude it from the reach denominator rather than count it a miss.
        cache_only = called == {"semantic_cache"}
        # "Reached" = the expected tool was actually called somewhere in the
        # composition (order-independent; our answers legitimately call several).
        reached = (not expected) or bool(called & set(expected))
        if cache_only and expected:
            agg["cache"] += 1
            reach_note = "CACHE"
        else:
            agg["scored"] += 1
            agg["reached"] += int(reached)
            reach_note = ""
        agg["forbidden"] += int(forbidden_hit)
        verdict = "OK"
        if forbidden_hit or (not cache_only and expected and not reached) or not ask_ok or not intent_ok:
            verdict = "MISS"
        elif cache_only and expected:
            verdict = "CACHE"
        rows.append((c["id"], verdict, intent, ("ASK" if asked else ",".join(tools)[:34]),
                     "FORBIDDEN" if forbidden_hit else reach_note))
    print("\n=== CHAT MODE (end-to-end via /chat) ===")
    print(f"{'case':28} {'verdict':8} {'intent':12} {'tools/ask':36} note")
    for r in rows:
        print(f"{r[0]:28} {r[1]:8} {str(r[2]):12} {str(r[3]):36} {r[4]}")
    scored = max(1, agg["scored"])
    print("\n-- chat metrics --")
    print(f"cases            : {agg['n']} (errors {agg['err']}, cache-inconclusive {agg['cache']})")
    print(f"tool-reached     : {agg['reached']}/{agg['scored']} = {agg['reached']/scored:.1%}  (expected tool called)")
    print(f"forbidden-rate   : {agg['forbidden']}/{agg['n'] - agg['err']} = {agg['forbidden']/max(1, agg['n']-agg['err']):.1%}  (target 0)")
    if agg["ask_n"]:
        print(f"ask-accuracy     : {agg['ask_ok']}/{agg['ask_n']} = {agg['ask_ok']/agg['ask_n']:.1%}")
    if agg["intent_n"]:
        print(f"intent-accuracy  : {agg['intent_ok']}/{agg['intent_n']} = {agg['intent_ok']/agg['intent_n']:.1%}")
    return {"mode": "chat", "metrics": {
        "cases": agg["n"], "errors": agg["err"], "cache_inconclusive": agg["cache"],
        "tool_reached": agg["reached"] / scored, "forbidden_rate": agg["forbidden"] / max(1, agg["n"] - agg["err"]),
        "ask_accuracy": (agg["ask_ok"] / agg["ask_n"]) if agg["ask_n"] else None,
        "intent_accuracy": (agg["intent_ok"] / agg["intent_n"]) if agg["intent_n"] else None},
        "rows": rows}


# --------------------------------------------------------------- resolve mode

def _pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))] if values else 0.0


LM_CALLS: list[dict] = []


def run_resolve_mode(cases, backend: str = "current", lm_spec: dict | None = None):
    lm_spec = lm_spec or {}
    """The routing DECISION layer (app/routing/resolver.resolve) in-process with
    the real embedding router and the real speech model: intent accuracy, which
    tier decided (method), and latency per case. This is the surface to measure
    when changing the order of the rules/embedding/model cascade -- router mode
    only sees the deterministic rules and chat mode costs a full agent turn.

    `backend` picks what answers the classification seam: "current" is the
    speech model (runtime._call_intent_lm); "jev" is TypeSafe's decision model
    (app.routing.jev_backend). Same cases, same rules ahead of the model, so
    the two are comparable on accuracy, model-call latency, tokens, and -- for
    Jev, which returns a distribution -- calibration of its stated confidence."""
    import asyncio
    from app.nodes import runtime
    from app.routing import resolver
    from app.routing.resolver import resolve
    from app.routing.semantic import embedding_router

    if backend == "jev":
        from app.routing import jev_backend
        jev_backend.CALLS.clear()
        call_lm = jev_backend.call_lm
    elif backend == "lm":
        # Any LiteLLM model at the classification seam, running the SAME
        # SpeechResolution program the speech model runs -- so a candidate
        # model is judged on our question, not a paraphrase of it.
        import dspy
        from app.settings import settings as _settings
        dspy.configure_cache(enable_disk_cache=False, enable_memory_cache=False)
        model, base = lm_spec["model"], lm_spec.get("api_base")
        lm = (dspy.LM(f"openai/{model}", api_base=base, api_key="none", timeout=_settings.llm_request_timeout_seconds, num_retries=0, max_tokens=4000)
              if base else dspy.LM(model, timeout=_settings.llm_request_timeout_seconds, num_retries=0))
        LM_CALLS.clear()

        async def call_lm(program, **kwargs):
            t0 = time.perf_counter()
            result = await runtime._run_guarded(program, lm, kwargs)
            usage = (lm.history[-1].get("usage") or {}) if lm.history else {}
            LM_CALLS.append({"request": kwargs.get("request"), "latency_ms": (time.perf_counter() - t0) * 1000,
                             "usage": usage, "understanding": dict(result.understanding) if hasattr(result, "understanding") else None})
            return result
        backend = f"lm:{model}"
    else:
        call_lm = runtime._call_intent_lm
    resolver._understanding_cache.clear()   # a prior backend's answers must not be reused
    warm_knowledge_snapshot()

    rows, agg = [], {"n": 0, "ok": 0, "methods": {}, "latency": [], "model_decided": []}
    for c in cases:
        if "expected_intent" not in c:
            continue
        agg["n"] += 1
        state = {"request": c["query"], "history": "", "session_context": {}}
        calls_before = len(jev_backend.CALLS) if backend == "jev" else len(LM_CALLS) if backend.startswith("lm:") else 0
        t0 = time.perf_counter()
        try:
            out = asyncio.run(resolve(state, call_lm, embedding_factory=embedding_router))
        except Exception as e:  # keep measuring the rest
            rows.append((c["id"], "ERR", str(e)[:40], "-", "-", "-"))
            continue
        ms = (time.perf_counter() - t0) * 1000
        agg["latency"].append(ms)
        d = out.get("routing_decision") or {}
        method = d.get("method") or "?"
        agg["methods"][method] = agg["methods"].get(method, 0) + 1
        ok = out.get("intent") == c["expected_intent"] or (
            out.get("intent") == "team" and out.get("team_subintent")
        )
        agg["ok"] += int(bool(ok))
        if backend == "jev" and len(jev_backend.CALLS) > calls_before:
            call = jev_backend.CALLS[-1]
            agg["model_decided"].append({"id": c["id"], "ok": bool(ok), "confidence": call["speech_act_confidence"],
                                         "latency_ms": call["latency_ms"], "tokens": call["usage"].get("input_tokens", 0),
                                         "understanding": call["understanding"]})
        elif backend.startswith("lm:") and len(LM_CALLS) > calls_before:
            call = LM_CALLS[-1]
            u_ = call["understanding"] or {}
            agg["model_decided"].append({"id": c["id"], "ok": bool(ok), "confidence": u_.get("confidence"),
                                         "latency_ms": call["latency_ms"], "tokens": call["usage"].get("prompt_tokens", 0) or 0,
                                         "understanding": u_})
        rows.append((c["id"], "OK" if ok else "MISS", out.get("intent"), c["expected_intent"], method, f"{ms:.0f}"))
    n = max(1, agg["n"])
    p50, p95 = _pct(agg["latency"], 0.5), _pct(agg["latency"], 0.95)
    print(f"\n=== RESOLVE MODE (decision layer, in-process; backend={backend}) ===")
    print(f"{'case':28} {'verdict':8} {'intent':14} {'expected':14} {'method':16} ms")
    for r in rows:
        print(f"{r[0]:28} {r[1]:8} {str(r[2]):14} {str(r[3]):14} {str(r[4]):16} {r[5]}")
    print("\n-- resolve metrics --")
    print(f"cases            : {agg['n']}")
    print(f"intent-accuracy  : {agg['ok']}/{n} = {agg['ok']/n:.1%}")
    print(f"decided by       : {agg['methods']}")
    print(f"latency p50/p95  : {p50:.0f} / {p95:.0f} ms")
    metrics = {"cases": agg["n"], "intent_accuracy": agg["ok"] / n, "methods": agg["methods"], "p50_ms": p50, "p95_ms": p95, "backend": backend}
    if agg["model_decided"]:
        md = agg["model_decided"]
        tokens = sum(m["tokens"] for m in md)
        bins = {}
        for m in md:
            b = min(9, int((m["confidence"] or 0) * 10)) / 10
            bins.setdefault(b, []).append(m["ok"])
        calibration = {f"{b:.1f}-{b + .1:.1f}": {"n": len(v), "accuracy": sum(v) / len(v)} for b, v in sorted(bins.items())}
        metrics.update({"model_calls": len(md), "model_accuracy": sum(m["ok"] for m in md) / len(md),
                        "model_latency_p50_ms": _pct([m["latency_ms"] for m in md], 0.5),
                        "model_latency_p95_ms": _pct([m["latency_ms"] for m in md], 0.95),
                        "input_tokens": tokens, "usd_per_1000_calls": tokens / len(md) * 1000 * 0.042 / 1_000_000,
                        "calibration": calibration})
        print(f"model calls      : {len(md)} · accuracy on those {metrics['model_accuracy']:.1%} · latency p50/p95 {metrics['model_latency_p50_ms']:.0f}/{metrics['model_latency_p95_ms']:.0f} ms")
        if backend == "jev":
            print(f"tokens           : {tokens} in total · ${metrics['usd_per_1000_calls']:.4f} per 1,000 calls at $0.042/M")
        else:
            print(f"tokens           : {tokens} prompt tokens in total ({tokens / max(1, len(md)):.0f} per call)")
        print("calibration      : " + ", ".join(f"[{k}] {v['accuracy']:.0%} of {v['n']}" for k, v in calibration.items()))
        for m in md:
            if not m["ok"]:
                print(f"  miss {m['id']:26} conf {m['confidence']:.2f} -> {m['understanding']['speech_act']}/{m['understanding']['domain']}")
    return {"mode": "resolve", "metrics": metrics, "rows": rows}


def warm_knowledge_snapshot() -> int:
    """The router's knowledge-base anchor reads an in-memory resolver snapshot
    that the app warms at startup; a bare process has none, so every KB ask
    fell to the model and the harness measured a router missing its anchor
    (three 'shared misses' that were this, not routing). Warm it as the app
    does and say how many entities it holds -- zero means the KB anchor is
    absent from this run and its cases are not meaningful."""
    import asyncio
    from app.knowledge import tool as kb_tool
    try:
        asyncio.run(kb_tool.resolver(force=True))
    except Exception as e:
        print(f"knowledge snapshot: EMPTY ({str(e)[:60]}) -- the KB anchor is absent from this run")
        return 0
    res = kb_tool.snapshot()
    n = len(res) if res is not None else 0
    print(f"knowledge snapshot: {n} entities" + ("" if n else " -- the KB anchor is absent from this run"))
    return n


# -------------------------------------------------------------- disagree mode

def run_disagree_mode(prompts_path: str):
    """Both backends over UNLABELED prompts (from collect.py), to find the ones
    worth labeling: where the backends disagree, or Jev's confidence is under
    the resolver's threshold. Prompts both backends agree on at high confidence
    almost never change the table; these do. Writes to_label.json next to the
    cases file: entries with expected_intent left blank for a human to fill,
    then paste into cases.json."""
    import asyncio
    from app.nodes import runtime
    from app.routing import jev_backend, resolver
    from app.routing.resolver import resolve
    from app.routing.semantic import embedding_router
    from app.settings import settings

    warm_knowledge_snapshot()
    prompts = json.loads(Path(prompts_path).read_text())
    if prompts and isinstance(prompts[0], str):
        prompts = [{"query": p, "source": "list"} for p in prompts]
    rows, to_label = [], []
    for c in prompts:
        state = {"request": c["query"], "history": "", "session_context": {}}
        resolver._understanding_cache.clear()
        try:
            cur = asyncio.run(resolve(dict(state), runtime._call_intent_lm, embedding_factory=embedding_router))
        except Exception as e:
            cur = {"intent": f"ERR {str(e)[:30]}", "routing_decision": {}}
        resolver._understanding_cache.clear()
        jev_backend.CALLS.clear()
        try:
            jev = asyncio.run(resolve(dict(state), jev_backend.call_lm, embedding_factory=embedding_router))
        except Exception as e:
            jev = {"intent": f"ERR {str(e)[:30]}", "routing_decision": {}}
        call = jev_backend.CALLS[-1] if jev_backend.CALLS else None
        conf = call["speech_act_confidence"] if call else None
        act = f"{call['understanding']['speech_act']}/{call['understanding']['domain']}" if call else "(rules)"
        disagree = cur.get("intent") != jev.get("intent")
        low = conf is not None and conf < settings.intent_model_confidence_threshold
        flag = "DISAGREE" if disagree else ("LOW-CONF" if low else "")
        rows.append((c["query"][:60], cur.get("intent"), (cur.get("routing_decision") or {}).get("method"), jev.get("intent"), act, f"{conf:.2f}" if conf is not None else "-", flag))
        if flag:
            entry = {"id": "label-me-" + str(len(to_label) + 1), "query": c["query"], "expected_tools": c.get("expected_tools", []),
                     "forbidden_tools": [], "expected_intent": "", "router": bool(c.get("expected_tools")),
                     "notes": f"{flag}: current={cur.get('intent')} jev={jev.get('intent')} ({act} @ {conf}); source={c.get('source')}"}
            to_label.append(entry)
    print(f"\n=== DISAGREE MODE ({len(prompts)} unlabeled prompts) ===")
    print(f"{'prompt':60} {'current':13} {'by':14} {'jev':13} {'jev act/domain':18} {'conf':5} flag")
    for r in rows:
        print(f"{r[0]:60} {str(r[1]):13} {str(r[2]):14} {str(r[3]):13} {r[4]:18} {r[5]:5} {r[6]}")
    n_dis = sum(1 for r in rows if r[6] == "DISAGREE"); n_low = sum(1 for r in rows if r[6] == "LOW-CONF")
    print(f"\nagree: {len(rows) - n_dis} · disagree: {n_dis} · agree but Jev under threshold: {n_low}")
    out = Path(CASES_PATH).with_name("to_label.json")
    out.write_text(json.dumps(to_label, indent=2))
    print(f"{len(to_label)} entries to label -> {out}  (fill expected_intent, then append to cases.json)")
    return {"mode": "disagree", "metrics": {"prompts": len(rows), "disagree": n_dis, "low_conf": n_low}, "rows": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["router", "chat", "resolve", "both", "disagree"], default="router")
    ap.add_argument("--prompts", default=str(Path(__file__).with_name("candidates.json")), help="disagree mode: unlabeled prompts (collect.py output, or a JSON list of strings)")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--backend", choices=["current", "jev", "lm"], default="current", help="resolve mode: what answers the classification seam")
    ap.add_argument("--lm-model", help="resolve mode with --backend lm: LiteLLM model id (or a served model id with --lm-api-base)")
    ap.add_argument("--lm-api-base", help="OpenAI-compatible base URL serving --lm-model")
    ap.add_argument("--semantic", action="store_true", help="router mode: allow embedding fallback in candidates")
    ap.add_argument("--llm-select", action="store_true", help="router mode: apply app.routing.tool_selector's model arbitration on top of the deterministic ranking (forces settings.llm_tool_selection_enabled=True for this run)")
    ap.add_argument("--base", default="http://localhost:8000/chat")
    ap.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = ap.parse_args()

    cases = json.loads(CASES_PATH.read_text())
    if args.llm_select:
        from app.settings import settings
        settings.llm_tool_selection_enabled = True
    out = {}
    if args.mode in ("router", "both"):
        out["router"] = run_router_mode(cases, args.k, args.semantic, args.llm_select)
    if args.mode == "resolve":
        out["resolve"] = run_resolve_mode(cases, backend=args.backend, lm_spec={"model": args.lm_model, "api_base": args.lm_api_base})
    if args.mode == "disagree":
        out["disagree"] = run_disagree_mode(args.prompts)
    if args.mode in ("chat", "both"):
        out["chat"] = run_chat_mode(cases, args.base)
    Path(args.out).write_text(json.dumps(out, indent=1))
    print(f"\nSaved {args.out}")


if __name__ == "__main__":
    main()

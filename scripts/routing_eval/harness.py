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

def _ranked_tools(query: str, semantic: bool):
    """(intent, caps, chains, [tool_name ranked]) from the deterministic layer,
    or None when Layer-1 abstains (the embedding/LLM classifier would decide)."""
    from app.capability_router import route_capabilities
    from app.provider_registry import get_provider_router
    from app.nodes.research import _BACKSTOP_CAPABILITIES, _DIRECT_CAPABILITY_ORDER

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
    return route.intent, caps, chains, [t.name for t in ranked]


def run_router_mode(cases, k: int, semantic: bool):
    rows, agg = [], {"top1": 0, "recall_sum": 0.0, "mrr_sum": 0.0, "forbidden": 0, "n": 0, "abstain": 0}
    for c in cases:
        if not c.get("router", True):
            continue
        agg["n"] += 1
        expected = set(c.get("expected_tools") or [])
        forbidden = set(c.get("forbidden_tools") or [])
        result = _ranked_tools(c["query"], semantic)
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
    print("\n=== ROUTER MODE (deterministic, semantic_fallback=%s) ===" % semantic)
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

def run_resolve_mode(cases):
    """The routing DECISION layer (app/routing/resolver.resolve) in-process with
    the real embedding router and the real speech model: intent accuracy, which
    tier decided (method), and latency per case. This is the surface to measure
    when changing the order of the rules/embedding/model cascade -- router mode
    only sees the deterministic rules and chat mode costs a full agent turn."""
    import asyncio
    from app.nodes import runtime
    from app.routing.resolver import resolve
    from app.routing.semantic import embedding_router

    rows, agg = [], {"n": 0, "ok": 0, "methods": {}, "latency": []}
    for c in cases:
        if "expected_intent" not in c:
            continue
        agg["n"] += 1
        state = {"request": c["query"], "history": "", "session_context": {}}
        t0 = time.perf_counter()
        try:
            out = asyncio.run(resolve(state, runtime._call_intent_lm, embedding_factory=embedding_router))
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
        rows.append((c["id"], "OK" if ok else "MISS", out.get("intent"), c["expected_intent"], method, f"{ms:.0f}"))
    n = max(1, agg["n"])
    lat = sorted(agg["latency"])
    p50 = lat[len(lat) // 2] if lat else 0.0
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0.0
    print("\n=== RESOLVE MODE (decision layer, in-process) ===")
    print(f"{'case':28} {'verdict':8} {'intent':14} {'expected':14} {'method':16} ms")
    for r in rows:
        print(f"{r[0]:28} {r[1]:8} {str(r[2]):14} {str(r[3]):14} {str(r[4]):16} {r[5]}")
    print("\n-- resolve metrics --")
    print(f"cases            : {agg['n']}")
    print(f"intent-accuracy  : {agg['ok']}/{n} = {agg['ok']/n:.1%}")
    print(f"decided by       : {agg['methods']}")
    print(f"latency p50/p95  : {p50:.0f} / {p95:.0f} ms")
    return {"mode": "resolve", "metrics": {"cases": agg["n"], "intent_accuracy": agg["ok"] / n,
                                           "methods": agg["methods"], "p50_ms": p50, "p95_ms": p95}, "rows": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["router", "chat", "resolve", "both"], default="router")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--semantic", action="store_true", help="router mode: allow embedding fallback in candidates")
    ap.add_argument("--base", default="http://localhost:8000/chat")
    ap.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = ap.parse_args()

    cases = json.loads(CASES_PATH.read_text())
    out = {}
    if args.mode in ("router", "both"):
        out["router"] = run_router_mode(cases, args.k, args.semantic)
    if args.mode == "resolve":
        out["resolve"] = run_resolve_mode(cases)
    if args.mode in ("chat", "both"):
        out["chat"] = run_chat_mode(cases, args.base)
    Path(args.out).write_text(json.dumps(out, indent=1))
    print(f"\nSaved {args.out}")


if __name__ == "__main__":
    main()

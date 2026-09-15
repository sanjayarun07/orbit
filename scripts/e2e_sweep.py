"""End-to-end use-case sweep against a running Orbit server.

Multi-turn scenarios through POST /chat (and a few sibling endpoints) with
assertions on intent, tools reached, answer markers, cards and safety
outcomes -- the flows a user actually exercises, not just single prompts:
chain-ambiguity ask + reply, wallet-connect hand-off + acknowledgement, risk
charter set -> quote blocked, team desk on -> routed to the desk, pronoun
follow-ups, confirm/cancel controls, history deletion, MCP tools.

Costs real model and provider calls. Usage:
  .venv/bin/python scripts/e2e_sweep.py --base http://localhost:8000 [--out results.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

READ_ONLY_SOL_WALLET = "5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG"  # public, funded; quotes only
EVM_WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


def _post(base, path, body, timeout=170):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"detail": str(e)}


def _tools(d):
    return [v for k, v in (d.get("trajectory") or {}).items() if "tool_name" in k and isinstance(v, str)]


# ---------------------------------------------------------------- scenarios
# Each step: dict(message, wallet?, extra?, check=lambda status, data, tools, ctx -> list[str] problems)
# `ctx` carries session_id / prior data between steps; a step may set ctx["plan_id"].

def S(name, steps, group):
    return {"name": name, "steps": steps, "group": group}


def _intent(*ok):
    return lambda st, d, t, c: [] if d.get("intent") in ok else [f"intent={d.get('intent')} not in {ok}"]


def _contains(*needles):
    return lambda st, d, t, c: [f"answer lacks {n!r}" for n in needles if n.lower() not in (d.get("answer") or "").lower()]


def _not_contains(*needles):
    return lambda st, d, t, c: [f"answer has forbidden {n!r}" for n in needles if n.lower() in (d.get("answer") or "").lower()]


def _tools_any(*names):
    return lambda st, d, t, c: [] if any(any(n in x for x in t) for n in names) else [f"no tool from {names} in {t}"]


def _tools_none(*names):
    return lambda st, d, t, c: [f"forbidden tool {n}" for n in names if any(n in x for x in t)]


def _no_tools():
    return lambda st, d, t, c: [] if not t else [f"unexpected tools {t}"]


def _has(key, present=True):
    return lambda st, d, t, c: [] if bool(d.get(key)) == present else [f"{key} present={bool(d.get(key))}, expected {present}"]


def _status(code):
    return lambda st, d, t, c: [] if st == code else [f"status {st} != {code}"]


def _all(*checks):
    return lambda st, d, t, c: [p for ch in checks for p in ch(st, d, t, c)]


def _remember_plan(st, d, t, c):
    if d.get("trade_plan"):
        c["plan_id"] = d["trade_plan"]["plan_id"]
    return []


SCENARIOS = [
    S("market overview card", [dict(message="how is the crypto market today", check=_all(_intent("research"), _tools_any("crypto_market_overview")))], "research"),
    S("token deep dive", [dict(message="deep dive on BONK", check=_all(_intent("research"), _tools_any("token_deep_dive")))], "research"),
    S("chain ambiguity: ask then answer", [
        dict(message="price of PEPE", check=_all(_intent("research"), _contains("which one you mean"))),
        dict(message="ethereum", check=_all(_intent("research"), _not_contains("which one you mean"), lambda st, d, t, c: [] if t else ["no tools after disambiguation"])),
    ], "research"),
    S("trending on solana", [dict(message="trending tokens on solana", check=_all(_intent("research"), _tools_any("dexscreener", "geckoterminal")))], "research"),
    S("top holders", [dict(message="top holders of BONK", check=_all(_intent("research"), _tools_any("holders", "top_holders")))], "research"),
    S("token security, casual phrasing", [dict(message="can you check if BONK is safe to ape into", check=_all(_intent("research"), _tools_any("security", "shield", "goplus", "honeypot", "birdeye", "search_verified")))], "research"),
    S("hyperliquid positions for an address", [dict(message=f"Show hyperliquid perp positions for {EVM_WALLET}", check=_all(_intent("research"), _tools_any("hyperliquid")))], "research"),
    S("news", [dict(message="any news on Solana today?", check=_all(_intent("research"), _tools_any("perplexity", "web_search")))], "research"),
    S("explain (no tools)", [dict(message="explain what TVL means", check=_all(_intent("general"), _no_tools()))], "routing"),
    S("advice is research, not a simulation", [dict(message="should I buy HYPE token?", check=_all(_intent("research"), _tools_none("jupiter_simulate_swap", "sol_balance"), _contains("not financial advice")))], "routing"),
    S("conditional order never quotes", [dict(message="if SOL drops under 100 buy 2 SOL", wallet=READ_ONLY_SOL_WALLET, check=_all(lambda st, d, t, c: [] if d.get("intent") not in ("trade", "cross_chain_swap") else ["routed to execution"], _has("trade_plan", False)))], "safety"),
    S("policy question", [dict(message="my wallet policy?", wallet=READ_ONLY_SOL_WALLET, check=_all(_intent("general"), _contains("Max per trade"), _no_tools()))], "policy"),
    S("portfolio: balances + exposure", [
        dict(message="how much SOL do I have", wallet=READ_ONLY_SOL_WALLET, check=_all(_intent("portfolio"), _tools_any("portfolio_snapshot", "sol_balance"))),
        dict(message="what's my exposure to SOL right now", wallet=READ_ONLY_SOL_WALLET, check=_intent("portfolio")),
    ], "portfolio"),
    S("trade simulation (read-only)", [dict(message="what would happen if I sold 0.05 SOL for USDC", wallet=READ_ONLY_SOL_WALLET, check=_all(_intent("portfolio"), _contains("simulation"), _has("trade_plan", False)))], "portfolio"),
    S("jupiter quote -> cancel", [
        dict(message="swap 0.01 SOL to USDC on solana with 50 bps slippage", wallet=READ_ONLY_SOL_WALLET, check=_all(_intent("trade"), _has("trade_plan"), _remember_plan)),
        dict(message="cancel", wallet=READ_ONLY_SOL_WALLET, check=_all(_intent("general"), _contains("dismissed"))),
    ], "trading"),
    S("typed CONFIRM is refused in chat", [
        dict(message="swap 0.01 SOL to USDC on solana with 50 bps slippage", wallet=READ_ONLY_SOL_WALLET, check=_all(_intent("trade"), _has("trade_plan"), _remember_plan)),
        dict(message=lambda c: f"CONFIRM {c.get('plan_id', 'plan_x')}", wallet=READ_ONLY_SOL_WALLET, check=_all(_intent("general"), _contains("confirmation button"), _has("trade_plan", False))),
    ], "safety"),
    S("risk charter blocks an over-cap quote", [
        dict(message="set my risk charter", wallet=READ_ONLY_SOL_WALLET, extra={"risk_charter_fields": {"max_trade_usd": 0.5, "verified_only": True}},
             check=_all(_intent("general"), lambda st, d, t, c: [] if d.get("risk_charter_fields", {}).get("max_trade_usd") == 0.5 else ["charter fields not persisted"])),
        dict(message="swap 0.01 SOL to USDC on solana with 50 bps slippage", wallet=READ_ONLY_SOL_WALLET,
             check=_all(_contains("Blocked by your risk charter", "max per trade"), _has("trade_plan", False),
                        lambda st, d, t, c: [] if (d.get("risk_assessment") or {}).get("verdict") == "blocked" else ["risk verdict not blocked"])),
        dict(message="clear my risk charter", wallet=READ_ONLY_SOL_WALLET, check=lambda st, d, t, c: [] if d.get("risk_charter") is None else ["charter not cleared"]),
    ], "safety"),
    S("cross-chain: refused without wallet, then 'yes connected'", [
        dict(message="swap 0.01 sol to usdc on base with 50bps slippage", check=_all(_intent("cross_chain_swap"), _contains("Connect wallet"), _has("cross_chain_swap", False))),
        dict(message="yes connected", wallet=READ_ONLY_SOL_WALLET, check=_all(_intent("cross_chain_swap"), _has("cross_chain_swap"), _contains("Relay quote"))),
    ], "trading"),
    S("team desk switch routes to the desk", [
        dict(message="team status", extra={"team_mode": True}, check=_all(_intent("general"), _contains("ON"), lambda st, d, t, c: [] if d.get("team_mode") is True else ["team_mode not echoed"])),
        dict(message="TVL on solana", check=_all(_intent("research", "trade"), _has("team_report"))),
    ], "desk"),
    S("pronoun follow-up keeps the subject", [
        dict(message="price and liquidity of BONK on solana", check=_all(_intent("research"), lambda st, d, t, c: [] if t else ["no tools"])),
        # A holders tool, or the router's semantic cache replaying a holders answer
        # for the same resolved mint (the check is on what the user gets).
        dict(message="who are its top holders?", check=_all(_intent("research"), lambda st, d, t, c: [] if (
            any("holders" in name for name in t) or any(k in (d.get("answer") or "").lower() for k in ("largest token accounts", "top holders", "% of supply"))
        ) else [f"no holders tool or holders answer in {t}"])),
    ], "context"),
    S("stale revision is rejected, not reinterpreted", [
        dict(message="hello", check=_intent("general")),
        dict(message="hello again", revision_offset=-1, expect_status=409, check=lambda st, d, t, c: []),
    ], "safety"),
]


def run(base: str) -> dict:
    rows = []
    for sc in SCENARIOS:
        ctx: dict = {}
        sid, rev = None, None
        problems: list[str] = []
        t0 = time.perf_counter()
        for i, step in enumerate(sc["steps"]):
            message = step["message"](ctx) if callable(step["message"]) else step["message"]
            body = {"message": message, **(step.get("extra") or {})}
            if step.get("wallet"):
                body["wallet_address"] = step["wallet"]
            if sid:
                body["session_id"] = sid
                body["context_revision"] = rev + step.get("revision_offset", 0)
            status, data = _post(base, "/chat", body)
            data = data if isinstance(data, dict) else {}
            tools = _tools(data)
            expected = step.get("expect_status", 200)
            if status != expected:
                problems.append(f"step {i+1} ({message[:40]!r}): HTTP {status} != {expected} {str(data)[:120]}")
                continue
            if status == 200:
                sid, rev = data["session_id"], data["session_revision"]
            problems += [f"step {i+1} ({message[:40]!r}): {p}" for p in step["check"](status, data, tools, ctx)]
        ms = (time.perf_counter() - t0) * 1000
        rows.append({"scenario": sc["name"], "group": sc["group"], "ok": not problems, "problems": problems, "ms": round(ms), "session_id": sid})
        print(f"{'OK  ' if not problems else 'MISS'} {sc['group']:9} {sc['name']:52} {ms/1000:6.1f}s" + ("" if not problems else "\n      " + "\n      ".join(problems)))
    # history deletion on the last session
    if rows and rows[-1]["session_id"]:
        sid = rows[-1]["session_id"]
        req = urllib.request.Request(f"{base}/chat/history/{sid}", method="DELETE")
        with urllib.request.urlopen(req, timeout=30) as r:
            deleted = r.status == 200
        with urllib.request.urlopen(f"{base}/chat/history/{sid}", timeout=30) as r:
            empty = not json.load(r)["messages"]
        ok = deleted and empty
        rows.append({"scenario": "delete conversation history", "group": "history", "ok": ok, "problems": [] if ok else ["history not deleted"], "ms": 0, "session_id": sid})
        print(f"{'OK  ' if ok else 'MISS'} {'history':9} {'delete conversation history':52}")
    passed = sum(1 for r in rows if r["ok"])
    print(f"\n-- e2e sweep: {passed}/{len(rows)} scenarios passed --")
    return {"passed": passed, "total": len(rows), "rows": rows}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    result = run(args.base.rstrip("/"))
    if args.out:
        open(args.out, "w").write(json.dumps(result, indent=1))
    sys.exit(0 if result["passed"] == result["total"] else 1)

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
import http.cookiejar
import os
import urllib.error
import urllib.request

# Signed-in session for the whole sweep: accounts gate history, wallets and
# tasks, and credits are metered per turn. The cookie jar keeps the session.
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))
SWEEP_EMAIL = "sweep@example.com"

READ_ONLY_SOL_WALLET = "5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG"  # public, funded; quotes only
EVM_WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


def _request(base, method, path, body=None, timeout=170, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"content-type": "application/json", "x-orbit-device": "e2e-sweep", **(headers or {})})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"detail": str(e)}


def _post(base, path, body, timeout=170):
    return _request(base, "POST", path, body, timeout)


def sign_in(base: str) -> dict:
    """Magic-link sign-in (dev link) and, when ADMIN_API_KEY is in the env,
    a Max plan + credits so the sweep never runs dry."""
    status, started = _post(base, "/auth/email/start", {"email": SWEEP_EMAIL}, timeout=30)
    if status != 200 or "dev_link" not in started:
        raise SystemExit(f"sign-in unavailable ({status}): set DEV_EXPOSE_MAGIC_LINKS=true and leave RESEND unset for the sweep")
    token = started["dev_link"].rsplit("signin=", 1)[1]
    status, me = _post(base, "/auth/email/verify", {"token": token}, timeout=30)
    if status != 200:
        raise SystemExit(f"sign-in failed: {me}")
    admin_key = os.environ.get("ADMIN_API_KEY")
    if admin_key:
        headers = {"authorization": f"Bearer {admin_key}"}
        _request(base, "PUT", f"/admin/users/{SWEEP_EMAIL}/plan", {"plan_id": "max"}, 30, headers)
        _request(base, "POST", f"/admin/users/{SWEEP_EMAIL}/credits", {"amount": 500, "reason": "e2e sweep", "reference": f"sweep-{int(time.time())}"}, 30, headers)
    # A previous run that stopped mid-way can leave the sweep account with a
    # tight risk charter; quotes would then be blocked and look like regressions.
    _post(base, "/chat", {"message": "clear my risk charter", "session_id": f"sweep-reset-{int(time.time())}", "wallet_address": READ_ONLY_SOL_WALLET}, timeout=60)
    status, me = _request(base, "GET", "/me", None, 30)
    print(f"signed in as {me.get('user', {}).get('email')} · plan {me.get('plan', {}).get('id')} · {me.get('credits', {}).get('balance')} credits")
    return me


def _tools(d):
    return [v for k, v in (d.get("trajectory") or {}).items() if "tool_name" in k and isinstance(v, str)]


# ---------------------------------------------------------------- scenarios
# Each step: dict(message, wallet?, extra?, check=lambda status, data, tools, ctx -> list[str] problems)
# `ctx` carries session_id / prior data between steps; a step may set ctx["plan_id"].

def S(name, steps, group):
    return {"name": name, "steps": steps, "group": group}


def API(method, path, body=None, check=None, expect_status=200, admin=False):
    """A non-chat step: call an endpoint and check its JSON. `admin=True` sends
    the ADMIN_API_KEY from the environment (the step is skipped without it)."""
    return {"api": (method, path, body), "check": check or (lambda st, d, t, c: []), "expect_status": expect_status, "admin": admin}


def _has_key(*keys):
    return lambda st, d, t, c: [f"missing {k}" for k in keys if k not in d]


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
    S("trending narratives lead with the story", [dict(message="What are the trending narratives right now in crypto?",
                                                       check=_all(_intent("research"), _tools_any("crypto_market_brief"), _contains("Crypto narratives right now", "What the market is trading on", "On-chain metas"), _not_contains("Promoted-token attention")))], "research"),
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
    S("home: news tiles, chips, $ticker search", [
        API("GET", "/home/highlights", check=lambda st, d, t, c: [] if len(d.get("cards", [])) == 4 and d.get("source") in ("news", "market", "static") else [f"cards={len(d.get('cards', []))} source={d.get('source')}"]),
        API("GET", "/home/suggestions", check=lambda st, d, t, c: [] if {x["id"] for x in d.get("categories", [])} >= {"trending", "week", "crypto", "stocks", "macro"} else [f"categories={[x['id'] for x in d.get('categories', [])]}"]),
        API("GET", "/tokens/search?q=sol", check=lambda st, d, t, c: [] if d.get("tokens") and d["tokens"][0]["symbol"] == "SOL" else [f"tokens={d.get('tokens')}"]),
        API("GET", "/calendar?days=7", check=lambda st, d, t, c: [] if isinstance(d.get("events"), list) else ["no events list"]),
    ], "home"),
    S("home tile follow-up is news research", [dict(message="Tell me more about this and why it matters for the market: Bitcoin ETFs lose $450M as CLARITY Act stalls",
                                                     check=_all(_intent("research"), _tools_any("perplexity_web_search", "web_search"), _tools_none("dexscreener_pair_search")))], "home"),
    S("why is X moving (crypto + stock)", [
        dict(message="why is SOL down today?", check=_all(_intent("research"), _tools_any("market_data", "perplexity_web_search"), _contains("Why is Solana"))),
        dict(message="why is NVDA stock up?", check=_all(_intent("research"), _contains("Why is NVDA"))),
        dict(message="why USELESS token was pumping this week in binance?", check=_all(_intent("research"), _contains("Why is"), _contains("USELESS"), _tools_none("dexscreener_pair_search"))),
    ], "research"),
    S("market event calendar card", [dict(message="what events could move the market this week?", check=_all(_intent("research"), _tools_any("market_event_calendar"), _contains("Market events")))], "research"),
    S("social sentiment tool", [dict(message="what is crypto twitter saying about BONK", check=_all(_intent("research"), _tools_any("x_kol_sentiment"), _contains("sentiment")))], "research"),
    S("fees, yields and stablecoins (free DefiLlama)", [
        dict(message="how much revenue does Aave make", check=_all(_intent("research"), _tools_any("defillama_fees_revenue"), _contains("Aave"))),
        dict(message="best USDC yield on Base", check=_all(_intent("research"), _tools_any("defillama_yields"), _contains("APY"))),
        dict(message="what backs USDe and how does it keep its peg", check=_all(_intent("research"), _tools_any("knowledge_base_search"), _contains("USDe"))),
        API("GET", "/admin/research/gaps?days=30", admin=True, check=lambda st, d, t, c: [] if isinstance(d.get("topics"), list) else [f"gaps={d}"]),
    ], "research"),
    S("knowledge base: docs, incidents, funding, graph", [
        dict(message="How does Aave V3's E-mode change the liquidation threshold?", check=_all(_intent("research"), _tools_any("knowledge_base_search"), _contains("[1]"))),
        dict(message="has Aave ever been hacked?", check=_all(_intent("research"), _tools_any("knowledge_base_search"), _tools_none("perplexity_web_search"))),
        dict(message="who are the investors backing EigenLayer?", check=_all(_intent("research"), _tools_any("knowledge_base_search"))),
        dict(message="what is Aave's TVL right now", check=_all(_intent("research"), _tools_none("knowledge_base_search"))),   # live numbers never go to the KB
        API("GET", "/knowledge/search?q=Aave%20liquidation%20threshold&limit=3", check=lambda st, d, t, c: [] if d.get("hits") and d.get("citations") and d["entities"] and d["entities"][0]["id"] == "protocol:aave-v3" else [f"search: entities={d.get('entities')} hits={len(d.get('hits') or [])}"]),
        API("GET", "/knowledge/graph/protocol:aave-v3?relation=COMPETITOR_OF", check=lambda st, d, t, c: [] if d.get("edges") else ["no competitor edges"]),
        API("GET", "/knowledge/status", check=lambda st, d, t, c: [] if d.get("backend") == "postgres" and d.get("vector_native") and d.get("chunks", 0) > 1000 else [f"status={d}"]),
    ], "knowledge"),
    S("tasks from chat: remind, alert, list, delete", [
        dict(message="remind me in 3 hours to check SOL", extra={"tz_offset_min": 330}, check=_all(_intent("general"), _contains("Reminder set", "check SOL"))),
        dict(message="alert me when SOL drops below $10", check=_all(_intent("general"), _contains("Alert set", "SOL < $10"))),
        dict(message="show my tasks", check=_all(_intent("general"), _contains("Your tasks", "Reminder: check SOL", "SOL < $10"))),
        API("GET", "/me/tasks", check=lambda st, d, t, c: (c.__setitem__("task_id", d["tasks"][0]["id"]) or []) if len(d.get("tasks", [])) >= 2 else [f"tasks={len(d.get('tasks', []))}"]),
        dict(message="delete all my tasks", check=_all(_intent("general"), _contains("deleted"))),
        API("GET", "/me/tasks", check=lambda st, d, t, c: [] if not [x for x in d.get("tasks", []) if x["status"] != "done"] else ["tasks not deleted"]),
    ], "tasks"),
    S("morning brief: run now lands in the inbox", [
        API("POST", "/me/tasks", {"kind": "brief", "schedule": {"daily": "08:00"}, "tz_offset_min": 330}, check=lambda st, d, t, c: (c.__setitem__("brief_id", d.get("id")) or []) if d.get("id") else ["no task id"], expect_status=201),
        API("POST", lambda c: f"/me/tasks/{c['brief_id']}/run", check=lambda st, d, t, c: [] if d.get("fired") else [f"brief did not fire: {d}"]),
        API("GET", "/me/inbox", check=lambda st, d, t, c: [] if d.get("items") and "Morning brief" in d["items"][0]["title"] else [f"inbox={d}"]),
        API("POST", "/me/inbox/read", {}, check=lambda st, d, t, c: [] if d.get("unread") == 0 else [f"unread={d.get('unread')}"]),
        API("DELETE", lambda c: f"/me/tasks/{c['brief_id']}", check=lambda st, d, t, c: [] if d.get("deleted") else ["not deleted"]),
    ], "tasks"),
    S("account surface: me, usage, sessions, keys", [
        API("GET", "/me", check=lambda st, d, t, c: [] if d.get("authenticated") and d.get("plan") else ["not signed in"]),
        API("GET", "/me/usage?days=7", check=_has_key("by_day", "by_kind", "total")),
        API("GET", "/me/sessions", check=lambda st, d, t, c: [] if d.get("sessions") else ["no sessions"]),
        API("GET", "/me/api-keys", check=_has_key("keys", "allowed")),
        API("GET", "/me/credits", check=_has_key("balance", "ledger", "costs")),
        API("GET", "/billing/plans", check=lambda st, d, t, c: [] if [p["id"] for p in d.get("plans", [])] == ["free", "pro", "max"] else ["plans wrong"]),
    ], "account"),
    S("stale revision is rejected, not reinterpreted", [
        dict(message="hello", check=_intent("general")),
        dict(message="hello again", revision_offset=-1, expect_status=409, check=lambda st, d, t, c: []),
    ], "safety"),
]


def run(base: str) -> dict:
    sign_in(base)
    rows = []
    for sc in SCENARIOS:
        ctx: dict = {}
        sid, rev = None, None
        problems: list[str] = []
        t0 = time.perf_counter()
        for i, step in enumerate(sc["steps"]):
            if "api" in step:
                method, path, body = step["api"]
                path = path(ctx) if callable(path) else path
                headers = None
                if step.get("admin"):
                    admin_key = os.environ.get("ADMIN_API_KEY")
                    if not admin_key:
                        continue   # admin-only check: nothing to assert without the key
                    headers = {"authorization": f"Bearer {admin_key}"}
                status, data = _request(base, method, path, body, 60, headers)
                data = data if isinstance(data, dict) else {}
                expected = step.get("expect_status", 200)
                if status != expected:
                    problems.append(f"step {i+1} ({method} {path}): HTTP {status} != {expected} {str(data)[:120]}")
                    continue
                problems += [f"step {i+1} ({method} {path}): {p}" for p in step["check"](status, data, [], ctx)]
                continue
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
        status, _ = _request(base, "DELETE", f"/chat/history/{sid}", None, 30)
        deleted = status == 200
        status, hist = _request(base, "GET", f"/chat/history/{sid}", None, 30)
        empty = status == 200 and not hist.get("messages")
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

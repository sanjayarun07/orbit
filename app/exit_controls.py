"""Chat controls for the exit monitor (app/exit_monitor.py).

Anchored commands, like the reminder controls: "watch my exit on BONK",
"exit analysis for WIF", "can I exit my BONK position", "how is my exit
on BONK", "stop watching my exit on BONK", "my exits". Signed-in users
with a connected or named Solana wallet only. The token is resolved
through Jupiter's registry: an exact verified symbol wins; two verified
tokens with the same symbol is a question back, never a guess.
"""
from __future__ import annotations

import re

from app import exit_monitor
from app.settings import settings
from app.jupiter import jupiter, normalize_mint

_TOKEN = r"(\$?[A-Za-z][A-Za-z0-9._-]{1,15}|[1-9A-HJ-NP-Za-km-z]{32,44})"      # unnamed: the ask pattern uses it several times
_WATCH = re.compile(rf"^\s*(?:please\s+)?(?:watch|monitor|track)\s+my\s+(?:exit|position)\s+(?:on|for|in)\s+{_TOKEN}\s*[.!?]?\s*$", re.I)
_STOP = re.compile(rf"^\s*(?:stop|cancel|end)\s+(?:watching|monitoring|tracking)\s+(?:my\s+)?(?:exit|position)\s+(?:on|for|in)\s+{_TOKEN}\s*[.!?]?\s*$", re.I)
_ASK = re.compile(rf"^\s*(?:(?:exit\s+analysis|exit\s+check)\s+(?:for|on)\s+{_TOKEN}|can\s+i\s+(?:still\s+)?(?:exit|get\s+out\s+of)\s+(?:my\s+)?{_TOKEN}(?:\s+position)?(?:\s+(?:in|from|with)\s+(?:the|my)\s+connected\s+wallet)?"
                  rf"|how(?:'s|\s+is)\s+my\s+exit\s+(?:on|for|in)\s+{_TOKEN}|what\s+would\s+(?:a\s+)?(?:full|25%|50%|half|100%)?\s*exit\s+(?:of|from)\s+(?:my\s+)?{_TOKEN}\s+(?:return|get\s+me))\s*[?.!]?(?:\s.*)?$", re.I | re.S)
_LIST = re.compile(r"^\s*(?:show|list|what\s+are)\s+(?:me\s+)?my\s+(?:exits|watched\s+positions|exit\s+monitors)\s*\??\s*$", re.I)
# "tell me when the discount on my full-position exit quote exceeds 5%",
# "alert me when my BONK exit drops 10%", "email me when my exit on WIF falls 15%"
_THRESHOLD = re.compile(
    r"^\s*(?P<verb>tell|alert|notify|email|warn)\s+me\s+(?:when|if)\s+(?:the\s+)?(?P<what>discount\s+(?:on|of)\s+)?my\s+(?:full[- ]position\s+)?"
    rf"(?:(?P<before>{_TOKEN})\s+)?exit(?:\s+quote)?(?:\s+(?:on|for|in)\s+(?P<after>{_TOKEN}))?\s+(?:exceeds|is\s+(?:more|higher|greater)\s+than|goes\s+(?:above|over|past)|widens\s+(?:past|beyond)|drops|falls|declines|deteriorates)(?:\s+by|\s+more\s+than|\s+over)?\s+(?P<pct>\d+(?:\.\d+)?)\s*%\s*[.!?]?(?:\s.*)?$", re.I | re.S)
_CHANNEL = re.compile(r"^\s*(?:(?:email|send)\s+me\s+my\s+exit\s+alerts(?:\s+by\s+email)?|(?:send\s+)?(?:my\s+)?exit\s+alerts\s+(?:by|via|to)\s+(?P<channel>email|inbox|app))\s*[.!?]?\s*$", re.I)
# "compare buying $500, $2,000 and $5,000 of BONK", "size check BONK at $1000", "what would $250 of WIF cost to enter and exit"
# The command may carry the chain and trailing instructions: "compare buying
# $500, $2,000 and $5,000 of ANSEM on Solana. Show entry quotes and immediate
# reverse-exit estimates. Do not prepare or execute any trade." (2026-09-23).
def _named(name: str) -> str:
    """The token pattern as a named group, so a mint with digits in it is
    read as the token and never discarded (review, 2026-09-23)."""
    return f"(?P<{name}>" + _TOKEN[1:]


_SIZES = re.compile(
    rf"^\s*(?:compare\s+buying\s+(?P<amounts>[\$\d,.\s]+(?:and|or)?[\$\d,.\s]*)\s+(?:of|worth\s+of|in)\s+{_named('t1')}"
    rf"|size\s+check\s+{_named('t2')}\s+(?:at|for|with)\s+(?P<amounts2>[\$\d,.\s]+(?:and|or)?[\$\d,.\s]*)"
    rf"|what\s+would\s+(?P<amounts3>\$[\d,.]+)\s+(?:of|in)\s+{_named('t3')}\s+cost\s+to\s+(?:enter\s+and\s+exit|buy\s+and\s+sell|get\s+in\s+and\s+out(?:\s+of)?))"
    rf"(?:\s+on\s+solana)?\s*[?.!]?(?:\s.*)?$", re.I | re.S)
# "What changed since I entered ANSEM? Separate token price movement, changes
# in my holdings, and worsening exit liquidity." -- the decomposition against
# the saved entry baseline, or the honest absence of one.
_CHANGED = re.compile(
    rf"^\s*what(?:'s|\s+has|\s+is)?\s+changed\s+since\s+(?:i\s+)?(?:entered|bought|got\s+into|entry\s+(?:on|in|into)|my\s+entry\s+(?:on|in|into))\s+{_TOKEN}\s*\??(?:\s.*)?$", re.I | re.S)
# "which has a better exit for a $1,000 position?", "what would exiting
# $500 of BONK cost?": an exit at a dollar size is the sizing diagnostic
# (entry, immediate reverse exit), read-only, no wallet; the token comes from
# the sentence or from the conversation's focus (expanded journeys,
# 2026-09-24: the wallet portfolio tool ran on the token's mint instead).
_SIZED_EXIT = re.compile(
    rf"^\s*(?:and\s+|so\s+|but\s+)?(?:(?:which|what|how)\b(?=[^?]*\b(?:exit\w*|get\s+out|unwind\w*)\b)[^?]*?|(?:exiting|unwinding)\s+)"
    rf"(?:a\s+|an\s+|my\s+|for\s+a\s+|for\s+|of\s+)?(?P<amount>\$\s*\d[\d,]*(?:\.\d+)?\s*[kK]?)(?:\s+(?:position|worth|stake|bag|exit|of\s+it|of\s+that|of\s+this))?"
    rf"(?:\s+(?:in|of|from)\s+{_named('t')})?\s*[?.!]?(?:\s.*)?$", re.I | re.S)
_AMOUNT = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d+)?)")
_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def is_public_control(message: str) -> bool:
    """A control that needs no account: the sizing diagnostic."""
    return bool(_SIZES.match(message or "") or _SIZED_EXIT.match(message or ""))


def is_exit_control(message: str) -> bool:
    return any(p.match(message or "") for p in (_WATCH, _STOP, _ASK, _LIST, _THRESHOLD, _CHANNEL, _SIZES, _CHANGED, _SIZED_EXIT))


async def _since_entry(p: dict) -> str:
    """Price move, holding change, discount widening and missing data, each
    on its own line, against the saved entry baseline."""
    symbol = p.get("symbol") or p["mint"][:6]
    entry = p.get("entry") or {}
    baseline = exit_monitor.full_exit(entry.get("rows") or [])
    if baseline is None:
        return f"Your {symbol} watch has no valid entry quote yet (the first quote failed), so there is no baseline to compare against."
    scale = 10 ** int(p.get("decimals") or 0)
    q0 = int(entry.get("quantity_raw") or p.get("quantity_raw") or 0)              # the baseline's holding, not the latest tick's
    head = f"# Since entry — {symbol}\n**Entry recorded**: {(entry.get('at') or p.get('created_at') or '')[:16].replace('T', ' ')} UTC · **Wallet**: `{p['wallet'][:6]}…{p['wallet'][-4:]}`\n"
    try:
        position = await exit_monitor.position_of(p["wallet"], p["mint"])
    except exit_monitor.BalanceUnavailable as exc:
        return head + f"\nYour current balance could not be read ({exc}), so neither the holding nor the exit can be compared right now. Nothing here is a price alert."
    if position is None:
        return head + (f"\n- **Holding**: {exit_monitor._qty(q0 / scale)} → 0 {symbol}: this wallet no longer holds the token, so there is no position to quote an exit for. "
                       f"Entry quote was {exit_monitor._usd(baseline['quoted_usdc'])}. Say `stop watching my exit on {symbol}` to close the watch.")
    rows = await exit_monitor.quote_exit(p["mint"], position["quantity_raw"])
    now_full = exit_monitor.full_exit(rows)
    q1 = int(position["quantity_raw"])
    lines = [head]
    if q0 and q1 != q0:
        lines.append(f"- **Holding**: {exit_monitor._qty(q0 / scale)} → {exit_monitor._qty(q1 / scale)} {symbol} ({(q1 - q0) / q0 * 100:+.1f}%).")
    else:
        lines.append(f"- **Holding**: unchanged at {exit_monitor._qty(q1 / scale)} {symbol}.")
    if now_full is None:
        failed = next((r for r in rows if r.get("fraction") == 1.0), {})
        lines.append(f"- **Exit liquidity**: the full-exit quote is unavailable right now ({failed.get('error', 'no quote')}); nothing can be said about the route until it answers.")
        lines.append(f"- **Reference price**: {exit_monitor._price(baseline.get('reference_price_usd'))} at entry; no current reading came back with the quote.")
    else:
        change = exit_monitor.explain_change(baseline, now_full)
        pc = change.get("price_change_pct")
        lines.append(f"- **Reference price**: {exit_monitor._price(baseline.get('reference_price_usd'))} → {exit_monitor._price(now_full.get('reference_price_usd'))}"
                     + (f" ({pc:+.1f}%)." if pc is not None else "."))
        lines.append(f"- **Full-exit quote**: {exit_monitor._usd(baseline['quoted_usdc'])} → {exit_monitor._usd(now_full['quoted_usdc'])} ({-change['drop_pct']:+.1f}%).")
        if change.get("discount_then_pct") is not None and change.get("discount_now_pct") is not None:
            w = change["discount_widening_pts"]
            lines.append(f"- **Route discount to the reference**: {change['discount_then_pct']:.1f}% → {change['discount_now_pct']:.1f}% "
                         + ("(wider: the book got thinner for your size)." if w > 0.05 else "(not wider)."))
        lines.append(f"- **Route**: {' → '.join(change['route_then']) or 'unknown'} → {' → '.join(change['route_now']) or 'unknown'}; price impact {change.get('impact_then_pct') or 0:.2f}% → {change.get('impact_now_pct') or 0:.2f}%.")
        if now_full.get("slot"):
            lines.append(f"- Quotes computed at slot {now_full['slot']} (Jupiter contextSlot).")
    lines += ["", "Two Jupiter quotes for your exact size, read apart: a price move, a holding change and a thinner book are different things. Not a sell instruction."]
    return "\n".join(lines)


def _rules_text(rules: dict) -> str:
    parts = []
    if rules.get("drop_pct") is not None:
        parts.append(f"drop {float(rules['drop_pct']):g}%")
    if rules.get("discount_pct") is not None:
        parts.append(f"discount {float(rules['discount_pct']):g}%")
    if rules.get("channel") == "email":
        parts.append("email")
    return f" · alerts: {', '.join(parts)}" if parts else ""


def _amounts(text: str) -> list[float]:
    out = []
    for m in _AMOUNT.finditer(text or ""):
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if value > 0 and value not in out:
            out.append(value)
    return out[:5]


def _clean(token: str | None) -> str | None:
    """A ticker as typed, without the $ and the sentence's punctuation
    ("ANSEM." was looked up with its full stop, UI run 2026-09-23)."""
    return token.lstrip("$").strip(".,!?;:") if token else token


def _token_of(m: re.Match) -> str:
    return _clean(next(g for g in m.groups() if g))


_resolved: dict[str, tuple[float, tuple]] = {}
_RESOLVE_TTL = 600.0


async def resolve_token(token: str) -> tuple[str | None, str | None, str | None]:
    """(mint, symbol, question). A mint resolves to itself; a symbol through
    Jupiter's registry: exactly one verified match wins, several ask. A
    resolution is kept for ten minutes, so "watch" right after "exit
    analysis" does not search again (live, 2026-09-22: the second search
    was rate-limited and the command failed)."""
    import time

    hit = _resolved.get(token.upper())
    if hit and hit[0] > time.monotonic():
        return hit[1]
    out = await _resolve_token(token)
    if out[2] is None:
        _resolved[token.upper()] = (time.monotonic() + _RESOLVE_TTL, out)
    return out


async def _resolve_token(token: str) -> tuple[str | None, str | None, str | None]:
    if _ADDRESS.match(token):
        try:
            item = await jupiter.token_by_mint(normalize_mint(token))
            return item["id"], item.get("symbol"), None
        except Exception:
            return token, None, None
    try:
        items = await jupiter.search_tokens(token)
    except Exception:
        return None, None, "I couldn't reach the token registry just now; try again in a moment."
    exact = [i for i in items if (i.get("symbol") or "").upper() == token.upper()]
    verified = [i for i in exact if "verified" in set(i.get("tags") or [])]
    pick = verified if verified else exact
    if len(pick) == 1:
        return pick[0]["id"], pick[0].get("symbol"), None
    if len(pick) > 1:
        options = ", ".join(f"`{i['id']}` ({i.get('name', '?')})" for i in pick[:4])
        return None, None, f"Several tokens use the symbol {token.upper()}: {options}. Paste the mint of the one you hold."
    return None, None, f"I couldn't find a token with the symbol {token.upper()} in Jupiter's registry. Paste its mint address."


async def _sized_exit(text: str, m: re.Match, focus: dict | None) -> str:
    amounts = _amounts(m.group("amount"))
    if m.group("amount") and m.group("amount").strip().lower().endswith("k") and amounts:
        amounts = [a * 1000 for a in amounts]
    named = _clean(m.group("t")) if m.group("t") else None
    focus = focus or {}
    token = named or (focus.get("address") if focus.get("kind") == "token" and focus.get("address") else focus.get("label") if focus.get("kind") in ("token", "topic") else None)
    if not token or not amounts:
        return "Which token, and at what dollar size? Name it (a $ticker or its mint) and I'll quote the exit at that size read-only, no wallet needed."
    mint, symbol, question = await resolve_token(token)
    if question:
        return question
    rows = await exit_monitor.size_comparison(mint, tuple(amounts))
    label = symbol or ((focus.get("label") if focus.get("label") and focus.get("label") != "TOKEN" else None) or named or mint[:6])
    out = exit_monitor.render_sizes(label, mint, rows)
    lead = (f"**Read-only sizing for a {'/'.join(exit_monitor._usd(a) for a in amounts)} position in {label}**: what the book charges to enter and to leave at that size right now, "
            "quoted without a wallet. Nothing is prepared or submitted.")
    if not named and re.search(r"\b(?:which|compare|better|versus|vs|both|each)\b", text, re.I):
        out += f"\n\n_Only {label} was sized, the token this conversation is about; name the other token and I'll size it the same way._"
    return f"{lead}\n\n{out}"


async def handle(message: str, user: dict | None, wallet: str | None, focus: dict | None = None) -> str | None:
    """The reply to an exit control, or None when the message is not one."""
    text = (message or "").strip()
    m = _SIZED_EXIT.match(text)
    if m:
        return await _sized_exit(text, m, focus)
    if user is None and not _SIZES.match(text):
        return "Sign in to use exit monitors and alerts; the sizing comparison works without an account."
    if _LIST.match(text):
        rows = [p for p in await exit_monitor.list_for(user["id"]) if p["status"] == "active"]
        if not rows:
            return "You are not watching any exits. Say `watch my exit on BONK` with a connected Solana wallet to start."
        lines = ["Watched exits:"]
        for i, p in enumerate(rows, start=1):
            full = exit_monitor.full_exit((p.get("entry") or {}).get("rows") or [])
            lines.append(f"{i}. **{p.get('symbol') or p['mint'][:6]}** in `{p['wallet'][:6]}…{p['wallet'][-4:]}` since {(p['created_at'] or '')[:10]}"
                         + (f" · full exit quoted {exit_monitor._usd(full['quoted_usdc'])} at entry" if full else "") + _rules_text(p.get("rules") or {}))
        return "\n".join(lines)
    m = _THRESHOLD.match(text)
    if m:
        pct = float(m.group("pct"))
        kind = "discount_pct" if m.group("what") else "drop_pct"
        token = _clean(m.group("before") or m.group("after"))
        if token and token.lower() in ("full", "position", "quote"):
            token = None
        rows = [p for p in await exit_monitor.list_for(user["id"]) if p["status"] == "active"]
        if token:
            mint, symbol, question = await resolve_token(token.lstrip("$"))
            if question:
                return question
            rows = [p for p in rows if p["mint"] == mint]
            if not rows:
                return f"You are not watching an exit on {symbol or token}. Say `watch my exit on {symbol or token}` first."
        if not rows:
            return "You are not watching any exits yet. Say `watch my exit on BONK` first, then set the rule."
        rules = {kind: pct}
        if m.group("verb").lower() == "email":
            rules["channel"] = "email"
        for p in rows:
            await exit_monitor.set_rules(p["id"], **rules)
        what = "the discount to the reference price exceeds" if kind == "discount_pct" else "a full exit is quoted"
        tail = f" {pct:g}%" if kind == "discount_pct" else f" {pct:g}% lower than the baseline"
        names = ", ".join(p.get("symbol") or p["mint"][:6] for p in rows)
        trigger = (f"Trigger: every {settings.exit_monitor_interval_minutes} minutes Jupiter is asked for a full-position sell quote at your exact size; "
                   + ("the discount is 1 − quoted proceeds ÷ marked value at the reference price, and the alert fires when it is at or above your rule"
                      if kind == "discount_pct" else "the alert fires when the quoted proceeds are at or below the baseline quote minus your rule")
                   + f", at most once per {settings.exit_alert_cooldown_hours:g} hours, into your inbox. It never sells and never places an order.")
        return f"Set: you will be told when {what}{tail} for {names}" + (", by email as well as here." if rules.get("channel") == "email" else ".") + "\n\n" + trigger
    m = _CHANNEL.match(text)
    if m:
        channel = "email" if (m.group("channel") or "email").lower() == "email" else "inapp"
        rows = [p for p in await exit_monitor.list_for(user["id"]) if p["status"] == "active"]
        if not rows:
            return "You are not watching any exits yet. Say `watch my exit on BONK` first."
        for p in rows:
            await exit_monitor.set_rules(p["id"], channel=channel)
        return f"Exit alerts for {len(rows)} position(s) will {'also go to your email' if channel == 'email' else 'stay in the app'}."
    m = _CHANGED.match(text)
    if m:
        token = next(g for g in m.groups() if g)
        mint, symbol, question = await resolve_token(token.lstrip("$"))
        if question:
            return question
        rows = [p for p in await exit_monitor.list_for(user["id"]) if p["status"] == "active" and p["mint"] == mint]
        if not rows:
            return (f"I have no entry snapshot for {symbol or token}: you are not watching an exit on it, so there is nothing to compare against. "
                    f"Say `watch my exit on {symbol or token}` with your wallet connected; from then on I can separate the price move, "
                    "your holding and the route's discount.")
        # The connected wallet's watch when it has one; otherwise every wallet
        # watching this token, each read on its own (review, 2026-09-23).
        mine = [p for p in rows if wallet and p["wallet"] == wallet]
        return "\n\n---\n\n".join([await _since_entry(p) for p in (mine or rows)])
    m = _SIZES.match(text)
    if m:
        token = _clean(m.group("t1") or m.group("t2") or m.group("t3"))
        amounts = _amounts(m.group("amounts") or m.group("amounts2") or m.group("amounts3") or "")
        if not token or not amounts:
            return "Name the token and the dollar amounts, for example: `compare buying $500, $2,000 and $5,000 of BONK`."
        mint, symbol, question = await resolve_token(token.lstrip("$"))
        if question:
            return question
        rows = await exit_monitor.size_comparison(mint, tuple(amounts))
        return exit_monitor.render_sizes(symbol or token.lstrip("$"), mint, rows)
    m = _STOP.match(text)
    if m:
        mint, symbol, question = await resolve_token(_token_of(m))
        if question:
            return question
        for p in await exit_monitor.list_for(user["id"]):
            if p["status"] == "active" and p["mint"] == mint and (not wallet or p["wallet"] == wallet):
                await exit_monitor.stop(p["id"], user["id"])
                return f"Stopped watching your {symbol or mint[:6]} exit."
        return f"You were not watching an exit on {symbol or mint[:6]}."
    m = _WATCH.match(text) or _ASK.match(text)
    if not m:
        return None
    if not wallet:
        return "Connect a Solana wallet first, or paste the wallet address, so I can read the position you actually hold."
    mint, symbol, question = await resolve_token(_token_of(m))
    if question:
        return question
    try:
        position = await exit_monitor.position_of(wallet, mint)
    except exit_monitor.BalanceUnavailable as exc:
        return f"I couldn't read `{wallet[:6]}…{wallet[-4:]}`'s balance from the chain just now ({exc}); that is not a zero. Try again in a moment."
    if position is None:
        return f"`{wallet[:6]}…{wallet[-4:]}` holds no {symbol or mint[:6]} on Solana, so there is no position to quote."
    rows = await exit_monitor.quote_exit(mint, position["quantity_raw"])
    existing = next((p for p in await exit_monitor.list_for(user["id"]) if p["status"] == "active" and p["mint"] == mint and p["wallet"] == wallet), None)
    if existing and not await exit_monitor.is_live(existing):
        # An active row whose monitor is gone (a registration that failed
        # halfway, a cancelled job): closed and registered afresh.
        await exit_monitor._update(existing["id"], status="closed")
        existing = None
    if _WATCH.match(text):
        if existing:
            card = exit_monitor.render_card({**existing, **position}, rows, existing.get("entry"), await exit_monitor.history(existing["id"]))
            return "Already watching this position.\n\n" + card
        try:
            watched = await exit_monitor.watch(user["id"], wallet, mint, symbol, position, rows)
        except ValueError as exc:
            return str(exc)
        card = exit_monitor.render_card({**watched, **position}, rows, watched.get("entry"))
        return (f"Watching your {symbol or mint[:6]} exit: the quote is re-taken every {exit_monitor.settings.exit_monitor_interval_minutes} minutes and you get an inbox "
                f"alert if a full exit is quoted {exit_monitor.settings.exit_alert_drop_pct:.0f}% lower than now.\n\n" + card)
    if existing:
        await exit_monitor.record(existing["id"], position["quantity_raw"], rows)
        return exit_monitor.render_card({**existing, **position}, rows, existing.get("entry"), await exit_monitor.history(existing["id"]))
    return exit_monitor.render_card({"wallet": wallet, "mint": mint, "symbol": symbol, **position}, rows)

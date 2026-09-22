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
from app.jupiter import jupiter, normalize_mint

_TOKEN = r"(\$?[A-Za-z][A-Za-z0-9._-]{1,15}|[1-9A-HJ-NP-Za-km-z]{32,44})"      # unnamed: the ask pattern uses it several times
_WATCH = re.compile(rf"^\s*(?:please\s+)?(?:watch|monitor|track)\s+my\s+(?:exit|position)\s+(?:on|for|in)\s+{_TOKEN}\s*[.!?]?\s*$", re.I)
_STOP = re.compile(rf"^\s*(?:stop|cancel|end)\s+(?:watching|monitoring|tracking)\s+(?:my\s+)?(?:exit|position)\s+(?:on|for|in)\s+{_TOKEN}\s*[.!?]?\s*$", re.I)
_ASK = re.compile(rf"^\s*(?:(?:exit\s+analysis|exit\s+check)\s+(?:for|on)\s+{_TOKEN}|can\s+i\s+(?:still\s+)?(?:exit|get\s+out\s+of)\s+(?:my\s+)?{_TOKEN}(?:\s+position)?"
                  rf"|how(?:'s|\s+is)\s+my\s+exit\s+(?:on|for|in)\s+{_TOKEN}|what\s+would\s+(?:a\s+)?(?:full|25%|50%|half|100%)?\s*exit\s+(?:of|from)\s+(?:my\s+)?{_TOKEN}\s+(?:return|get\s+me))\s*[?.!]?\s*$", re.I)
_LIST = re.compile(r"^\s*(?:show|list|what\s+are)\s+(?:me\s+)?my\s+(?:exits|watched\s+positions|exit\s+monitors)\s*\??\s*$", re.I)
# "tell me when the discount on my full-position exit quote exceeds 5%",
# "alert me when my BONK exit drops 10%", "email me when my exit on WIF falls 15%"
_THRESHOLD = re.compile(
    r"^\s*(?P<verb>tell|alert|notify|email|warn)\s+me\s+(?:when|if)\s+(?:the\s+)?(?P<what>discount\s+(?:on|of)\s+)?my\s+(?:full[- ]position\s+)?"
    rf"(?:(?P<before>{_TOKEN})\s+)?exit(?:\s+quote)?(?:\s+(?:on|for|in)\s+(?P<after>{_TOKEN}))?\s+(?:exceeds|is\s+(?:more|higher|greater)\s+than|goes\s+(?:above|over|past)|widens\s+(?:past|beyond)|drops|falls|declines|deteriorates)(?:\s+by|\s+more\s+than|\s+over)?\s+(?P<pct>\d+(?:\.\d+)?)\s*%\s*[.!?]?\s*$", re.I)
_CHANNEL = re.compile(r"^\s*(?:(?:email|send)\s+me\s+my\s+exit\s+alerts(?:\s+by\s+email)?|(?:send\s+)?(?:my\s+)?exit\s+alerts\s+(?:by|via|to)\s+(?P<channel>email|inbox|app))\s*[.!?]?\s*$", re.I)
# "compare buying $500, $2,000 and $5,000 of BONK", "size check BONK at $1000", "what would $250 of WIF cost to enter and exit"
_SIZES = re.compile(
    rf"^\s*(?:compare\s+buying\s+(?P<amounts>[\$\d,.\s]+(?:and|or)?[\$\d,.\s]*)\s+(?:of|worth\s+of|in)\s+{_TOKEN}"
    rf"|size\s+check\s+{_TOKEN}\s+(?:at|for|with)\s+(?P<amounts2>[\$\d,.\s]+(?:and|or)?[\$\d,.\s]*)"
    rf"|what\s+would\s+(?P<amounts3>\$[\d,.]+)\s+(?:of|in)\s+{_TOKEN}\s+cost\s+to\s+(?:enter\s+and\s+exit|buy\s+and\s+sell|get\s+in\s+and\s+out(?:\s+of)?))\s*[?.!]?\s*$", re.I)
_AMOUNT = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d+)?)")
_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def is_exit_control(message: str) -> bool:
    return any(p.match(message or "") for p in (_WATCH, _STOP, _ASK, _LIST, _THRESHOLD, _CHANNEL, _SIZES))


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


def _token_of(m: re.Match) -> str:
    return next(g for g in m.groups() if g).lstrip("$")


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


async def handle(message: str, user: dict, wallet: str | None) -> str | None:
    """The reply to an exit control, or None when the message is not one."""
    text = (message or "").strip()
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
        token = m.group("before") or m.group("after")
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
        return f"Set: you will be told when {what}{tail} for {names}" + (", by email as well as here." if rules.get("channel") == "email" else ".")
    m = _CHANNEL.match(text)
    if m:
        channel = "email" if (m.group("channel") or "email").lower() == "email" else "inapp"
        rows = [p for p in await exit_monitor.list_for(user["id"]) if p["status"] == "active"]
        if not rows:
            return "You are not watching any exits yet. Say `watch my exit on BONK` first."
        for p in rows:
            await exit_monitor.set_rules(p["id"], channel=channel)
        return f"Exit alerts for {len(rows)} position(s) will {'also go to your email' if channel == 'email' else 'stay in the app'}."
    m = _SIZES.match(text)
    if m:
        token = next((g for g in m.groups() if g and not any(ch.isdigit() for ch in g) and g.lower() not in ("and", "or")), None)
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

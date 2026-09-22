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
_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def is_exit_control(message: str) -> bool:
    return any(p.match(message or "") for p in (_WATCH, _STOP, _ASK, _LIST))


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
                         + (f" · full exit quoted {exit_monitor._usd(full['quoted_usdc'])} at entry" if full else ""))
        return "\n".join(lines)
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
    position = await exit_monitor.position_of(wallet, mint)
    if position is None:
        return f"`{wallet[:6]}…{wallet[-4:]}` holds no {symbol or mint[:6]} on Solana, so there is no position to quote."
    rows = await exit_monitor.quote_exit(mint, position["quantity_raw"])
    existing = next((p for p in await exit_monitor.list_for(user["id"]) if p["status"] == "active" and p["mint"] == mint and p["wallet"] == wallet), None)
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

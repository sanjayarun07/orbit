"""What the product can do when the user asks it to operate itself.

"Save this investigation", "export an evidence-linked report", "show my
token watchlist ranked by changes" are requests to the application, not
market look-ups. Until 2026-09-23 they reached the token path (SAVE, MY and
SEPARATE were looked up as tickers). The classifier now names them `app`
and this module answers with what exists and what does not, so the user is
never told a feature exists that does not.
"""
from __future__ import annotations

import re

_SAVE = re.compile(r"\b(?:save|bookmark|keep|pin|revisit|store)\b", re.I)
_EXPORT = re.compile(r"\b(?:export|download|pdf|csv|report|share)\b", re.I)
_WATCHLIST = re.compile(r"\b(?:watch\s*list|watchlist|ranked|tracked\s+tokens|my\s+tokens)\b", re.I)
_ALERTS = re.compile(r"\b(?:alerts?|notif\w+|remind\w*|email\s+me|tell\s+me\s+when)\b", re.I)

SAVED = ("**Saving.** Every conversation is kept automatically under Recents in the sidebar: rename, pin or archive it from its menu, "
         "and reopen it later with its answers and cards intact. There is no separate \"save investigation\" action to take.")
EXPORT = ("**Exporting a report.** There is no evidence-linked report export yet. What exists: the answer and its cards stay in the "
          "conversation; Settings › Data & privacy exports your whole account as JSON (conversations included). A report with sources, "
          "timestamps and coverage gaps per investigation is on the roadmap, not available today.")
WATCHLIST = ("**Watchlist.** There is no ranked token watchlist yet. What exists today: `watch my exit on BONK` keeps a position-specific "
             "exit monitor (quotes re-taken every 15 minutes, alerts on deterioration, rules you set by chat), `show my exits` lists them, "
             "and Settings › Tasks holds price alerts, reminders and the morning brief. A list ranked by liquidity, concentration, "
             "deployer activity and exit quotes is on the roadmap, not available today.")
ALERTS = ("**Alerts.** Exit alerts: `watch my exit on BONK`, then `tell me when the discount on my full-position exit quote exceeds 5%`, "
          "`email me when my BONK exit drops 10%` or `exit alerts to inbox`. Price alerts, reminders and the morning brief live in "
          "Settings › Tasks; low-credit and receipt emails in Settings › Notifications.")
GENERIC = ("This reads as a request to the app rather than a market look-up. What Orbit can do for you here: conversations are saved "
           "automatically under Recents; exit monitors and alerts are set by chat (`watch my exit on BONK`); Settings › Tasks holds price "
           "alerts and reminders; Settings › Data & privacy exports your account. Tell me which of these you meant, or name a token, "
           "wallet or protocol to research.")


def answer(request: str) -> str:
    text = request or ""
    parts = []
    if _SAVE.search(text):
        parts.append(SAVED)
    if _EXPORT.search(text):
        parts.append(EXPORT)
    if _WATCHLIST.search(text):
        parts.append(WATCHLIST)
    if _ALERTS.search(text) and not _WATCHLIST.search(text):
        parts.append(ALERTS)
    if not parts:
        return GENERIC
    return "\n\n".join(dict.fromkeys(parts)) + "\n\nNothing was saved, exported or changed by this message."

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
_WHAT_CAN = re.compile(r"\b(?:what\s+can\s+(?:i|you)\s+do|what\s+do\s+you\s+(?:do|support|offer)|how\s+do\s+i\s+use|how\s+does\s+this\s+work|new\s+to\s+crypto|without\s+(?:connecting\s+)?(?:a\s+)?wallet|getting\s+started|where\s+do\s+i\s+start)\b", re.I)

WHAT_CAN = ("**What you can do here without connecting a wallet.** Ask about any token, protocol or market in plain words: a price, what is "
            "trending, whether a token looks safe, who holds it, the news behind a move, how something in DeFi works. Answers come with the "
            "data cards they are built from, and every conversation is kept under Recents.\n\n"
            "**With a public wallet address** (paste it; no signing, no control given), you can see its holdings, value and health, quote what "
            "a position would fetch if sold, and watch an exit for deterioration.\n\n"
            "**What never happens on its own:** no trade is placed, no funds move, nothing is signed. Trades are only ever prepared for your "
            "review and signed by you in your own wallet; here that is off in research mode. Start with `how is the crypto market today` or "
            "`what does TVL mean`.")

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


def is_product_question(request: str) -> bool:
    """A question about what this product does or how to use it."""
    text = request or ""
    return bool(_WHAT_CAN.search(text)) and not re.search(r"\b(?:price|holders?|liquidity|volume|market\s+cap|tvl)\b", text, re.I)


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
    if _WHAT_CAN.search(text) and not parts:
        return WHAT_CAN
    if not parts:
        return GENERIC
    return "\n\n".join(dict.fromkeys(parts)) + "\n\nNothing was saved, exported or changed by this message."

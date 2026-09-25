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
_FIND_CHAT = re.compile(r"\b(?:find|reopen|open|get\s+back\s+to|return\s+to|see)\s+(?:this|that|my|the|an?\s+(?:old|earlier|previous))\s+(?:conversation|chat|thread)s?\b|\bconversation\s+again\b|\bwhere\s+(?:can\s+i|do\s+i|is)\s+(?:find\s+)?(?:this|my|the)\s+(?:conversation|chat|history)\b", re.I)
_EXIT_WATCH_MEANING = re.compile(r"\bexit\s+watch(?:es)?\b.{0,80}\b(?:stop[- ]loss|limit\s+order|protect|sell\s+for\s+me|automatic|trigger|guarantee)\b|\b(?:stop[- ]loss|limit\s+order)\b.{0,80}\bexit\s+watch\b", re.I)
_MARKED_VS_PROCEEDS = re.compile(r"\bdifference\s+between\b.{0,60}\b(?:portfolio\s+value|marked\s+value|reference\s+valu\w+|holdings?\s+value)\b.{0,80}\b(?:receive|get|proceeds|selling|sell|exit)\b", re.I)

FIND_CHAT = ("**Finding this conversation again.** It is under Recents in the sidebar (the menu button on a phone), newest first, with a "
             "search box; pin it from its ⋯ menu to keep it at the top, or rename it. Recents live in this browser; the transcript itself is "
             "kept on your account, so signing in elsewhere and opening the link restores it.\n\n"
             "**Downloading your data.** Settings › Data & privacy › Export downloads your account as JSON: conversations, tasks, wallets, "
             "memory. There is no per-conversation report export yet.")
EXIT_WATCH = ("**What an exit watch does.** Every 15 minutes it asks Jupiter for a sell quote of your exact position and compares the "
              "quoted proceeds with the quote at entry; when they fall past your rule (20% by default, or a discount rule you set) it posts "
              "to your inbox, or emails you if you asked. It watches executable exit value, not a price level.\n\n"
              "**What it is not.** It is not a stop-loss and not a limit order: nothing is ever sold, no order rests on any venue, and a "
              "move between two checks is not caught until the next check. If liquidity vanishes inside the 15-minute gap, the first you "
              "hear is the next quote. Treat it as an early-warning read on whether you can still get out, and decide yourself.")
MARKED_VS_PROCEEDS = ("**Portfolio value** is a reference: your quantity times a quoted reference price, as if the whole position could be sold at "
                      "that price with no effect on it.\n\n**What you could receive** is a route's answer for your exact size right now: the "
                      "sale walks through pools, each fill moves the price, so the quoted proceeds sit below the reference, and the minimum "
                      "out is lower still by your slippage tolerance. Network fees are on top. The gap grows with position size and shrinks "
                      "with depth; a thin token can show a large reference value and a fraction of it as proceeds.\n\n"
                      "Ask `exit analysis for X` for both numbers on your own position: marked value, quoted proceeds at 25/50/100%, "
                      "minimum out, price impact and route.")


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


# "Can I see a wallet without giving you control of it?": a question about how
# this product reads wallets, not a wallet to read (frozen trust run,
# 2026-09-23: it fetched the portfolio of the token contract in focus).
_WALLET_VIEW = re.compile(r"\b(?:see|view|look\s+at|read|check|analy[sz]e|track)\b.{0,40}\b(?:a|any|someone'?s?|another|an?\s+public)\s+(?:wallet|address|portfolio)\b.{0,60}"
                          r"\b(?:without\s+(?:giving|granting|handing|connecting|signing|control|access|approv\w+)|read[- ]only|just\s+(?:the\s+)?address|public\s+address|no\s+(?:control|signing|access))\b"
                          r"|\b(?:read[- ]only|view[- ]only)\s+(?:wallet|address|portfolio|mode)\b", re.I)
WALLET_VIEW = ("**Yes: a wallet can be read from its public address alone.** Paste the address (Solana or EVM) and Orbit reads what is public "
               "on-chain: holdings and their value, open positions, recent transfers, health checks and what a position would fetch if sold. "
               "Nothing is connected, nothing is signed, and no access or control is given; the address is read the way a block explorer "
               "reads it.\n\n"
               "**Connecting a wallet is different.** It is only needed to sign a transaction yourself, and only when execution is on; even "
               "then Orbit never holds a key, and every transaction is shown for review in your own wallet before you sign.\n\n"
               "Try: paste an address followed by `what does it hold`.")


# "What happens if the quote provider times out or there is no route? Does
# your system treat either as zero holdings or authorize a retry trade?": a
# question about this product's behaviour, answered from what the code does
# (app/exit_monitor._route_error), not with the policy card (frozen trust
# run, 2026-09-23).
_QUOTE_FAILS = re.compile(r"\b(?:quote|route|routing|price)\s+(?:provider|source|api|feed)?\s*(?:times?\s+out|timeout|fails?|failure|unavailable|down|errors?)\b|\bno\s+route\b", re.I)
_SYSTEM_BEHAVIOUR = re.compile(r"\b(?:what\s+happens|what\s+do\s+you\s+do|does\s+(?:your|the|this)\s+(?:system|app|bot|orbit)|do\s+you\s+(?:treat|retry|re-?try|authori[sz]e|assume)|"
                               r"treat(?:ed|s)?\s+(?:it|that|either|this|as)|as\s+zero|zero\s+holdings|retry\s+trade|authori[sz]e\s+a\s+retry|in\s+that\s+case)\b", re.I)


def _asks_quote_failure_behaviour(text: str) -> bool:
    return bool(_QUOTE_FAILS.search(text) and _SYSTEM_BEHAVIOUR.search(text))


QUOTE_FAILURE = ("**A failed quote is unknown, never zero, and never a trade.**\n\n"
                 "- **Timeout, dropped connection, provider outage or rate limit**: the quote is recorded as *unavailable* with the reason "
                 "(\"quote unavailable (timeout)\", \"quote provider outage\"). Holdings are untouched: the last known quantity stands, "
                 "the gap is recorded, and no alert fires on unavailable data.\n"
                 "- **No route**: only when the quote provider itself says so (Jupiter's COULD_NOT_FIND_ANY_ROUTE). For an exit watch that "
                 "is the exit-risk alert in its plainest form: \"a full exit was quoted at X and cannot be quoted now\", with the smaller "
                 "sizes that still quote.\n"
                 "- **Missing chain read**: not a zero and not a position; the card says the chain could not be read.\n\n"
                 "Nothing retries a trade. A quote is read-only; a swap is only ever prepared for your review and signed by you in your own "
                 "wallet, and a quote older than its validity window is replaced by a fresh one, never re-used. In research mode no swap is "
                 "prepared at all.")


# "Summarize the first Home market headline", "Summarize today's Home
# headlines; which source published each and when?": the tiles on Home are
# product state, answered from the tiles themselves ("Home" was looked up as
# a token, expanded UI review 2026-09-24).
_HOME_HEADLINES = re.compile(r"\b(?:home\s+(?:market\s+|news\s+)?(?:headlines?|tiles?|cards?|strip)|(?:today'?s|the\s+current|the\s+latest)\s+(?:home\s+)?headlines?\b.{0,40}\b(?:home|tiles?|cards?)|headlines?\s+on\s+(?:the\s+)?home)\b", re.I)
_ORDINAL_PICK = re.compile(r"\b(first|second|third|fourth|1st|2nd|3rd|4th|last)\b", re.I)


def picked_headline(request: str) -> str | None:
    """The title of the Home headline a product answer summarised ("the
    first Home market headline"), so the next turn's "that event" points at
    it; None when the request is not a headline pick."""
    if not _HOME_HEADLINES.search(request or ""):
        return None
    pick = _ORDINAL_PICK.search(request or "")
    if not pick or re.search(r"\b(?:all|each|every|which)\b", request or "", re.I):
        return None
    from app import home_highlights
    data = home_highlights.get_highlights()
    cards = [c for c in (data.get("cards") or []) + (data.get("meme_cards") or []) if c.get("title") and c.get("kind") in ("crypto", "stocks", "memes")]
    if not cards:
        return None
    order = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "last": len(cards) - 1}
    return cards[min(order.get(pick.group(1).lower(), 0), len(cards) - 1)]["title"]


def listed_headlines(request: str) -> str | None:
    """The titles a product answer listed ("summarize today's Home
    headlines"), joined, so "is this old news?" points at them."""
    if not _HOME_HEADLINES.search(request or "") or picked_headline(request):
        return None
    from app import home_highlights
    data = home_highlights.get_highlights()
    titles = [c["title"] for c in (data.get("cards") or []) + (data.get("meme_cards") or []) if c.get("title") and c.get("kind") in ("crypto", "stocks", "memes")]
    return "; ".join(titles[:4]) if titles else None


_FEED_COVERAGE = re.compile(r"\b(?:can|does|do|will|is|could)\s+(?:your|the|this)\s+(?:feed|data\s+feed|source|ledger|tick\s+data)\b|\byour\s+(?:feed|ledger|tick\s+data)\b[^?]{0,40}\b(?:support|cover|handle|resolution|interval|granular\w*|go\s+back|history|span)", re.I)


def asks_feed_coverage(request: str) -> bool:
    """A question about what the feed itself can answer, not a data ask."""
    return bool(_FEED_COVERAGE.search(request or ""))


FEED_COVERAGE = ("The Tequity feed covers Aster and Hyperliquid pairs: live snapshots every few seconds for 24h figures, and a tick ledger "
                 "(about every 5 minutes) for any window from 30 minutes to 30 days inside its span. Ask `what does your feed cover` for the live span.")


_DETERIORATION = re.compile(r"\b(?P<pct>\d{1,2}(?:\.\d+)?)\s*(?:%|percent)\s+(?:deterioration|decline|drop|worse|worsening)\b.{0,60}?\b(?:alert|watch|rule|threshold)\b.{0,80}?\$\s*(?P<usd>\d[\d,]*(?:\.\d+)?)\s*(?:position|stake|bag|holding)?"
                            r"|\b(?:alert|watch|rule|threshold)\b.{0,60}?\b(?P<pct2>\d{1,2}(?:\.\d+)?)\s*(?:%|percent)\s+(?:deterioration|decline|drop|worse|worsening)\b.{0,80}?\$\s*(?P<usd2>\d[\d,]*(?:\.\d+)?)", re.I | re.S)


def deterioration_answer(request: str) -> str | None:
    """What an N% deterioration alert means for a $X position: the alert is
    on quoted exit proceeds, never on the token's price (live UI test
    2026-09-25: a 20% deterioration was explained as a 20% price fall)."""
    m = _DETERIORATION.search(request or "")
    if not m:
        return None
    pct = float(m.group("pct") or m.group("pct2"))
    usd = float((m.group("usd") or m.group("usd2")).replace(",", ""))
    threshold = usd * (1 - pct / 100)
    return (f"A {pct:g}% deterioration alert watches the **quoted exit proceeds**, not the token price. Orbit re-takes a full-exit quote for your "
            f"position on a schedule and compares it with the baseline quote taken when the watch started. For a position whose full exit quoted "
            f"${usd:,.2f} at the baseline, the alert fires when a full exit quotes below **${threshold:,.2f}** ({pct:g}% less), whatever moved it: "
            f"price, liquidity or route. A price fall of {pct:g}% does not fire it by itself if the quote holds, and thinning liquidity can fire it "
            f"without the price moving. Nothing was created by this question.")


def home_headlines_answer(request: str) -> str:
    from app import home_highlights
    data = home_highlights.get_highlights()
    cards = [c for c in (data.get("cards") or []) + (data.get("meme_cards") or []) if c.get("title") and c.get("kind") in ("crypto", "stocks", "memes")]
    if not cards:
        return "Home is showing no news headlines right now (the tiles are the live market cards). Ask for the crypto market today, or name a story."
    pick = _ORDINAL_PICK.search(request or "")
    if pick and not re.search(r"\b(?:all|each|every|which)\b", request or "", re.I):
        order = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "last": len(cards) - 1}
        c = cards[min(order.get(pick.group(1).lower(), 0), len(cards) - 1)]
        return (f"**{c['title']}**\n\n{c.get('summary') or ''}\n\n"
                f"Event date: {c.get('date') or 'not stated by the source'} · Source: {c.get('source') or 'not stated'} · "
                f"Checked: {str(data.get('as_of') or '')[:16].replace('T', ' ')} UTC · {'Verified against its source' if c.get('verified') else 'Not verified against a source'}.\n\n"
                f"For the market read, tap the tile or ask: `{c.get('prompt') or 'What does this mean for the market: ' + c['title']}`")
    lines = ["**Today's Home headlines** (event date · source · checked)", ""]
    for i, c in enumerate(cards, start=1):
        lines.append(f"{i}. **{c['title']}** · {c.get('date') or 'no date'} · {c.get('source') or 'no source'} · {'verified' if c.get('verified') else 'unverified'}")
    lines += ["", f"Checked at {str(data.get('as_of') or '')[:16].replace('T', ' ')} UTC. The date is the day the event happened as the source printed it; the tiles are re-read every 30 minutes, so the publication time of each source is on the source's own page."]
    return "\n".join(lines)


def matches(request: str) -> bool:
    """Whether a specific product answer exists for this request. The
    classifier's `app` label stands only then; otherwise the ask is research
    (live run 2026-09-23: "using saved snapshots" was read as an app action)."""
    return answer(request) != GENERIC


def is_product_question(request: str) -> bool:
    """A question about what this product does or how to use it, or about one
    of its own concepts (an exit watch, marked value against sale proceeds)."""
    text = request or ""
    if _FIND_CHAT.search(text) or _EXIT_WATCH_MEANING.search(text) or _MARKED_VS_PROCEEDS.search(text) or _WALLET_VIEW.search(text) or _asks_quote_failure_behaviour(text) or _HOME_HEADLINES.search(text) or _FEED_COVERAGE.search(text) or _DETERIORATION.search(text):
        return True
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
    if _FIND_CHAT.search(text):
        return FIND_CHAT
    if _EXIT_WATCH_MEANING.search(text):
        return EXIT_WATCH
    if _MARKED_VS_PROCEEDS.search(text):
        return MARKED_VS_PROCEEDS
    if _WALLET_VIEW.search(text):
        return WALLET_VIEW
    if _asks_quote_failure_behaviour(text):
        return QUOTE_FAILURE
    if _HOME_HEADLINES.search(text):
        return home_headlines_answer(text)
    if _FEED_COVERAGE.search(text):
        return FEED_COVERAGE
    if _DETERIORATION.search(text):
        return deterioration_answer(text)
    if _WHAT_CAN.search(text) and not parts:
        return WHAT_CAN
    if not parts:
        return GENERIC
    return "\n\n".join(dict.fromkeys(parts)) + "\n\nNothing was saved, exported or changed by this message."

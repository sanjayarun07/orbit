"""Deterministic entity carry-over for safe conversational follow-ups."""

from __future__ import annotations

from dataclasses import dataclass
import re
from solders.pubkey import Pubkey


_EVM_ADDRESS = r"0x[0-9a-fA-F]{40}"
_SOLANA_ADDRESS = r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])"
_ANY_ADDRESS = re.compile(rf"{_EVM_ADDRESS}|{_SOLANA_ADDRESS}")
_LABELED_TOKEN_ADDRESS = re.compile(
    rf"\b(?:solana\s+)?(?:token\s+)?(?:mint|contract)(?:\s+address)?\s*(?::|is)?\s*({_EVM_ADDRESS}|{_SOLANA_ADDRESS})",
    re.IGNORECASE,
)
_TOKEN_WORD = re.compile(r"\b(?:token|coin|contract|mint|memecoin|meme coin|erc-?20)\b", re.IGNORECASE)
# "I mean SPX6900 the meme token, not the index.": a correction of the previous
# request's subject re-runs that request about the corrected subject
# (expanded journeys, 2026-09-24: the correction itself went to the web).
_SUBJECT_CORRECTION = re.compile(r"^\s*(?:no,?\s+|sorry,?\s+|actually,?\s+)?i\s+mean(?:t)?\s+(?P<new>\$?[A-Za-z][A-Za-z0-9._-]{1,15})\b\s*(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)


_ITEM_LINE = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*•])\s+(.+?)\s*$")


def answer_items(answer: str, limit: int = 8) -> list[str]:
    """Items the user actually saw, for a later "which of those".

    Prefer the written answer. When its summary was withheld, the visible
    numbered items live in the first evidence card instead. Read that card's
    answer section, stopping before its bibliography so source numbers do not
    become conversation referents.
    """
    written, _, evidence = (answer or "").partition("\n---\n")

    def listed(text: str) -> list[str]:
        found = []
        for line in text.splitlines():
            m = _ITEM_LINE.match(line)
            if not m:
                continue
            if re.match(r"\*\*[^*]{1,48}\*\*\s*:", m.group(1)):
                # A card's field ("**Largest position**: `9WzD…`") is a fact
                # about one subject, not an item the user can pick from: the
                # deployer card's three fields had become "those" and its full
                # address a token to rank (live probe 2026-09-27).
                continue
            item = re.sub(r"\*\*|`|\[(\d+)\]", "", m.group(1)).strip()
            if len(item) >= 12 and not item.lower().startswith(("time:", "web_discovery", "knowledge", "finance_discovery")):
                found.append(item[:320])
        return found[:limit]

    def tabled(text: str) -> list[str]:
        """The rows of the card's first table ("which of those are exchanges"
        after a holders table: the rows are the items, expanded run 2026-09-25)."""
        found = []
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else []
            if len(cells) < 2 or all(re.fullmatch(r":?-+:?", c) for c in cells if c) or cells[0].lstrip("#").strip().lower() in ("", "#", "rank"):
                if not (cells and re.fullmatch(r"\d+", cells[0])):
                    continue
            row = " · ".join(re.sub(r"\*\*|`", "", c) for c in cells[:4] if c and c != "—")
            if len(row) >= 6:
                found.append(row[:320])
        return found[:limit]

    items = listed(written)
    if not evidence:
        return items
    first_card = re.split(r"(?m)^#{1,6}\s+", evidence, maxsplit=2)
    body = first_card[1] if len(first_card) > 1 else evidence
    body = re.split(r"(?im)^\s*(?:sources?|references?)\s*:\s*$", body, maxsplit=1)[0]
    card_items = listed(body) or tabled(body)
    # Some generated summaries put "1. ... 2. ... 3. ..." on one line.
    # The numbered evidence card then represents the actual visible set more
    # faithfully than a single truncated summary-line match.
    return card_items if len(items) < 2 and len(card_items) > len(items) else items


_THOSE = re.compile(r"\b(?:which|what|how many)\s+of\s+(?:those|these|them)\b|\b(?:those|these)\s+(?:events?|items?|projects?|candidates?|sources?|headlines?|tokens?|ones)\b|\b(?:for|of|about)\s+each\s+(?:candidate|item|one|of\s+(?:those|these|them))\b", re.I)


_FULL_ADDRESS = re.compile(r"\b(?:0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})\b")


def _short_addresses(text: str) -> str:
    """Addresses in a note shortened to what the user saw: the planner reads
    a full address anywhere in the request as the subject of a new contract
    (a holder's wallet became a token to rank, live probe 2026-09-27)."""
    return _FULL_ADDRESS.sub(lambda m: f"{m.group(0)[:4]}…{m.group(0)[-4:]}", text or "")


def conversation_subject(session_context: dict | None) -> str | None:
    """What the conversation is about now: the carried contract's subject,
    else the focus's address or label."""
    ctx = session_context or {}
    contract_subject = ((ctx.get("last_contract") or {}).get("subject") or {}).get("id")
    focus = ctx.get("focus") or {}
    return contract_subject or focus.get("address") or focus.get("label") or None


def referent_items(request: str, session_context: dict | None) -> tuple[list[str] | None, str | None, str | None]:
    """(items, the ask they answered, a question) for a "which of those" ask.

    The nearest relevant, evidence-backed list: the previous answer's items
    when it listed any; else the newest earlier list, only while the
    conversation's subject is still the one that list was about (after the
    deployer card, "those" are still the holders). An older list about
    something else is a question, never a guess (user rule, 2026-09-27).
    """
    ctx = session_context or {}
    if not _THOSE.search(request or ""):
        return None, None, None
    items = ctx.get("last_items") or []
    if items:
        return list(items), None, None
    lists = [entry for entry in (ctx.get("item_lists") or []) if isinstance(entry, dict) and entry.get("items")]
    if not lists:
        return None, None, None
    newest = lists[-1]
    subject_now = conversation_subject(ctx)
    if newest.get("subject") and subject_now and newest["subject"] == subject_now:
        return list(newest["items"]), newest.get("request") or None, None
    options = "; ".join(f"the {len(e['items'])} items answering \"{(e.get('request') or 'an earlier question')[:80]}\"" for e in reversed(lists[-3:]))
    return None, None, (f"Which items do you mean? The last answer listed none, and the lists earlier in this conversation were about something else: {options}. "
                        "Name the list, or ask the question with the items in it.")


def items_note(request: str, session_context: dict | None) -> str | None:
    items, source, _question = referent_items(request, session_context)
    if not items:
        return None
    listed = "; ".join(f"({i + 1}) {_short_addresses(t)}" for i, t in enumerate(items))
    where = f"the answer to \"{source[:80]}\" listed earlier in this conversation" if source else "the previous answer listed"
    return f"Resolved from conversation context: \"those\" are the items {where} -- {listed} -- answer about exactly these, in this order; never substitute a different set."


def referent_question(request: str, session_context: dict | None) -> str | None:
    """The clarifying question when "those" has no clear list; None otherwise."""
    return referent_items(request, session_context)[2]


def _last_user_request(history: str, current: str, session_context: dict | None = None) -> str | None:
    """The previous user turn: from the session context first (the bounded
    history text drops the user line after a long answer, live 2026-09-24),
    else from the history (never this turn)."""
    remembered = ((session_context or {}).get("last_request") or "").strip()
    if remembered and remembered != (current or "").strip():
        return remembered
    turns = [line[len("user: "):].strip() for line in (history or "").splitlines() if line.startswith("user: ")]
    turns = [t for t in turns if t and t != (current or "").strip()]
    return turns[-1] if turns else None


_THEME_PRONOUN = re.compile(r"\b(?:it|this|that|these|those|the\s+same)\b", re.IGNORECASE)


def themed_followup(request: str, conversation_history: str, session_context: dict | None = None) -> str | None:
    """'how it works in akash network' after 'how else can we tie a dual token
    to the main token': the previous question named no subject, so "it" is
    what that question was about, applied to the subject named now (live,
    2026-09-24: Orbit explained Akash in general; the theme was dual tokens)."""
    from app.routing.subject_probe import subject_of
    from app.routing.subject_probe import is_referent
    focus = (session_context or {}).get("focus") or {}
    if not _THEME_PRONOUN.search(request or ""):
        return None
    if focus and (is_referent(request) or (focus.get("kind") == "token" and focus.get("address"))):
        return None                      # a referent question ("does that authorize X?") and a resolved token are the focus's (the referent carry); a leftover topic under "how it works in X" is not
    subject = subject_of(request)
    if not subject:
        return None
    previous = _last_user_request(conversation_history, request, session_context)
    if not previous or subject_of(previous):
        return None                      # the previous question had its own subject: the focus carry handles "it"
    # The rewritten question leads: every reader of the request -- the web
    # search's query, the contract planner, the router -- takes its first
    # line as the ask, and a note under a bare "how it works in X" still
    # fetched "what X is" (live, 2026-09-24).
    theme = previous.strip().rstrip("?.! ")
    return (f"In {subject}: {theme}?\nResolved from conversation context: the user wrote \"{request.strip()}\"; \"it\" is what the previous question was about "
            f"(\"{previous.strip()}\"), so this asks how {subject} does that -- answer {subject}'s version of that, not what {subject} is in general.")


_METRIC_WORDS = {"liquidity", "holders", "holder", "volume", "price", "prices", "pools", "pairs", "supply", "mcap", "marketcap", "fdv", "tvl", "apy", "yield", "yields",
                 "trades", "buyers", "sellers", "whales", "concentration", "security", "safety", "unlocks", "vesting", "news", "sentiment", "funding", "liquidations"}


def metric_corrected_request(request: str, conversation_history: str, session_context: dict | None = None) -> str | None:
    """'Actually, I meant liquidity, not holders' after '$BONK holders?': the
    subject stays BONK and the metric changes -- a metric word is never the
    new subject (live UI test 2026-09-25: it looked up a token called LIQUIDITY)."""
    from app.routing.subject_probe import subject_of
    m = _SUBJECT_CORRECTION.match(request or "")
    if not m or m.group("new").lstrip("$").lower() not in _METRIC_WORDS:
        return None
    metric = m.group("new").lstrip("$").lower()
    focus = (session_context or {}).get("focus") or {}
    previous = _last_user_request(conversation_history, request, session_context) or ""
    subject = (focus.get("label") if focus.get("label") and focus.get("label") != "TOKEN" else None) or subject_of(previous)
    if not subject:
        return None
    rest = re.sub(r"^\s*,?\s*not\s+\w+[.,;!]?\s*", "", m.group("rest").strip(" .,;!"), flags=re.I).strip()
    mint = f" {focus['address']}" if focus.get("kind") == "token" and focus.get("address") else ""
    chain = f" on {focus['chain']}" if focus.get("chain") else ""
    return (f"{metric} of {subject}{mint}{chain}" + (f". {rest}" if rest else "") +
            f"\nResolved from conversation context: the user corrected the metric of the previous request (\"{previous}\") to {metric}; the subject stays {subject}.")


def corrected_request(request: str, conversation_history: str, session_context: dict | None = None) -> str | None:
    """The previous request re-targeted at the corrected subject, or None."""
    from app.routing.subject_probe import subject_of
    m = _SUBJECT_CORRECTION.match(request or "")
    if not m:
        return None
    if m.group("new").lstrip("$").lower() in _METRIC_WORDS:
        return None                      # a metric correction, not a subject correction (metric_corrected_request)
    previous = _last_user_request(conversation_history, request, session_context)
    if not previous:
        return None
    new = m.group("new").lstrip("$")
    old = subject_of(previous)
    if not old or old.lower() == new.lower() or not re.search(rf"\b{re.escape(old)}\b", previous):
        return None
    rest = m.group("rest").strip(" .,;!")
    # "the SPX6900 token": the token word travels with the name so the
    # resolver reads it as a ticker (digits alone are not one).
    replacement = f"the {new} token" if _TOKEN_WORD.search(rest) else new
    rerun = re.sub(rf"\b{re.escape(old)}\b", replacement, previous)
    return (f"{rerun}\nResolved from conversation context: the user corrected the subject of the previous request from {old} to {new}"
            + (f" ({rest})" if rest else "") + "; answer that request about the corrected subject.")
# A message that's just a pronoun reference back to the canonical focus --
# "it"/"that"/"this", optionally "... one". Deliberately anchored (^...$)
# only where used standalone (_PRONOUN.match on an already-isolated capture
# group); the phrase-level patterns below embed the same alternation
# in-line instead of anchoring the whole message.
_PRONOUN = re.compile(r"^(?:it|that|this)(?:\s+one)?$", re.IGNORECASE)
_TOKEN_FOLLOWUP = re.compile(
    r"\b(?:this|that|the)\s+(?:token|coin|contract)\b"
    r"|\b(?:its|their)\s+(?:(?:top|largest|biggest|current|recent|five|ten|twenty|\d+)\s+){0,2}(?:holders?|liquidity|volume|price|trades?|safety|risk)\b"
    # Bare pronoun follow-ups ("how about that one?", "tell me more about it") --
    # verified live these previously resolved to nothing at all, unlike the
    # "this/that + noun" phrasing above, which was already handled.
    r"|\b(?:how|what)\s+about\s+(?:it|that|this)(?:\s+one)?\b"
    r"|\b(?:tell\s+me\s+more\s+about|check|analyze)\s+(?:it|that|this)(?:\s+one)?\b"
    # "is it safe?", "is that one legit?" -- a bare-pronoun safety follow-up.
    r"|\bis\s+(?:it|that|this)(?:\s+one)?\s+(?:a\s+)?(?:safe|legit|rug|scam|honeypot)\b",
    re.IGNORECASE,
)
_WALLET_FOLLOWUP = re.compile(
    r"\b(?:this|that|the)\s+(?:wallet|address|portfolio)\b"
    r"|\b(?:its|their)\s+(?:transactions?|balances?|holdings?|counterparties|pnl|leverage|positions?|assets?)\b"
    # "Which assets does it hold?", "what's in it?", "the ANSEM amount there": the wallet in focus (expanded UI review, 2026-09-24)
    r"|\b(?:does|do|did)\s+(?:it|they)\s+(?:hold|own|have|contain)\b|\bwhat(?:'s|\s+is)\s+in\s+(?:it|there)\b|\bamount\s+(?:there|in\s+it)\b|\bassets?\s+(?:in|of)\s+(?:it|that|there)\b"
    r"|\b(?:how|what)\s+about\s+(?:it|that|this)(?:\s+one)?\b"
    r"|\b(?:tell\s+me\s+more\s+about|check|analyze)\s+(?:it|that|this)(?:\s+one)?\b",
    re.IGNORECASE,
)
_WALLET_WORD = re.compile(
    r"\b(?:wallet|address|portfolio|holdings?|balances?|transactions?|counterparties|pnl|leverage|positions?)\b",
    re.IGNORECASE,
)
_PROFILER_ADDRESS = re.compile(rf"profiler\?address=({_EVM_ADDRESS}|{_SOLANA_ADDRESS})", re.IGNORECASE)
_CHAINS = re.compile(
    r"\b(solana|ethereum|base|arbitrum|optimism|polygon|bnb|bsc|avalanche|sui|tron|robinhood(?:\s+chain)?)\b",
    re.IGNORECASE,
)
_NAMED_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])\$?([A-Za-z][A-Za-z0-9._-]{1,15})\s+"
    r"(?:token|coin|memecoin|meme\s+coin)\b",
    re.IGNORECASE,
)
_NAMED_TRADE_TARGET = re.compile(
    # An optional quantity phrase ("half of", "50% of", "all") between the verb
    # and the actual target -- without this, "sell half of it" captured "half"
    # as the target instead of "it" (verified live).
    r"\b(?:buy|swap(?:\s+[^\n]{0,40}?\s+to)?|sell|trade|exchange)\s+"
    r"(?:(?:all|half|most|some)\s+(?:of\s+)?|\d+(?:\.\d+)?%?\s+(?:of\s+)?)?"
    r"\$?([A-Za-z][A-Za-z0-9._-]{1,15})\b",
    re.IGNORECASE,
)


_ORDINAL_WORDS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "four": 3,
    "fifth": 4, "5th": 4, "five": 4,
}
_ORDINAL = re.compile(
    r"\b(?:the\s+|number\s+|#|option\s+)?(" + "|".join(_ORDINAL_WORDS) + r"|[1-5])\b(?:\s+one)?",
    re.IGNORECASE,
)


def _chain_key(chain: str) -> str:
    """Canonicalize a chain label so a DEX Screener chainId and the user's
    wording compare equal (bsc<->bnb, and case)."""
    key = (chain or "").strip().lower()
    return {"bsc": "bnb"}.get(key, key)


def resolve_pending_token(request: str, pending: dict) -> str | None:
    """Match a disambiguation reply to one of the pending token candidates and
    rewrite the ORIGINAL request with that token's address+chain, so the data
    question re-runs against the chain the user picked. Returns None when the
    reply names no candidate (the caller then treats it as a fresh query).

    A reply may pick a candidate by pasted contract, by chain name ("Base"), or
    by position ("the second one", "2").
    """
    candidates = pending.get("candidates") or []
    original = pending.get("original_request") or ""
    if not candidates or not original:
        return None
    chosen: dict | None = None
    # 1. Pasted contract address that matches one of the candidates.
    for addr in _ANY_ADDRESS.findall(request):
        chosen = next((c for c in candidates if str(c.get("address", "")).lower() == addr.lower()), None)
        if chosen:
            break
    # 2. A named chain.
    if chosen is None:
        wanted = {_chain_key(m) for m in _CHAINS.findall(request)}
        if wanted:
            chosen = next((c for c in candidates if _chain_key(c.get("chain", "")) in wanted), None)
    # 3. A position ("the second one", "2"). Only when the reply is essentially
    #    just the ordinal -- avoids a stray number in a fresh question selecting.
    if chosen is None and len(request.split()) <= 4:
        match = _ORDINAL.search(request)
        if match:
            token = match.group(1).lower()
            index = _ORDINAL_WORDS.get(token, int(token) - 1 if token.isdigit() else -1)
            if 0 <= index < len(candidates):
                chosen = candidates[index]
    if chosen is None:
        return None
    return f"{original} {chosen['address']} on {chosen['chain']}"


@dataclass(frozen=True)
class TokenReference:
    address: str
    chain: str | None


@dataclass(frozen=True)
class WalletReference:
    address: str
    chain: str | None


_URL = re.compile(r"https?://\S+")


def _plain(text: str) -> str:
    """Markdown emphasis and links removed: an address inside a source URL
    (etherscan.io/address/0x…) is a link, not a wallet the conversation named
    (UI review, 2026-09-23: a "wallet" chip appeared from a citation)."""
    return _URL.sub(" ", text.replace("`", "").replace("*", ""))


def _valid_address(value: str) -> bool:
    if value.startswith("0x"):
        return bool(re.fullmatch(_EVM_ADDRESS, value))
    try:
        Pubkey.from_string(value)
        return True
    except ValueError:
        return False


def _addresses(text: str) -> list[re.Match]:
    return [match for match in _ANY_ADDRESS.finditer(text) if _valid_address(match.group(0))]


def _chain_for(address: str, text: str) -> str | None:
    if not address.startswith("0x"):
        return "solana"
    # A 0x address can never exist on Solana -- "solana" can still appear in
    # the combined request/answer/history text for an unrelated reason (a
    # Hyperliquid section, an earlier unrelated mention, ...), and this looks
    # at the *last* chain word in that text, not something scoped to the
    # address itself. Without this exclusion an EVM wallet's context chip
    # (and anything built from it) gets mislabeled "solana".
    matches = [match for match in _CHAINS.finditer(text) if match.group(1).lower() != "solana"]
    if not matches:
        return None
    chain = matches[-1].group(1).lower()
    if chain == "bsc":
        return "bnb"
    if chain.startswith("robinhood"):
        return "robinhood"
    return chain


def extract_token_reference(*texts: str) -> TokenReference | None:
    """Find an explicitly labelled or token-qualified address, newest text first."""
    for raw in texts:
        if not raw:
            continue
        text = _plain(raw)
        text = re.sub(r"https?://[^\s)<>]+", "", text)
        labeled = [
            match
            for match in _LABELED_TOKEN_ADDRESS.finditer(text)
            if _valid_address(match.group(1))
        ]
        if labeled:
            address = labeled[-1].group(1)
            return TokenReference(address, _chain_for(address, text))
        if _TOKEN_WORD.search(text):
            addresses = _addresses(text)
            if addresses:
                address = addresses[-1].group(0)
                return TokenReference(address, _chain_for(address, text))
    return None


def extract_wallet_reference(*texts: str) -> WalletReference | None:
    """Find the latest wallet address without mistaking a linked token contract for it."""
    context = "\n".join(_plain(raw) for raw in texts if raw)
    for raw in texts:
        if not raw:
            continue
        text = _plain(raw)

        # Nansen profiler links identify their address as a wallet even when the
        # same answer also contains token contract links.
        profiler = list(_PROFILER_ADDRESS.finditer(text))
        if profiler:
            address = profiler[-1].group(1)
            return WalletReference(address, _chain_for(address, context))

        user_lines = re.findall(r"(?mi)^user:\s*(.+)$", text)
        candidates = reversed(user_lines) if user_lines else [text]
        for candidate in candidates:
            addresses = _addresses(candidate)
            if not addresses:
                continue
            # A bare address in a user turn is a wallet candidate. An address
            # described as a token/contract/mint is deliberately excluded.
            stripped = candidate.strip()
            bare_address = bool(re.fullmatch(rf"(?:this address\s+)?({_EVM_ADDRESS}|{_SOLANA_ADDRESS})", stripped, re.IGNORECASE))
            if not _TOKEN_WORD.search(candidate) and (bare_address or user_lines or _WALLET_WORD.search(candidate)):
                address = addresses[-1].group(0)
                return WalletReference(address, _chain_for(address, context))
    return None


def resolve_contextual_request(
    request: str, conversation_history: str, session_context: dict | None = None
) -> str:
    """Attach prior token or wallet identity to an unambiguous follow-up."""
    pending = (session_context or {}).get("pending_token")
    if pending:
        # The previous turn asked which chain a symbol is on. If this reply picks
        # a candidate, re-run the original data question against that token; the
        # pending state is cleared afterwards regardless (main.py rewrites it from
        # this turn's outcome), so a non-matching reply falls through as fresh.
        resolved = resolve_pending_token(request, pending)
        if resolved is not None:
            return resolved
    request_addresses = _addresses(request)
    if not conversation_history or request_addresses:
        if not session_context or request_addresses:
            return request
    focus = (session_context or {}).get("focus") or {}
    # A named execution follow-up may omit the chain and mint because the
    # immediately preceding token research already established them. Bind the
    # subject only when its name matches the canonical focus; never infer from
    # an unrelated historical token.
    trade_target = _NAMED_TRADE_TARGET.search(request)
    focus_label = str(focus.get("label") or "")
    named_research = re.search(r"\b(?:about|analy[sz]e|research)\s+\$?([A-Za-z][A-Za-z0-9._-]{1,15})\b", request, re.I)
    if named_research and focus.get("kind") == "token" and (
        named_research.group(1).casefold() == focus_label.casefold()
        or _PRONOUN.match(named_research.group(1))
    ):
        address = f" mint {focus['address']}" if focus.get("address") else ""
        chain = f" on {focus['chain']}" if focus.get("chain") else ""
        return f"{request}\nResolved subject: token {focus_label}{address}{chain}."
    if (
        trade_target
        and focus.get("kind") == "token"
        and focus.get("chain")
        and (
            (focus_label and trade_target.group(1).lower() == focus_label.lower())
            or _PRONOUN.match(trade_target.group(1))
        )
    ):
        address = f" with mint {focus['address']}" if focus.get("address") else ""
        return (
            f"{request}\nResolved from canonical session context: "
            f"token {focus_label}{address} on {focus['chain']}."
        )
    if _TOKEN_FOLLOWUP.search(request):
        if focus.get("kind") == "token" and focus.get("address"):
            chain = f" on {focus['chain']}" if focus.get("chain") else ""
            return f"{request}\nResolved from canonical session context: token {focus['address']}{chain}."
        reference = extract_token_reference(conversation_history)
        if reference is not None:
            chain = f" on {reference.chain}" if reference.chain else ""
            return f"{request}\nResolved from conversation context: token {reference.address}{chain}."
    if _WALLET_FOLLOWUP.search(request):
        wallet = (session_context or {}).get("connected_wallet") or {}
        if focus.get("kind") == "wallet" and focus.get("address"):
            wallet = focus
        if wallet.get("address"):
            chain = f" on {wallet['chain']}" if wallet.get("chain") else ""
            return f"{request}\nResolved from canonical session context: wallet {wallet['address']}{chain}."
        reference = extract_wallet_reference(conversation_history)
        if reference is not None:
            chain = f" on {reference.chain}" if reference.chain else ""
            return f"{request}\nResolved from conversation context: wallet {reference.address}{chain}."
    # A follow-up that names nothing of its own continues the conversation's
    # subject: "Were early buys bundled or sniped?" after an ANSEM turn is
    # about ANSEM; "Build bull, base and bear cases" after EigenLayer is about
    # EigenLayer (UI run, 2026-09-23: they became new questions or new assets).
    from app.routing.subject_probe import continues_subject

    last = (session_context or {}).get("last_contract") or {}
    from app.tequity import fuzzy_venue
    names_last_venue = bool(last.get("venue")) and any(w.lower() == last["venue"] or fuzzy_venue(w) == last["venue"] for w in re.findall(r"[A-Za-z]{4,}", request))
    corrects_last_venue = names_last_venue and re.match(r"\s*(?:i\s+mean|i\s+meant|no,?\s|not\b|only\b|just\b|actually\b)", request, re.I) is not None
    place = last.get("venue") or ((last.get("subject") or {}).get("chain") if last.get("kind") == "market_ranking" else None)
    if place and not focus.get("address") and (continues_subject(request) or corrects_last_venue):
        # A correction to the previous ranking ask ("I meant the past hour",
        # "now make it seven days, same Base DEX scope") keeps the place, the
        # kind and the metric; the words of this turn change only what they
        # say. The carried ask leads the request, because the planner reads
        # the ask and never the note (live UI test 2026-09-25: a seven-day
        # follow-up on Base gainers became Base DEX volume).
        direction = last.get("direction")
        what = {"market_ranking": {"gainers": "gainers", "losers": "losers"}.get(direction, "top movers") if last.get("metric") != "volume" else "most traded pairs",
                "holders": "holders", "yields": "yields"}.get(last.get("kind"), last.get("kind") or "data")
        scope = " (tokenized stocks only)" if (last.get("filters") or {}).get("stocks_only") else " (crypto only)" if (last.get("filters") or {}).get("crypto_only") else ""
        return (f"{what} on {place}{scope}: {request.strip()}"
                f"\nResolved from canonical session context: the previous ask was {what} on {place}{scope}; this continues it there with only the change stated here.")
    metric_fix = metric_corrected_request(request, conversation_history, session_context)
    if metric_fix:
        return metric_fix
    corrected = corrected_request(request, conversation_history, session_context)
    if corrected:
        return corrected
    themed = themed_followup(request, conversation_history, session_context)
    if themed:
        return themed
    those = items_note(request, session_context)
    if those:
        # Keep the user's question as the contract planner's body. A prior
        # headline may mention a whale or ticker without asking for holders;
        # promoting that list into the ask changes the route. The web query
        # explicitly incorporates the resolved set separately.
        return f"{request.strip()}\n{those}"
    if focus.get("label") and continues_subject(request):
        if focus.get("kind") == "token" and focus.get("address"):
            chain = f" on {focus['chain']}" if focus.get("chain") else ""
            return f"{request}\nResolved from canonical session context: token {focus_label}{' mint ' + focus['address'] if focus_label else focus['address']}{chain}."
        if focus.get("source") == "home_headline":
            # A Home headline's words are not an ask ("as yields rise" sent
            # "when did that happen?" to the yields tool, 2026-09-24).
            return (f"{request}\nResolved from conversation context: this continues the discussion about the Home news headline \"{focus['label']}\" "
                    "(a news story: answer about the story, its event date and its sources, never about a word in the headline).")
        if focus.get("kind") == "topic":
            return (f"{request}\nResolved from conversation context: this continues the discussion about {focus['label']} "
                    "(the subject of the previous turns: a protocol, company or topic, not a token symbol to look up).")
    return request

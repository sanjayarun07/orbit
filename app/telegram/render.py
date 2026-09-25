"""An AgentResponse, as Telegram sees it.

Telegram's HTML is a short list: bold, italic, underline, strike, spoiler,
link, code, pre, blockquote. There are no headings, no lists and no tables, so
the conversion is lossy on purpose and the losses are chosen here rather than
left to a generic library: a heading becomes bold, a bullet becomes "•", and a
markdown table becomes a monospace block, because a pipe table rendered as
prose on a phone is unreadable.

Splitting happens on the MARKDOWN, before conversion, so a message boundary can
never fall inside a tag -- the failure mode that makes Telegram reject the
whole message with "can't parse entities" and show the user nothing.
"""

from __future__ import annotations

import html
import re

from app.models import AgentResponse
from app.telegram.client import MAX_MESSAGE_CHARS

# Below Telegram's 4096 so a footer or a "(1/3)" marker cannot push a chunk over.
CHUNK_TARGET = 3500

_FENCE = re.compile(r"^```")
_CODE_BLOCK = re.compile(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# Underscore italics only between word boundaries, so snake_case identifiers
# and a mint address with an underscore are left alone.
_ITALIC_STAR = re.compile(r"(?<![\*\w])\*(?!\s)([^\*\n]+?)(?<!\s)\*(?![\*\w])")
_ITALIC_UNDER = re.compile(r"(?<![_\w])_(?!\s)([^_\n]+?)(?<!\s)_(?![_\w])")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_RULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DIVIDER = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_PLACEHOLDER = "\x00CODE{}\x00"


def to_html(markdown: str) -> str:
    """Markdown as the answer model writes it -> Telegram HTML."""
    blocks: list[str] = []

    def _stash(match: re.Match) -> str:
        blocks.append(match.group(1))
        return _PLACEHOLDER.format(len(blocks) - 1)

    text = _CODE_BLOCK.sub(_stash, markdown or "")
    lines = text.split("\n")
    out: list[str] = []
    table: list[str] = []

    def _flush_table() -> None:
        if not table:
            return
        out.append(f"<pre>{html.escape(_format_table(table))}</pre>")
        table.clear()

    for line in lines:
        if _TABLE_ROW.match(line):
            table.append(line)
            continue
        _flush_table()
        if _RULE.match(line):
            continue
        heading = _HEADING.match(line)
        if heading:
            out.append(f"<b>{_inline(heading.group(1))}</b>")
            continue
        quote = _QUOTE.match(line)
        if quote:
            out.append(f"<blockquote>{_inline(quote.group(1))}</blockquote>")
            continue
        bullet = _BULLET.match(line)
        if bullet:
            out.append(f"{bullet.group(1)}• {_inline(line[bullet.end():])}")
            continue
        out.append(_inline(line))
    _flush_table()

    rendered = "\n".join(out)
    for index, block in enumerate(blocks):
        rendered = rendered.replace(
            _PLACEHOLDER.format(index), f"<pre>{html.escape(block.rstrip())}</pre>"
        )
    return _collapse_blank_lines(rendered).strip()


def _inline(text: str) -> str:
    """One line: escape first, then re-introduce only the tags we emit."""
    codes: list[str] = []

    def _stash_code(match: re.Match) -> str:
        codes.append(match.group(1))
        return _PLACEHOLDER.format(len(codes) - 1)

    text = _INLINE_CODE.sub(_stash_code, text)
    links: list[tuple[str, str]] = []

    def _stash_link(match: re.Match) -> str:
        links.append((match.group(1), match.group(2)))
        return f"\x00LINK{len(links) - 1}\x00"

    text = _LINK.sub(_stash_link, text)
    text = html.escape(text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _ITALIC_STAR.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_UNDER.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    for index, (label, url) in enumerate(links):
        text = text.replace(
            f"\x00LINK{index}\x00", f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
        )
    for index, code in enumerate(codes):
        text = text.replace(_PLACEHOLDER.format(index), f"<code>{html.escape(code)}</code>")
    return text


def _format_table(rows: list[str]) -> str:
    """A pipe table as aligned columns, so it survives a phone in monospace."""
    parsed = [
        [cell.strip() for cell in row.strip().strip("|").split("|")]
        for row in rows
        if not _TABLE_DIVIDER.match(row)
    ]
    if not parsed:
        return ""
    width = max(len(row) for row in parsed)
    parsed = [row + [""] * (width - len(row)) for row in parsed]
    # Markdown emphasis inside a cell would render literally in <pre>.
    parsed = [[_BOLD.sub(r"\1", cell).replace("`", "") for cell in row] for row in parsed]
    widths = [max(len(row[col]) for row in parsed) for col in range(width)]
    lines = ["  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)).rstrip() for row in parsed]
    if len(lines) > 1:
        lines.insert(1, "  ".join("-" * widths[col] for col in range(width)).rstrip())
    return "\n".join(lines)


def _collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)


def split_markdown(markdown: str, limit: int = CHUNK_TARGET) -> list[str]:
    """Split on paragraph boundaries, never inside a fenced code block."""
    text = (markdown or "").strip()
    if not text:
        return []
    paragraphs, current, in_fence = [], [], False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        if line.strip() == "" and not in_fence and current:
            paragraphs.append("\n".join(current))
            current = []
            continue
        current.append(line)
    if current:
        paragraphs.append("\n".join(current))

    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
            buffer = ""
        if paragraph.lstrip().startswith("```") and len(paragraph) > limit:
            # A code block longer than a message. Cutting it like prose would
            # leave an unterminated fence, which renders the rest of the
            # answer as code; instead it becomes several complete blocks.
            chunks.extend(_split_fence(paragraph, limit))
            continue
        while len(paragraph) > limit:
            # One paragraph longer than a whole message: break at the last
            # newline, then the last space, and only then mid-text.
            window = paragraph[:limit]
            cut = max(window.rfind("\n"), window.rfind(" "))
            cut = cut if cut > limit // 2 else limit
            chunks.append(paragraph[:cut])
            paragraph = paragraph[cut:].lstrip()
        buffer = paragraph
    if buffer:
        chunks.append(buffer)
    return chunks


def _split_fence(paragraph: str, limit: int) -> list[str]:
    """An over-long fenced block as several blocks, each opened and closed."""
    lines = paragraph.split("\n")
    opening = lines[0].strip() if lines and lines[0].lstrip().startswith("```") else "```"
    body = [line for line in lines[1:] if not line.strip().startswith("```")]
    # The fence markers and newlines are part of every chunk's budget.
    budget = max(limit - len(opening) - 8, 200)
    chunks, current, size = [], [], 0
    for line in body:
        if current and size + len(line) + 1 > budget:
            chunks.append(f"{opening}\n" + "\n".join(current) + "\n```")
            current, size = [], 0
        current.append(line[:budget])
        size += len(current[-1]) + 1
    if current:
        chunks.append(f"{opening}\n" + "\n".join(current) + "\n```")
    return chunks


def render_answer(response: AgentResponse) -> list[str]:
    """The answer as one or more Telegram-ready HTML messages, footer last."""
    chunks = [to_html(chunk) for chunk in split_markdown(response.answer)]
    chunks = [chunk for chunk in chunks if chunk]
    if not chunks:
        chunks = ["<i>No answer was produced for that.</i>"]
    footer = render_footer(response)
    if footer:
        if len(chunks[-1]) + len(footer) + 2 <= MAX_MESSAGE_CHARS:
            chunks[-1] = f"{chunks[-1]}\n\n{footer}"
        else:
            chunks.append(footer)
    return chunks


def render_footer(response: AgentResponse) -> str:
    """Sources, freshness and the validator's verdict on one line.

    The web UI has a panel for this; a chat has a line. It is kept because an
    answer whose provenance is invisible reads like every other bot in the
    group, and because the validator's `warn` is the user's cue to ask again.
    """
    parts: list[str] = []
    evidence = response.evidence
    if evidence and evidence.providers:
        shown = ", ".join(html.escape(name) for name in evidence.providers[:4])
        extra = len(evidence.providers) - 4
        parts.append(f"{shown}{f' +{extra}' if extra > 0 else ''}")
    validation = response.validation
    if validation:
        if validation.as_of:
            parts.append(f"as of {html.escape(validation.as_of)}")
        if validation.status == "warn":
            parts.append("⚠️ unverified figures")
    if response.team_mode:
        parts.append("desk")
    return f"<i>{' · '.join(parts)}</i>" if parts else ""


def render_trade_plan(response: AgentResponse) -> str | None:
    """The review card. Deliberately terse and deliberately not a confirmation:
    nothing in Telegram can sign, and the card must not imply otherwise."""
    plan = response.trade_plan
    if plan is None:
        return None
    decimals = plan.input_token.decimals or 0
    amount = plan.proposal.amount_atomic / (10 ** decimals) if decimals else plan.proposal.amount_atomic
    lines = [
        "<b>Swap quote</b>",
        f"{amount:,.6g} {html.escape(plan.input_token.symbol or '?')}"
        f" → {html.escape(plan.output_token.symbol or '?')}",
    ]
    if plan.input_value_usd:
        lines.append(f"Value: ${plan.input_value_usd:,.2f}")
    readiness = response.trade_readiness
    if readiness:
        lines.append(f"Readiness: {html.escape(readiness.level.replace('_', ' '))}")
        for warning in readiness.warnings[:3]:
            lines.append(f"⚠️ {html.escape(warning)}")
    for warning in plan.warnings[:3]:
        lines.append(f"⚠️ {html.escape(warning)}")
    lines.append(f"<i>Expires {plan.expires_at.strftime('%H:%M UTC')}. Nothing is signed yet.</i>")
    return "\n".join(lines)


def answer_key(answer: str) -> str:
    """A short key for one answer, carried in its buttons and recomputed from
    the persisted message when a button is pressed."""
    import hashlib
    return hashlib.sha256((answer or "").encode("utf-8")).hexdigest()[:10]


def keyboard(response: AgentResponse, *, app_url: str | None = None) -> dict | None:
    """Quick actions as callback buttons, suggestions as a second rank.

    An ordinary answer carries no link out: it is finished in the chat, and a
    button pointing away from every reply is noise. Only a quote gets one,
    because signing has to happen in a browser.

    Callback data is `qa:<index>` and nothing else. The action itself is read
    back from the conversation the server persisted it on, so a forged or
    replayed callback can only ever name a position in a list the server wrote
    -- and `execute_chat_turn` still checks the reconstructed action byte for
    byte against that list before routing it.
    """
    rows: list[list[dict]] = []
    if response.trade_plan is not None and app_url:
        # `url`, not `web_app`: a web_app button opens Telegram's in-app
        # webview, which has its own cookie jar -- the user arrives signed out
        # of the account they are signed into in their browser, and extension
        # wallets (MetaMask, Phantom) do not exist there at all. A plain link
        # opens the real browser, where the session and the wallet already are.
        rows.append([{"text": "🔐 Review and sign", "url": app_url}])
    key = answer_key(response.answer or "")
    for index, action in enumerate(response.quick_actions[:6]):
        label = action.entity_label or action.prompt
        rows.append([{"text": _button_label(label), "callback_data": f"qa:{key}:{index}"}])
    for index, suggestion in enumerate(response.suggestions[:3]):
        if len(response.quick_actions) + index >= 8:
            break
        rows.append([{"text": _button_label(suggestion), "callback_data": f"sg:{key}:{index}"}])
    # No "Open Anvaya" on an ordinary answer. It was on every reply, where it
    # is noise: a research answer is finished in the chat, and the web is for
    # the few things the chat cannot do -- /app and /account offer it there.
    return {"inline_keyboard": rows} if rows else None


def _button_label(text: str) -> str:
    """Telegram truncates hard and centres, so a long label becomes unreadable."""
    label = " ".join((text or "").split())
    return label if len(label) <= 40 else f"{label[:37]}…"

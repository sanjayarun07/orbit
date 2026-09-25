"""An audit of the previous answer: which of it was observed live and which
part the model wrote over the cards.

"For the previous answer, which fact was observed live and which part was an
inference?" is a question about this conversation's own answer, not a topic
to research (live UI test 2026-09-25: it was sent to the web, which reported
that "the previous answer is not visible"). The audit reads the answer the
session kept: the cards under the rule are what was fetched, with their
provider and time stamps; each sentence of the written summary is labelled
by what supports it -- figures the cards carry, a cited source, or nothing
(the model's own interpretation). Nothing is fetched for it.
"""
from __future__ import annotations

import re

from app import fact_gate

_PROVENANCE = re.compile(
    r"\b(?:which|what)\s+(?:facts?|parts?|figures?|numbers?|claims?|statements?|bits?|of\s+(?:that|this|it|the\s+(?:previous|last|prior)\s+answer))\b"
    r"[^?]{0,120}?\b(?:observed|live|measured|fetched|verified|read\s+directly|from\s+(?:a\s+|the\s+)?(?:source|tool|card|data))\b"
    r"[^?]{0,120}?\b(?:inference|inferred|inferences|assum\w+|guess\w*|interpret\w+|reasoning|estimat\w+|derived)\b", re.I | re.S)
_PROVENANCE_REVERSED = re.compile(
    r"\b(?:which|what)\s+(?:facts?|parts?|figures?|numbers?|claims?|statements?|bits?)\b[^?]{0,120}?\b(?:inference|inferred|assum\w+|guess\w*|interpret\w+|derived)\b"
    r"[^?]{0,120}?\b(?:observed|live|measured|fetched|verified|from\s+(?:a\s+|the\s+)?(?:source|tool|card|data))\b", re.I | re.S)


def is_provenance_ask(request: str) -> bool:
    """A question partitioning the previous answer into what was observed
    and what was inferred."""
    text = request or ""
    return bool(_PROVENANCE.search(text) or _PROVENANCE_REVERSED.search(text))


_CARD_HEAD = re.compile(r"^#+\s+(.+)$", re.M)
_STAMP_LINE = re.compile(r"(?:\*\*)?(?:Data freshness|As of|Provider|Source|Query|Fetched|Snapshot|From tick|Quote time)(?:\*\*)?\s*[:=]", re.I)
_MARKER = re.compile(r"\[(\d{1,3})\]")
_TIMING = re.compile(r"^\s*-\s*time:.*$", re.M)


def _split(answer: str) -> tuple[str, str]:
    cut = (answer or "").find("\n---\n")
    if cut < 0:
        return answer or "", ""
    return answer[:cut], answer[cut + 5:]


def _cards(evidence: str) -> list[tuple[str, str, int]]:
    """(title, the stamp line, table rows) per card under the rule."""
    out = []
    heads = list(_CARD_HEAD.finditer(evidence))
    for i, head in enumerate(heads):
        body = evidence[head.end(): heads[i + 1].start() if i + 1 < len(heads) else len(evidence)]
        stamp = next((line.strip() for line in body.splitlines() if _STAMP_LINE.search(line)), "")
        rows = sum(1 for line in body.splitlines() if line.strip().startswith("|") and not re.match(r"^\|\s*-", line.strip()) and "---" not in line) - (1 if "|" in body else 0)
        out.append((head.group(1).strip(), re.sub(r"\*\*", "", stamp)[:160], max(rows, 0)))
    return out


def _sentences(synthesis: str) -> list[str]:
    text = _TIMING.sub("", fact_gate.prose_of(synthesis))
    text = re.sub(r"\*\*Taken together\*\*", " ", text)
    text = re.sub(r"\*\*", "", text)
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip().split()) >= 4]
    return parts[:14]


def _has_figure(sentence: str) -> bool:
    for m in fact_gate._FIGURE.finditer(fact_gate._DATETIME.sub(" ", sentence)):
        token = m.group(0).strip().rstrip(",.")
        n = fact_gate._parse(token)
        if n is None or fact_gate._YEARLIKE.match(token) or (abs(n) <= 12 and "%" not in token and "$" not in token and token[-1:].lower() not in "kmb"):
            continue
        return True
    return False


def audit(previous: str | None) -> str:
    """The audit text for the previous answer; a plain statement when the
    session holds none."""
    if not (previous or "").strip():
        return "There is no previous answer in this conversation to audit yet. Ask something, and I can then say which of that answer was observed and which was inferred."
    synthesis, evidence = _split(previous)
    cards = _cards(evidence)
    lines = ["**Audit of the previous answer.** Nothing was fetched for this; it reads that answer as it was written.", ""]
    if cards:
        lines.append("**Observed live** -- the evidence cards under the rule, as fetched, with their own stamps:")
        for title, stamp, rows in cards:
            detail = f" -- {stamp}" if stamp else ""
            rows_note = f" ({rows} table rows)" if rows else ""
            lines.append(f"- **{title}**{detail}{rows_note}")
    else:
        lines.append("**Observed live:** nothing. That answer carried no evidence card, so none of its statements was a live observation in this conversation; "
                     "treat all of it as written, not fetched.")
    sentences = _sentences(synthesis)
    if sentences:
        lines += ["", "**Written over the cards** -- each sentence of the summary, with what supports it:"]
        for s in sentences:
            markers = sorted({int(m) for m in _MARKER.findall(s)})
            unsupported = fact_gate.unsupported_figures(s, [], evidence_text=evidence) if cards else []
            if markers:
                label = f"cited to source {', '.join(f'[{m}]' for m in markers)} of the web card: a source's claim, not a live observation"
            elif not _has_figure(s):
                label = "interpretation: the model's wording over the cards, no figure of its own"
            elif unsupported:
                label = f"no card carries exactly {', '.join(unsupported)} -- treat that figure as unsupported"
            elif cards:
                label = "figures observed: each number in it is in a card above"
            else:
                label = "figures with no card behind them"
            quoted = s if len(s) <= 300 else s[:300].rsplit(" ", 1)[0] + " …"
            lines.append(f"- \"{quoted}\" -- {label}")
    return "\n".join(lines)

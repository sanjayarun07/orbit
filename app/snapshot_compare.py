"""A dated comparison is answered from the snapshot ledger, or it says so.

"What changed in ANSEM between September 21 and September 22?" was answered
with today's cards under a lead that still said "between" (UI run,
2026-09-23). The ledger (app/holder_snapshots.py) is the only source of a
past state; when it has rows at both ends the comparison is drawn from them
and anchored to their times, and when it does not the answer opens with
that limitation and labels everything after it as current data.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app import holder_snapshots
from app.signals import Subject

_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE = re.compile(
    rf"\b(?:(\d{{4}})-(\d{{2}})-(\d{{2}})|{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?|(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH}(?:,?\s+(\d{{4}}))?)\b", re.I)
_COMPARE = re.compile(r"\b(?:between|since|from|compared?|comparison|changed?|changes|snapshots?|as\s+of|versus|vs\.?|then\s+and\s+now|over\s+the\s+period)\b", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def dates_in(text: str, now: datetime | None = None) -> list[datetime]:
    now = now or _now()
    out: list[datetime] = []
    for m in _DATE.finditer(text or ""):
        y_iso, mo_iso, d_iso, mon1, d1, y1, d2, mon2, y2 = m.groups()
        try:
            if y_iso:
                when = datetime(int(y_iso), int(mo_iso), int(d_iso), tzinfo=timezone.utc)
            else:
                month = _MONTHS[(mon1 or mon2).lower()[:3]]
                day = int(d1 or d2)
                year = int(y1 or y2) if (y1 or y2) else now.year
                when = datetime(year, month, day, tzinfo=timezone.utc)
                if not (y1 or y2) and when > now + timedelta(days=1):
                    when = when.replace(year=year - 1)            # "December 3" said in January means last year
        except ValueError:
            continue
        if when not in out:
            out.append(when)
    return out


def dated_ask(request: str, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """(start, end) when the message asks how something changed between
    dates or since a date; end is the later day's end, or now for an open
    range, never later than now."""
    if not _COMPARE.search(request or ""):
        return None
    found = sorted(dates_in(request, now))
    if not found:
        return None
    now = now or _now()
    start = found[0]
    end = (found[-1] + timedelta(days=1) - timedelta(seconds=1)) if len(found) > 1 else now
    return start, min(end, now)


def _fmt(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M UTC")


async def lead(token: dict | None, window: tuple[datetime, datetime]) -> str:
    """The comparison card from the ledger, or the limitation."""
    start, end = window
    days = start.strftime("%B %-d") + (f" and {end.strftime('%B %-d, %Y')}" if end.date() != start.date() else f", {start.year}")
    if not token or not token.get("address"):
        return (f"**No saved snapshot to compare.** No token resolved for the dates asked ({days}), so nothing below compares two "
                "points in time: it is current data only.")
    subject = Subject(kind="token", id=token["address"], chain=token.get("chain") or "solana", symbol=token.get("symbol"))
    label = subject.label()
    older = await holder_snapshots.as_of(subject.key, start + timedelta(days=1) - timedelta(seconds=1))
    newer = await holder_snapshots.as_of(subject.key, end)
    recent = await holder_snapshots.history(subject.key, limit=200)
    earliest = holder_snapshots._dt(recent[0]["taken_at"]) if recent else None
    if older is None or holder_snapshots._dt(older["taken_at"]) < start - timedelta(days=2) or newer is None or newer["id"] == older["id"]:
        have = (f"Orbit's ledger for {label} begins {_fmt(earliest)}" if earliest else f"Orbit has not recorded {label} in its snapshot ledger")
        return (f"**No saved snapshot of {label} for {start.strftime('%B %-d, %Y')}.** {have}, so a comparison between {days} cannot be drawn. "
                "Everything below is current data, not a change over that period.")
    t0, t1 = holder_snapshots._dt(older["taken_at"]), holder_snapshots._dt(newer["taken_at"])
    changes = holder_snapshots.diff(older, newer)
    lines = [f"# Snapshot comparison — {label}",
             f"**Provider**: Orbit snapshot ledger (Mobula data) · **Earlier**: {_fmt(t0)} · **Later**: {_fmt(t1)} · asked: {days}", ""]
    lines += [f"- {c}" for c in changes] if changes else [f"No material change in concentration, labelled cohorts, LP state, price or liquidity between {_fmt(t0)} and {_fmt(t1)}."]
    missing = sorted({m for row in (older, newer) for m in ((row.get("flags") or {}).get("missing") or [])})
    if missing:
        lines += ["", f"Not covered in one or both rows: {', '.join(missing)}."]
    lines += ["", "Two rows of Orbit's own periodic snapshots; each is dated above. Labels are Mobula's classifications, evidence not proof. "
                  "The cards below are current data, not part of the comparison."]
    return "\n".join(lines)

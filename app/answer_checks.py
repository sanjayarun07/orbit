"""Deterministic answer requirements (evals/answers/cases.json).

Routing tests say the right tool was reached; live sweeps say the HTTP
call succeeded. Neither says the answer satisfied the question. Each case
names what the answer must contain, relative to the evidence snapshot it
was captured with: the right contract, holder rows, a concentration
definition, a disclosed gap, a date in the future of the capture, a
definition without an unrelated table. Dates are judged against the
snapshot's capture time, never the wall clock, so a pinned snapshot does
not rot. A model judge (the answer gate's) is a separate, optional layer
for relevance; these checks are the floor.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_MONTH = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:,)?\s+(20\d{2})\b", re.I)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _dates(text: str) -> list[datetime]:
    out = []
    for y, m, d in _DATE.findall(text):
        try:
            out.append(datetime(int(y), int(m), int(d), tzinfo=timezone.utc))
        except ValueError:
            pass
    for mon, d, y in _MONTH.findall(text):
        try:
            out.append(datetime(int(y), _MONTHS[mon[:3].lower()], int(d), tzinfo=timezone.utc))
        except (ValueError, KeyError):
            pass
    return out


def contains_any(snapshot: dict, values: list[str], **_) -> tuple[bool, str]:
    text = (snapshot.get("answer") or "").lower()
    hit = [v for v in values if v.lower() in text]
    return (bool(hit), f"found {hit[0]!r}" if hit else f"none of {values} in the answer")


def contains_all(snapshot: dict, values: list[str], **_) -> tuple[bool, str]:
    text = (snapshot.get("answer") or "").lower()
    missing = [v for v in values if v.lower() not in text]
    return (not missing, "all present" if not missing else f"missing {missing}")


def not_contains(snapshot: dict, values: list[str], **_) -> tuple[bool, str]:
    text = (snapshot.get("answer") or "").lower()
    hit = [v for v in values if v.lower() in text]
    return (not hit, "absent" if not hit else f"found {hit[0]!r}, which must not appear")


def regex(snapshot: dict, pattern: str, **_) -> tuple[bool, str]:
    m = re.search(pattern, snapshot.get("answer") or "", re.I | re.M)
    return (m is not None, f"matched {m.group(0)[:60]!r}" if m else f"no match for /{pattern}/")


def min_rows(snapshot: dict, pattern: str, count: int, **_) -> tuple[bool, str]:
    n = len(re.findall(pattern, snapshot.get("answer") or "", re.I | re.M))
    return (n >= count, f"{n} row(s) matching, need {count}")


def no_table(snapshot: dict, **_) -> tuple[bool, str]:
    rows = [l for l in (snapshot.get("answer") or "").splitlines() if l.strip().startswith("|")]
    return (not rows, "no table" if not rows else f"{len(rows)} table line(s) in an explanation")


def future_date(snapshot: dict, **_) -> tuple[bool, str]:
    """At least one date in the answer lies after the snapshot's capture time."""
    captured = datetime.fromisoformat(snapshot["captured_at"])
    later = [d for d in _dates(snapshot.get("answer") or "") if d > captured]
    return (bool(later), f"{later[0].date()} is after the capture" if later else "no date after the capture time in the answer")


def tool_reached(snapshot: dict, tools: list[str], **_) -> tuple[bool, str]:
    names = set()
    for key, value in (snapshot.get("trajectory") or {}).items():
        if key.startswith("tool_name") and isinstance(value, str):
            names.add(value)
    hit = [t for t in tools if t in names]
    return (bool(hit), f"reached {hit[0]}" if hit else f"none of {tools} in {sorted(names)}")


def envelope(snapshot: dict, tool: str, status: list[str] | None = None, **_) -> tuple[bool, str]:
    envs = [e for e in snapshot.get("envelopes") or [] if e.get("tool") == tool]
    if not envs:
        return (False, f"no envelope from {tool}")
    if status and envs[0].get("status") not in status:
        return (False, f"{tool} envelope is {envs[0].get('status')}, expected one of {status}")
    return (True, f"{tool} envelope {envs[0].get('status')}")


def gaps_disclosed(snapshot: dict, **_) -> tuple[bool, str]:
    """Every partial or unavailable envelope is stated in the answer (the
    validator's coverage check, replayed on the snapshot)."""
    from app.evidence import Evidence, undisclosed_gaps

    envs = [Evidence(**{k: v for k, v in e.items() if k in Evidence.model_fields}) for e in snapshot.get("envelopes") or []]
    hidden = undisclosed_gaps(snapshot.get("answer") or "", envs)
    gaps = [e for e in envs if e.status != "complete"]
    if hidden:
        return (False, "undisclosed: " + "; ".join(e.gap() for e in hidden[:2]))
    return (True, f"{len(gaps)} gap(s), all disclosed" if gaps else "no gaps")


CHECKS: dict[str, Callable[..., tuple[bool, str]]] = {
    "contains_any": contains_any, "contains_all": contains_all, "not_contains": not_contains, "regex": regex, "min_rows": min_rows,
    "no_table": no_table, "future_date": future_date, "tool_reached": tool_reached, "envelope": envelope, "gaps_disclosed": gaps_disclosed,
}


def run_checks(case: dict, snapshot: dict) -> list[dict]:
    out = []
    for spec in case.get("requires", []):
        spec = dict(spec)
        name = spec.pop("check")
        fn = CHECKS[name]
        try:
            ok, detail = fn(snapshot, **spec)
        except Exception as exc:  # noqa: BLE001 - a broken check is a failed check
            ok, detail = False, f"check error: {type(exc).__name__}: {exc}"
        out.append({"check": name, "args": spec, "ok": ok, "detail": detail})
    return out


def verdict(results: list[dict]) -> bool:
    return all(r["ok"] for r in results)

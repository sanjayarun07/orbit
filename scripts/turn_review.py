"""Write a review digest of the turn log: errors, open issues, thumbs-down
and validation warnings for a period, as Markdown plus the raw rows as
JSON, under reports/turns-<date>/ (untracked).

    .venv/bin/python scripts/turn_review.py --days 1
    .venv/bin/python scripts/turn_review.py --days 7 --all      # every turn, not only the problems

Reads the same store the admin page reads (Postgres via DATABASE_URL, or the
in-process fallback when run inside the app). Nothing is modified.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app import turn_log


def _line(t: dict) -> str:
    when = (t.get("created_at") or "")[:16].replace("T", " ")
    who = (t.get("user_id") or t.get("identity_kind") or "?")[:8]
    status = f"{t['status']} {t.get('http_status') or ''}".strip()
    return (f"- **{when}** · {status} · {t.get('latency_ms') or '—'} ms · {who} · {t.get('intent') or '—'} · "
            f"tools: {', '.join(t.get('tools') or []) or '—'}\n  - request: {(t.get('message') or '')[:200]!r}\n"
            + (f"  - error: {t['error'][:300]!r}\n" if t.get("error") else "")
            + (f"  - rating: {t['rating']}{' · ' + t['rating_comment'] if t.get('rating_comment') else ''}\n" if t.get("rating") else "")
            + (f"  - flag: {t.get('flag_note') or 'open'}{' (resolved)' if t.get('resolved_at') else ''}\n" if t.get("flagged") else "")
            + (f"  - gate: {json.dumps(t['gate'])[:200]}\n" if t.get("gate") else "")
            + (f"  - answer: {(t.get('answer') or '')[:300]!r}\n" if t.get("answer") else ""))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=float, default=1.0)
    parser.add_argument("--all", action="store_true", help="include answered, unflagged turns")
    parser.add_argument("--out", default=None, help="directory (default reports/turns-<date>)")
    args = parser.parse_args()

    summary = await turn_log.summary(days=args.days)
    turns = [turn_log.public(t, full=True) for t in await turn_log.list_turns(days=args.days, limit=1000)]
    problems = [t for t in turns if t["status"] == "error" or (t.get("flagged") and not t.get("resolved_at")) or t.get("rating") == "down"
                or (t.get("validation") or {}).get("status") == "warn" or t.get("gate")]
    out = Path(args.out or f"reports/turns-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "turns.json").write_text(json.dumps(turns if args.all else problems, indent=1, default=str))

    lines = [f"# Turn review — last {args.days:g} day(s)", f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", "",
             "| Turns | Errors | Open issues | Thumbs down | Validation warns | Gate rewrites | p50 ms | p95 ms | Users |", "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
             f"| {summary['turns']} | {summary['errors']} | {summary['open_flags']} | {summary['thumbs_down']} | {summary['validation_warnings']} | "
             f"{summary['gate_interventions']} | {summary['latency_ms']['p50'] or '—'} | {summary['latency_ms']['p95'] or '—'} | {summary['users']} |", ""]
    if summary["errors_by_status"]:
        lines += ["Errors by status: " + ", ".join(f"{k}: {v}" for k, v in sorted(summary["errors_by_status"].items())), ""]
    if summary["tools"]:
        lines += ["Tools that ran most: " + ", ".join(f"{t} ({n})" for t, n in summary["tools"][:10]), ""]
    for title, rows in (("Errors", [t for t in problems if t["status"] == "error"]),
                        ("Open issues and thumbs-down", [t for t in problems if (t.get("flagged") and not t.get("resolved_at")) or t.get("rating") == "down"]),
                        ("Validation warnings", [t for t in problems if (t.get("validation") or {}).get("status") == "warn"]),
                        ("Answer-gate rewrites", [t for t in problems if t.get("gate")])):
        lines += [f"## {title} ({len(rows)})", ""] + ([_line(t) for t in rows] or ["- none", ""])
    if args.all:
        lines += [f"## Every turn ({len(turns)})", ""] + [_line(t) for t in turns]
    (out / "review.md").write_text("\n".join(lines))
    print(f"{summary['turns']} turns, {summary['errors']} errors, {summary['open_flags']} open issues, {summary['thumbs_down']} thumbs down -> {out}/review.md")


if __name__ == "__main__":
    asyncio.run(main())

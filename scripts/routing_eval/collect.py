"""Gather candidate prompts for the routing eval, with provenance.

Labels are the expensive part of an eval, not prompts. This collects prompts
from the places that already have some signal, so labeling starts from the
best candidates rather than a blank page:

  --file prompts.txt   your own, one per line, typed the way users type
  --catalog            every tool's `answers` examples in app/tool_catalog.py,
                       already labeled with the tool that should handle them
  --gaps DAYS          the research_gaps log (turns that fell through to web
                       search) from the configured Postgres, last DAYS days

Writes candidates.json next to this script: [{query, source, expected_tools?}].
Then: harness.py --mode disagree --prompts candidates.json  (see there).

  .venv/bin/python scripts/routing_eval/collect.py --catalog --file my_prompts.txt
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

OUT = Path(__file__).with_name("candidates.json")


def from_file(path: str) -> list[dict]:
    lines = [l.strip() for l in Path(path).read_text().splitlines()]
    return [{"query": l, "source": f"file:{Path(path).name}"} for l in lines if l and not l.startswith("#")]


def from_catalog() -> list[dict]:
    from app.tool_catalog import TOOL_SPECS
    out = []
    for name, spec in TOOL_SPECS.items():
        for example in spec.answers:
            out.append({"query": example, "source": f"catalog:{name}", "expected_tools": [name]})
    return out


async def from_gaps(days: int) -> list[dict]:
    from app.db import get_pg_pool
    pool = await get_pg_pool()
    if pool is None:
        print("research_gaps: no Postgres configured (DATABASE_URL); nothing collected")
        return []
    rows = await pool.fetch(
        "SELECT request, topic, tools FROM research_gaps WHERE created_at >= NOW() - ($1 || ' days')::interval ORDER BY created_at DESC LIMIT 5000", str(days))
    return [{"query": r["request"], "source": f"gaps:{r['topic']}", "fell_through_to": list(r["tools"] or [])} for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="append", default=[])
    ap.add_argument("--catalog", action="store_true")
    ap.add_argument("--gaps", type=int, default=0, help="days of research_gaps to pull (needs Postgres)")
    args = ap.parse_args()
    candidates: list[dict] = []
    for f in args.file:
        candidates += from_file(f)
    if args.catalog:
        candidates += from_catalog()
    if args.gaps:
        candidates += asyncio.run(from_gaps(args.gaps))
    # Dedupe on the normalized text; keep the first provenance seen.
    seen, unique = set(), []
    for c in candidates:
        key = " ".join(c["query"].lower().split())
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    # Never re-collect what is already labeled.
    labeled = {" ".join(c["query"].lower().split()) for c in json.loads(Path(__file__).with_name("cases.json").read_text())}
    unique = [c for c in unique if " ".join(c["query"].lower().split()) not in labeled]
    OUT.write_text(json.dumps(unique, indent=2))
    by_source = {}
    for c in unique:
        by_source[c["source"].split(":")[0]] = by_source.get(c["source"].split(":")[0], 0) + 1
    print(f"{len(unique)} new candidate prompts -> {OUT}  {by_source}")


if __name__ == "__main__":
    main()

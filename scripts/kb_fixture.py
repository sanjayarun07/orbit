"""Refresh tests/fixtures/kb_entities.json from the configured knowledge base.

The routing gate (tests/test_routing_gate.py) resolves names against the
knowledge base's entities; a run without a database (CI) builds that
resolver from this fixture instead. Re-run after ingesting new protocols so
the gate judges the vocabulary the app actually has.

    python scripts/kb_fixture.py
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "kb_entities.json"


async def main() -> int:
    from app.knowledge.tool import get_store

    entities = await (await get_store()).list_entities()
    # The resolver reads names, aliases, types, addresses, and from the
    # metadata only the external ids it indexes, the TVL it ranks namesakes
    # by, and an incident's date (app/knowledge/entities.py). The rest --
    # descriptions, links -- is dropped so the fixture stays small.
    keep = ("coingecko_id", "defillama_slug", "tvl_usd", "date")
    rows = [{**dataclasses.asdict(e), "metadata": {k: e.metadata[k] for k in keep if e.metadata.get(k)}} for e in entities]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(rows, default=str, indent=0))
    print(f"{len(rows)} entities -> {OUT.relative_to(OUT.parents[2])}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Standalone knowledge ingestion process.

Crawling, chunking and embedding are CPU- and network-heavy; running them
inside the API process starves the event loop (chat latency climbs to tens
of seconds while a large docs site is indexed). Run this in its own process
instead. It shares the database with the API, which refreshes its resolver
snapshot every two minutes, so new documents become searchable on their own.

    .venv/bin/python scripts/kb_ingest.py                 # every due (protocol, source) pair, 3 at a time
    .venv/bin/python scripts/kb_ingest.py --parallel 4    # more concurrency
    .venv/bin/python scripts/kb_ingest.py --slug aave-v3  # one protocol, regardless of refresh timers
    .venv/bin/python scripts/kb_ingest.py --loop          # keep running (worker mode)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge import ingest as kb_ingest  # noqa: E402
from app.settings import settings  # noqa: E402


def _summary(results) -> str:
    seen = sum(r.documents_seen for r in results)
    changed = sum(r.documents_changed for r in results)
    chunks = sum(r.chunks_written for r in results)
    errors = sum(len(r.errors) for r in results)
    return f"pairs={len(results)} documents_seen={seen} changed={changed} chunks={chunks} errors={errors}"


async def main(args: argparse.Namespace) -> int:
    from app.knowledge.store import get_store

    store = await get_store()
    if store.__class__.__name__ != "PostgresStore":
        print("DATABASE_URL is not set: the standalone ingester needs Postgres (an in-memory store would vanish with this process).", file=sys.stderr)
        return 2
    if args.slug:
        semaphore = asyncio.Semaphore(max(1, args.parallel))

        async def one(slug: str) -> list:
            async with semaphore:
                results = await kb_ingest.run_protocol(f"protocol:{slug}")
                print(f"{slug}: {_summary(results)}")
                for r in results:
                    print(f"  {r.source}: seen={r.documents_seen} changed={r.documents_changed} chunks={r.chunks_written} errors={r.errors[:2]}")
                return results

        batches = await asyncio.gather(*(one(s) for s in args.slug))
        print("total:", _summary([r for batch in batches for r in batch]))
        return 0
    if args.derive:
        from app.knowledge.derive import derive_all

        print("derived:", await derive_all(store))
        return 0
    count = await kb_ingest.ensure_registry(store, limit=args.limit)
    print(f"registry: {count} protocols" + ("" if count else " -- nothing to ingest (DefiLlama unreachable?)"))
    while True:
        results = await kb_ingest.run_all(parallel=args.parallel, limit=args.limit)
        print(_summary(results))
        if not args.loop:
            return 0
        await asyncio.sleep(max(30, settings.knowledge_ingest_interval_seconds))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--parallel", type=int, default=3, help="protocols ingested concurrently (default 3)")
    parser.add_argument("--limit", type=int, default=None, help="only the top N protocols by TVL (default: KNOWLEDGE_REGISTRY_LIMIT)")
    parser.add_argument("--slug", nargs="+", help="ingest these protocols now, regardless of refresh timers (DefiLlama slugs, e.g. aave-v3 lido)")
    parser.add_argument("--loop", action="store_true", help="keep running, sleeping KNOWLEDGE_INGEST_INTERVAL_SECONDS between passes")
    parser.add_argument("--derive", action="store_true", help="only recompute derived edges (competitors, corroborated integrations)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parsed = parser.parse_args()
    logging.basicConfig(level=logging.INFO if parsed.verbose else logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("app.knowledge").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(asyncio.run(main(parsed)))

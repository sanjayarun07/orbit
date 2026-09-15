"""One-off, real, end-to-end invocation of the wash-trading detector --
NOT a test, not run automatically, and NOT free (spends real Dune credits
and Bitquery quota, and needs a reachable ClickHouse instance). Run this
manually once Phase 1 is code-complete and Phase 0's schema verification
(scripts/verify_dune_schema.py) has confirmed the real dex_solana.trades
column names, to prove the whole chain works against live data before
calling Phase 1 done.

Usage:
    export DUNE_API_KEY=... BITQUERY_API_KEY=... CLICKHOUSE_HOST=...
    python scripts/run_wash_trading_live.py \\
        --token-mint <mint address> \\
        --window-start 2026-09-12T00:00:00 --window-end 2026-09-13T00:00:00 \\
        --top-n 100
"""

import argparse
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from app.settings import settings  # noqa: E402
from app.wash_trading import pipeline  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-mint", required=True)
    parser.add_argument("--pool-filter", default=None)
    parser.add_argument("--window-start", required=True, help="ISO timestamp, e.g. 2026-09-12T00:00:00")
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--top-n", type=int, default=100)
    parser.add_argument("--label", default=None)
    args = parser.parse_args()

    missing = [
        name for name, value in (
            ("DUNE_API_KEY", settings.dune_api_key),
            ("BITQUERY_API_KEY", settings.bitquery_api_key),
            ("CLICKHOUSE_HOST", settings.clickhouse_host),
        ) if not value
    ]
    if missing:
        print(f"Missing required config: {', '.join(missing)} -- export these before running.", file=sys.stderr)
        sys.exit(1)

    window_start = datetime.fromisoformat(args.window_start).replace(tzinfo=timezone.utc)
    window_end = datetime.fromisoformat(args.window_end).replace(tzinfo=timezone.utc)

    print(f"Running wash-trading detection for {args.token_mint} [{window_start} - {window_end}], top {args.top_n}...")
    run = pipeline.run_detection(
        args.token_mint, args.pool_filter, window_start, window_end, args.top_n, args.label,
    )
    print(f"\nrun_id: {run.run_id}")
    print(json.dumps({
        "concentration": vars(run.concentration),
        "round_trip_count": run.round_trip_count,
        "fan_out_clusters": run.fan_out_clusters,
    }, indent=2, default=str))
    print(
        f"\nFetch the full persisted result later with:\n"
        f"  GET /admin/wash-trading/runs/{run.run_id}\n"
        f"  (Authorization: Bearer $ADMIN_API_KEY)"
    )


if __name__ == "__main__":
    main()

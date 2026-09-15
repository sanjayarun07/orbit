"""Phase 0 gating script for the wash-trading detector -- NOT a test, not
run automatically. Run manually, once, against a real DUNE_API_KEY before
trusting app/dune_tools.py's build_top_traders_sql / build_wallet_trades_sql.

What this answers (see app/dune_tools.py's module docstring for why these
matter):
  1. The real column names on dex_solana.trades (this script's SELECT *
     assumes nothing about columns -- it just dumps what comes back).
  2. Whether tx_id is shared across the legs of one routed swap, by
     querying one known routed-swap transaction signature and inspecting
     how many rows come back and how they differ.
  3. Whether amount_usd on a leg is the leg's own notional or the full
     route's notional.

Usage:
    export DUNE_API_KEY=...
    python scripts/verify_dune_schema.py
    python scripts/verify_dune_schema.py --tx-signature <a known routed-swap signature>

After running: update app/dune_tools.py's module docstring and the two
build_*_sql functions' docstrings with the confirmed schema, and remove the
"UNVERIFIED" language once confident.
"""

import argparse
import json
import sys

sys.path.insert(0, ".")

from app import dune_tools  # noqa: E402
from app.settings import settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tx-signature", help="A known routed-swap transaction signature to inspect leg count/shape")
    parser.add_argument("--token-mint", default=None, help="A token mint to sample recent trades for")
    args = parser.parse_args()

    if not settings.dune_api_key:
        print("DUNE_API_KEY is not set -- export it before running this script.", file=sys.stderr)
        sys.exit(1)

    print("=== dex_solana.trades: sample rows (SELECT * LIMIT 5) ===")
    rows = dune_tools.run_sql("SELECT * FROM dex_solana.trades LIMIT 5")
    print(json.dumps(rows, indent=2, default=str))
    if rows:
        print("\nColumns observed:", sorted(rows[0].keys()))

    if args.tx_signature:
        print(f"\n=== Rows for tx_id = '{args.tx_signature}' (leg-dedup check) ===")
        leg_rows = dune_tools.run_sql(
            f"SELECT * FROM dex_solana.trades WHERE tx_id = '{args.tx_signature}'"
        )
        print(json.dumps(leg_rows, indent=2, default=str))
        print(f"\n{len(leg_rows)} row(s) returned for this transaction.")
        if len(leg_rows) > 1:
            amounts = [row.get("amount_usd") for row in leg_rows]
            print(f"amount_usd per leg: {amounts}")
            print(
                "If these sum to roughly the route's total USD value, amount_usd is "
                "per-leg notional (SUM(amount_usd) in build_top_traders_sql is correct "
                "for total volume, but COUNT(DISTINCT tx_id) already correctly counts "
                "this as one transaction). If each leg's amount_usd already equals the "
                "full route value, SUM(amount_usd) double-counts and must change."
            )

    if args.token_mint:
        print(f"\n=== Recent trades for token_mint = '{args.token_mint}' (5) ===")
        token_rows = dune_tools.run_sql(
            "SELECT * FROM dex_solana.trades WHERE token_bought_mint_address = "
            f"'{args.token_mint}' OR token_sold_mint_address = '{args.token_mint}' "
            "ORDER BY block_time DESC LIMIT 5"
        )
        print(json.dumps(token_rows, indent=2, default=str))


if __name__ == "__main__":
    main()

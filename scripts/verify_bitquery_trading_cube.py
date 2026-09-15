"""Phase 2 prep probe -- NOT a test, not run automatically. Checks whether
our Bitquery plan has access to the MEV-filtered "Trading cube"
(Trading.Trades), which the wash-trading detector's methodology calls out
as useful for excluding MEV bot activity from the wash-volume estimate.

Earlier this session, live-testing found our Bitquery plan is
dataset: realtime only -- dataset: combined and dataset: archive both 403
with "access restricted... your plan only allows realtime". The Trading
cube's entitlement is a separate, unverified question -- this script's job
is just to get a real answer (200 with data, or 403/other error) so
app/wash_trading/pipeline.py's Phase 2 work can either build on it or stay
deferred, rather than guessing.

This also probes the Solana Transfers query shape used by
pipeline._fetch_funding_source (Transfer.Sender.Address / .Amount /
.AmountInUSD / .Currency.Symbol) against a real wallet, since that schema
is separately unverified (see pipeline.py's module comment on
_FUNDING_QUERY).

Usage:
    export BITQUERY_API_KEY=...
    python scripts/verify_bitquery_trading_cube.py --wallet <a real Solana wallet address>
"""

import argparse
import json
import sys

sys.path.insert(0, ".")

import httpx  # noqa: E402

from app.settings import settings  # noqa: E402
from app.wash_trading.pipeline import _FUNDING_QUERY  # noqa: E402


def _post(query: str, variables: dict) -> dict:
    with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
        response = client.post(
            settings.bitquery_graphql_url,
            headers={"Authorization": f"Bearer {settings.bitquery_api_key}"},
            json={"query": query, "variables": variables},
        )
        return {"status_code": response.status_code, "body": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wallet", required=True, help="A real Solana wallet address to probe funding-trace for")
    args = parser.parse_args()

    if not settings.bitquery_api_key:
        print("BITQUERY_API_KEY is not set -- export it before running this script.", file=sys.stderr)
        sys.exit(1)

    print("=== Probing Trading cube (Trading.Trades) entitlement ===")
    trading_cube_query = """query TradingCubeProbe($address: String!) {
      Trading {
        Trades(
          limit: {count: 3}
          where: {Trader: {Address: {is: $address}}}
        ) {
          Trade { Buy { Amount } Sell { Amount } }
          Block { Time }
        }
      }
    }"""
    result = _post(trading_cube_query, {"address": args.wallet})
    print(json.dumps(result, indent=2))
    if result["status_code"] == 200 and not (isinstance(result["body"], dict) and result["body"].get("errors")):
        print("\n=> Trading cube appears ACCESSIBLE on this plan. Update pipeline.py's Phase 2 notes.")
    else:
        print("\n=> Trading cube is NOT accessible (or the query shape is wrong) -- stays deferred to Phase 2.")

    print("\n=== Probing Solana Transfers funding-trace query shape ===")
    result = _post(_FUNDING_QUERY, {"address": args.wallet})
    print(json.dumps(result, indent=2))
    if result["status_code"] == 200 and not (isinstance(result["body"], dict) and result["body"].get("errors")):
        print("\n=> Funding-trace query shape is CORRECT. Remove the UNVERIFIED note in pipeline.py.")
    else:
        print("\n=> Funding-trace query needs adjustment -- see the error above and fix pipeline.py's _FUNDING_QUERY.")


if __name__ == "__main__":
    main()

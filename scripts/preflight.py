"""Preflight for a production env file: what the startup audit will say, and
what the deployment will and will not do, BEFORE `docker compose up`.

    python scripts/preflight.py .env            # on the host, from the checkout
    docker compose ... run --rm api python scripts/preflight.py /dev/null   # inside the image, env from compose

Reads the file the way the app does (pydantic-settings), runs the same
audit `app/main.py` runs at startup, and prints:

- every FATAL (the app would refuse to boot) and WARNING (it would boot and
  say so in /readyz);
- the deployment's mode and what it implies (research / execution / custodial);
- which optional providers are configured and which stay out of routing;
- the settings that matter for a multi-worker container.

Never prints a value: names only. Exit status 1 on any fatal finding, so it
can gate a deploy script.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import deployment  # noqa: E402
from app.settings import Settings  # noqa: E402

PROVIDERS = [
    ("OPENAI_API_KEY", "openai_api_key", "answers, classification, web search (required)"),
    ("MOBULA_API_KEY", "mobula_api_key", "wallets, portfolios, meme forensics, holder snapshots"),
    ("PERPLEXITY_API_KEY", "perplexity_api_key", "web research and the answer gate's fallback"),
    ("COINGECKO_API_KEY", "coingecko_api_key", "listed-asset facts, market overview (keyless tier otherwise)"),
    ("BIRDEYE_API_KEY", "birdeye_api_key", "Solana market data"),
    ("HELIUS_API_KEY", "helius_api_key", "Solana history and enhanced RPC"),
    ("GOLDRUSH_API_KEY", "goldrush_api_key", "EVM wallet balances and history"),
    ("BITQUERY_API_KEY", "bitquery_api_key", "EVM token resolution by real volume"),
    ("GOPLUS_ACCESS_TOKEN", "goplus_access_token", "EVM token security"),
    ("NANSEN_API_KEY", "nansen_api_key", "smart-money labels"),
    ("ROOTDATA_API_KEY", "rootdata_api_key", "projects, investors, people"),
    ("JUPITER_API_KEY", "jupiter_api_key", "Solana quotes and token registry"),
    ("RELAY_API_KEY", "relay_api_key", "cross-chain swaps"),
    ("TWITTERAPI_IO_KEY", "twitterapi_io_key", "X sentiment analyst (tweets)"),
    ("TYPESAFE_API_KEY", "typesafe_api_key", "Jev: the sentiment judge and the optional routing backend"),
    ("REDDIT_CLIENT_ID", "reddit_client_id", "Reddit crowd (needs the secret too)"),
    ("LUNARCRUSH_API_KEY", "lunarcrush_api_key", "social metrics"),
    ("DUNE_API_KEY", "dune_api_key", "Dune queries"),
    ("RESEND_API_KEY", "resend_api_key", "magic-link sign-in and task email"),
    ("PRIVY_APP_ID", "privy_app_id", "Privy embedded wallets (public id)"),
    ("REOWN_PROJECT_ID", "reown_project_id", "WalletConnect / Reown (public id)"),
    ("STRIPE_SECRET_KEY", "stripe_secret_key", "payments (ignored while CLOSED_BETA=true)"),
]


def load(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value[:1] in "\"'" and value[-1:] == value[:1]:
            value = value[1:-1]
        elif value.startswith("#"):
            value = ""                      # `KEY=   # comment` is unset
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if value.startswith("<") and value.endswith(">"):
            value = ""                      # a template placeholder is "unset"
        values[key] = value
    return values


def _from_file_only(values: dict[str, str]) -> Settings:
    """The settings the FILE describes. pydantic-settings also reads the
    process environment, which would let a variable in the operator's shell
    (or a test's) pass for a value in the file; every setting name is
    removed from the environment while the object is built."""
    import os

    names = {name.upper() for name in Settings.model_fields}
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.upper() in names}
    try:
        return Settings(_env_file=None, **{k.lower(): v for k, v in values.items() if v != ""})
    finally:
        os.environ.update(saved)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0]); print("usage: python scripts/preflight.py <env-file>"); return 2
    values = load(argv[1])
    # The compose file wires these from POSTGRES_PASSWORD; stand in for it so
    # the audit judges the rest of the file rather than repeating that.
    if values.get("POSTGRES_PASSWORD") and not values.get("DATABASE_URL"):
        values["DATABASE_URL"] = "postgresql://orbit:***@postgres:5432/orbit"
        values["REDIS_URL"] = values.get("REDIS_URL") or "redis://redis:6379/0"
    config = _from_file_only(values)
    problems = deployment.audit(config)
    fatal = [p for p in problems if p.severity == deployment.FATAL]
    print(f"env file: {argv[1]}   environment={config.environment}   production={'yes' if deployment._is_production(config) else 'no'}")
    print()
    print("startup audit:", "REFUSES TO BOOT" if fatal else "boots", f"({len(fatal)} fatal, {len(problems) - len(fatal)} warnings)")
    for p in problems:
        print(f"  [{p.severity}] {p.code} ({p.setting}): {p.message}")
    print()
    mode = deployment.deployment_mode(config)
    print(f"deployment: mode={mode} execution={'on' if deployment.execution_enabled(config) else 'off'} "
          f"custodial_signing={'on' if deployment.custodial_signing_enabled(config) else 'off'} closed_beta={'yes' if config.closed_beta else 'no'}")
    if values.get("SOLANA_PRIVATE_KEY"):
        print("  ! SOLANA_PRIVATE_KEY is set: the server would hold a signing key. A wallet-only deployment must not set it.")
    if config.max_trade_usd:
        print(f"  per-trade cap: ${config.max_trade_usd:g}  slippage cap: {config.max_slippage_bps} bps  price-impact cap: {config.max_price_impact_pct}%")
    print()
    print("providers:")
    for env_name, attr, what in PROVIDERS:
        present = bool(getattr(config, attr, None))
        print(f"  {'set    ' if present else 'unset  '} {env_name:<24} {what}")
    print()
    workers = int(values.get("UVICORN_WORKERS") or config.uvicorn_workers or 1)
    print(f"workers: UVICORN_WORKERS={workers}  mobula_shared_limit={config.mobula_shared_limit} (the Mobula minute allowance is shared through Redis)")
    print(f"         holder_snapshots={'on' if config.holder_snapshots_enabled else 'off'} (one leader per deployment through a Redis lease)")
    print(f"         chat_requests_per_minute={config.chat_requests_per_minute}  max_paid_data_cost_usd_per_turn={config.max_paid_data_cost_usd_per_turn}")
    print(f"         polymarket={'on' if config.polymarket_enabled else 'off'}  tradingview={'on' if config.tradingview_enabled else 'off'}  knowledge_ingest={'on' if config.knowledge_ingest_enabled else 'off (bulk-load by hand)'}")
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

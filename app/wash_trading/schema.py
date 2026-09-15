"""ClickHouse DDL and connection helper for the wash-trading detector.

Deliberately separate from app/provider_analytics.py's fire-and-forget
telemetry queue: that module owns one narrow write-only table
(orbit_provider_events) written from a background thread that drops events
under backpressure. This feature needs reads (re-running/inspecting a past
detection run) and a completely different schema, and is an
operator-triggered batch job, not a per-request event sink -- so it gets its
own client rather than sharing that queue/thread model.

Reuses the same settings.clickhouse_* fields and the same
clickhouse_connect.get_client(...) call shape as provider_analytics.py for
consistency, and the same inline "CREATE TABLE IF NOT EXISTS" convention
(no separate migration framework/tool exists in this codebase for ClickHouse
-- Postgres has no migration tool here either, see app/db.py).
"""

from __future__ import annotations

from typing import Any

from app.settings import settings


_TABLES = (
    """CREATE TABLE IF NOT EXISTS wash_trading_runs (
        run_id String, token_mint String, pool_filter Nullable(String),
        label Nullable(String), window_start DateTime64(3, 'UTC'),
        window_end DateTime64(3, 'UTC'), top_n UInt32,
        created_at DateTime64(3, 'UTC') DEFAULT now64(3),
        status LowCardinality(String)
    ) ENGINE = MergeTree ORDER BY (run_id, created_at)""",
    """CREATE TABLE IF NOT EXISTS wash_trading_trades (
        run_id String, tx_id String, trader_id String, token_mint String,
        side LowCardinality(String), amount_usd Float64,
        project LowCardinality(String), program_id String,
        block_time DateTime64(3, 'UTC'), is_round_trip_leg Bool DEFAULT false,
        round_trip_id Nullable(String)
    ) ENGINE = MergeTree ORDER BY (run_id, trader_id, block_time)""",
    """CREATE TABLE IF NOT EXISTS wash_trading_wallet_stats (
        run_id String, trader_id String, total_volume_usd Float64,
        trade_count UInt32, median_trade_usd Float64, round_trip_count UInt32,
        volume_share_pct Float64
    ) ENGINE = MergeTree ORDER BY (run_id, total_volume_usd)""",
    """CREATE TABLE IF NOT EXISTS wash_trading_funding_edges (
        run_id String, wallet String, funding_wallet String,
        funding_token LowCardinality(String), funding_amount_usd Float64,
        first_tx_signature String, first_tx_time DateTime64(3, 'UTC'),
        source LowCardinality(String)
    ) ENGINE = MergeTree ORDER BY (run_id, funding_wallet, wallet)""",
    # Phase 1 populates only simple shared-funding-wallet fan-out groups
    # here (reason='shared_funding_fanout'). Phase 2 adds score/edge_reasons
    # columns and repopulates with the full weighted-scorer algorithm.
    """CREATE TABLE IF NOT EXISTS wash_trading_clusters (
        run_id String, cluster_id String, wallet String, cluster_size UInt32,
        cluster_volume_usd Float64, shared_funding_wallet Nullable(String),
        reason LowCardinality(String)
    ) ENGINE = MergeTree ORDER BY (run_id, cluster_id, wallet)""",
)


def get_client() -> Any:
    """Returns a synchronous clickhouse_connect client for direct use.

    Unlike provider_analytics.py's background-thread worker, this is called
    directly from pipeline.run_detection (itself dispatched via
    asyncio.to_thread at the admin-route layer), so no queue/thread of its
    own is needed here.
    """
    if not settings.clickhouse_host:
        raise RuntimeError(
            "CLICKHOUSE_HOST is not configured -- the wash-trading detector needs a "
            "reachable ClickHouse instance to persist runs."
        )
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host, port=settings.clickhouse_port,
        username=settings.clickhouse_username, password=settings.clickhouse_password or "",
        database=settings.clickhouse_database, secure=settings.clickhouse_secure,
    )


def ensure_schema(client: Any) -> None:
    for ddl in _TABLES:
        client.command(ddl)

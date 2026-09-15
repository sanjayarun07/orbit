from datetime import datetime, timedelta, timezone

from app.settings import settings
from app.wash_trading import pipeline


def _dt(offset_seconds: float = 0) -> datetime:
    return datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)


def test_compute_concentration_stats_ranks_and_shares_volume():
    traders = [
        pipeline.TraderRow("A", transactions=10, volume_usd=700.0, avg_trade_usd=70.0),
        pipeline.TraderRow("B", transactions=5, volume_usd=200.0, avg_trade_usd=40.0),
        pipeline.TraderRow("C", transactions=3, volume_usd=100.0, avg_trade_usd=33.3),
    ]
    stats = pipeline.compute_concentration_stats(traders)
    assert stats.total_volume_usd == 1000.0
    assert stats.unique_traders == 3
    assert stats.top1_share_pct == 70.0
    assert stats.top10_share_pct == 100.0  # only 3 traders exist -- top10 saturates at 100%
    assert stats.median_volume_usd == 200.0


def test_compute_concentration_stats_handles_no_traders():
    stats = pipeline.compute_concentration_stats([])
    assert stats.total_volume_usd == 0.0
    assert stats.unique_traders == 0


def test_detect_round_trips_flags_rapid_near_equal_buy_sell():
    """A buy then a sell 5 seconds later at near-identical USD size is
    exactly the 'repeated short-duration round trip' pattern the
    methodology calls out as the real wash-trading signal, distinct from
    gross volume alone.
    """
    trades = [
        pipeline.TradeRow(_dt(0), "A", "MINT", "buy", 100_000.0, "raydium", "tx1"),
        pipeline.TradeRow(_dt(12), "A", "MINT", "sell", 99_400.0, "raydium", "tx2"),
    ]
    round_trips = pipeline.detect_round_trips(trades, max_gap_seconds=30, size_tolerance_pct=0.05)
    assert len(round_trips) == 1
    trip = round_trips[0]
    assert trip.trader_id == "A"
    assert trip.buy_tx_id == "tx1"
    assert trip.sell_tx_id == "tx2"
    assert trip.gap_seconds == 12


def test_detect_round_trips_ignores_pairs_outside_the_time_gap():
    trades = [
        pipeline.TradeRow(_dt(0), "A", "MINT", "buy", 100_000.0, "raydium", "tx1"),
        pipeline.TradeRow(_dt(600), "A", "MINT", "sell", 99_400.0, "raydium", "tx2"),  # 10 minutes later
    ]
    assert pipeline.detect_round_trips(trades, max_gap_seconds=30, size_tolerance_pct=0.05) == []


def test_detect_round_trips_ignores_pairs_outside_the_size_tolerance():
    trades = [
        pipeline.TradeRow(_dt(0), "A", "MINT", "buy", 100_000.0, "raydium", "tx1"),
        pipeline.TradeRow(_dt(5), "A", "MINT", "sell", 50_000.0, "raydium", "tx2"),  # 50% smaller -- a real trade, not a round trip
    ]
    assert pipeline.detect_round_trips(trades, max_gap_seconds=30, size_tolerance_pct=0.05) == []


def test_detect_round_trips_ignores_legs_of_the_same_transaction():
    """A single routed swap can post a buy leg and a sell leg against the
    same mint when that mint is only a pass-through hop (verified live: one
    real wallet had 99.3% of its naively-detected 'round trips' turn out to
    be exactly this) -- one atomic transaction is not two separate trades,
    so it must never be counted as a round trip even if the legs are
    same-side-opposite and same-size.
    """
    trades = [
        pipeline.TradeRow(_dt(0), "A", "MINT", "buy", 100.0, "raydium", "tx1"),
        pipeline.TradeRow(_dt(0), "A", "MINT", "sell", 100.0, "raydium", "tx1"),  # same tx_id
    ]
    assert pipeline.detect_round_trips(trades, max_gap_seconds=30, size_tolerance_pct=0.05) == []


def test_detect_round_trips_still_flags_a_genuine_cross_transaction_pair():
    """Guards against the same-tx_id fix above becoming an overcorrection
    that also silently drops real round trips across two distinct
    transactions.
    """
    trades = [
        pipeline.TradeRow(_dt(0), "A", "MINT", "buy", 100_000.0, "raydium", "tx1"),
        pipeline.TradeRow(_dt(5), "A", "MINT", "sell", 99_500.0, "raydium", "tx2"),  # different tx_id
    ]
    round_trips = pipeline.detect_round_trips(trades, max_gap_seconds=30, size_tolerance_pct=0.05)
    assert len(round_trips) == 1


def test_detect_round_trips_does_not_flag_two_same_side_trades():
    trades = [
        pipeline.TradeRow(_dt(0), "A", "MINT", "buy", 100_000.0, "raydium", "tx1"),
        pipeline.TradeRow(_dt(5), "A", "MINT", "buy", 100_000.0, "raydium", "tx2"),
    ]
    assert pipeline.detect_round_trips(trades, max_gap_seconds=30, size_tolerance_pct=0.05) == []


def test_detect_round_trips_does_not_reuse_a_trade_in_two_round_trips():
    trades = [
        pipeline.TradeRow(_dt(0), "A", "MINT", "buy", 100_000.0, "raydium", "tx1"),
        pipeline.TradeRow(_dt(5), "A", "MINT", "sell", 99_500.0, "raydium", "tx2"),
        pipeline.TradeRow(_dt(10), "A", "MINT", "sell", 99_500.0, "raydium", "tx3"),
    ]
    round_trips = pipeline.detect_round_trips(trades, max_gap_seconds=30, size_tolerance_pct=0.05)
    assert len(round_trips) == 1
    used_tx = {round_trips[0].buy_tx_id, round_trips[0].sell_tx_id}
    assert used_tx == {"tx1", "tx2"}  # tx3 is left unpaired, not double-counted


def test_detect_round_trips_uses_settings_defaults_when_not_overridden(monkeypatch):
    monkeypatch.setattr(settings, "wash_trading_round_trip_max_gap_seconds", 3.0)
    trades = [
        pipeline.TradeRow(_dt(0), "A", "MINT", "buy", 100_000.0, "raydium", "tx1"),
        pipeline.TradeRow(_dt(5), "A", "MINT", "sell", 99_500.0, "raydium", "tx2"),
    ]
    assert pipeline.detect_round_trips(trades) == []  # 5s gap exceeds the 3s setting default


def test_compute_fan_out_clusters_groups_wallets_sharing_a_funder():
    funding = {
        "A": pipeline.FundingInfo("A", "FunderX", "SOL", 500.0, "sig1", _dt(0), "bitquery_realtime"),
        "B": pipeline.FundingInfo("B", "FunderX", "SOL", 500.0, "sig2", _dt(1), "bitquery_realtime"),
        "C": pipeline.FundingInfo("C", "FunderY", "SOL", 500.0, "sig3", _dt(2), "bitquery_realtime"),
    }
    traders = [
        pipeline.TraderRow("A", 10, 18_000_000.0, 1_800_000.0),
        pipeline.TraderRow("B", 8, 12_000_000.0, 1_500_000.0),
        pipeline.TraderRow("C", 3, 9_000_000.0, 3_000_000.0),
    ]
    clusters = pipeline._compute_fan_out_clusters(funding, traders)
    assert len(clusters) == 1  # C's funder (FunderY) only has one wallet -- not a fan-out
    cluster = clusters[0]
    assert cluster["funding_wallet"] == "FunderX"
    assert set(cluster["wallets"]) == {"A", "B"}
    assert cluster["cluster_volume_usd"] == 30_000_000.0


def test_compute_fan_out_clusters_ignores_wallets_with_no_funding_found():
    funding = {"A": pipeline.FundingInfo("A", None, None, None, None, None, "bitquery_realtime")}
    assert pipeline._compute_fan_out_clusters(funding, []) == []


class _Response:
    def __init__(self, payload, status_ok=True):
        self.payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            import httpx
            raise httpx.HTTPStatusError("429", request=None, response=self)

    def json(self):
        return self.payload


def test_fetch_funding_source_finds_first_meaningful_transfer(monkeypatch):
    monkeypatch.setattr(settings, "bitquery_api_key", "bitquery-key")
    captured = {}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return _Response({"data": {"Solana": {"Transfers": [
                {"Block": {"Time": "2026-09-01T00:00:00Z"},
                 "Transfer": {"Sender": {"Address": "Dust"}, "Amount": "1", "AmountInUSD": "0.5",
                              "Currency": {"Symbol": "SOL"}},
                 "Transaction": {"Signature": "sig_dust"}},
                {"Block": {"Time": "2026-09-01T00:05:00Z"},
                 "Transfer": {"Sender": {"Address": "FunderX"}, "Amount": "100", "AmountInUSD": "500.0",
                              "Currency": {"Symbol": "SOL"}},
                 "Transaction": {"Signature": "sig_real"}},
            ]}}})

    monkeypatch.setattr(pipeline.httpx, "Client", Client)
    info = pipeline._fetch_funding_source("WalletA")
    assert info.funding_wallet == "FunderX"  # the $0.50 dust transfer is skipped
    assert info.funding_amount_usd == 500.0
    assert info.first_tx_signature == "sig_real"
    assert captured["json"]["variables"] == {"address": "WalletA"}


def test_fetch_funding_source_returns_none_when_no_transfers_found(monkeypatch):
    monkeypatch.setattr(settings, "bitquery_api_key", "bitquery-key")

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **_kwargs):
            return _Response({"data": {"Solana": {"Transfers": []}}})

    monkeypatch.setattr(pipeline.httpx, "Client", Client)
    info = pipeline._fetch_funding_source("WalletA")
    assert info.funding_wallet is None


def test_run_detection_orchestrates_and_persists_every_stage(monkeypatch):
    """End-to-end pipeline test with every external call mocked: Bitquery
    (top traders, wallet trades, funding) and ClickHouse. Verifies
    run_detection wires fetch -> detect -> trace -> cluster -> persist in
    the right order without touching a real network or database.

    Extraction is Bitquery-backed, not Dune (see pipeline.py's module note:
    Dune is blocked on this account's billing and its dune_fetch_* twins are
    kept unused for when that's resolved).
    """
    monkeypatch.setattr(settings, "bitquery_api_key", "bitquery-key")

    def fake_top_traders(token_mints, window_start, window_end, top_n):
        assert token_mints == ["MINT"]
        return [
            pipeline.TraderRow("A", transactions=5, volume_usd=18_000_000.0, avg_trade_usd=3_600_000.0),
            pipeline.TraderRow("B", transactions=4, volume_usd=12_000_000.0, avg_trade_usd=3_000_000.0),
        ]

    monkeypatch.setattr(pipeline, "_bitquery_top_traders", fake_top_traders)

    def fake_wallet_trades(wallet, token_mint, window_start, window_end):
        if wallet != "A":
            return []
        return [
            pipeline.TradeRow(_dt(1), "A", "MINT", "buy", 100_000.0, "bitquery_realtime", "tx1"),
            pipeline.TradeRow(_dt(13), "A", "MINT", "sell", 99_400.0, "bitquery_realtime", "tx2"),
        ]

    monkeypatch.setattr(pipeline, "_bitquery_wallet_trades_for_token", fake_wallet_trades)

    def fake_fetch_funding(wallet):
        funder = "FunderX"  # both A and B share a funder -> should produce a fan-out cluster
        return pipeline.FundingInfo(wallet, funder, "SOL", 500.0, f"sig_{wallet}", _dt(0), "bitquery_realtime")

    monkeypatch.setattr(pipeline, "_fetch_funding_source", fake_fetch_funding)

    inserted = []
    rows_by_table = {}

    class FakeClient:
        def command(self, _ddl):
            return None

        def insert(self, table, rows, column_names):
            inserted.append((table, len(rows), column_names))
            rows_by_table[table] = (rows, column_names)

    monkeypatch.setattr(pipeline.schema, "get_client", lambda: FakeClient())
    monkeypatch.setattr(pipeline.schema, "ensure_schema", lambda _client: None)

    run = pipeline.run_detection(
        "MINT", None, _dt(-3600), _dt(0), 100, label="stonkfun-test",
    )
    assert run.concentration.total_volume_usd == 30_000_000.0
    assert run.round_trip_count == 1
    assert len(run.fan_out_clusters) == 1
    assert run.fan_out_clusters[0]["funding_wallet"] == "FunderX"

    tables_written = {table for table, _count, _cols in inserted}
    assert tables_written == {
        "wash_trading_runs", "wash_trading_wallet_stats", "wash_trading_trades",
        "wash_trading_funding_edges", "wash_trading_clusters",
    }

    # wash_trading_wallet_stats.round_trip_count must reflect the real
    # per-wallet count, not the old hardcoded 0 -- wallet A has exactly the
    # one round trip detected above, wallet B (no trades fetched) has none.
    stats_rows, stats_cols = rows_by_table["wash_trading_wallet_stats"]
    round_trip_count_idx = stats_cols.index("round_trip_count")
    trader_id_idx = stats_cols.index("trader_id")
    counts = {row[trader_id_idx]: row[round_trip_count_idx] for row in stats_rows}
    assert counts == {"A": 1, "B": 0}


def test_run_detection_marks_run_failed_and_reraises_on_error(monkeypatch):
    monkeypatch.setattr(settings, "bitquery_api_key", "bitquery-key")

    def raise_error(token_mints, window_start, window_end, top_n):
        raise RuntimeError("Bitquery top-traders query failed after retry")

    monkeypatch.setattr(pipeline, "_bitquery_top_traders", raise_error)

    commands = []

    class FakeClient:
        def command(self, ddl):
            commands.append(ddl)

        def insert(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(pipeline.schema, "get_client", lambda: FakeClient())
    monkeypatch.setattr(pipeline.schema, "ensure_schema", lambda _client: None)

    import pytest
    with pytest.raises(RuntimeError, match="Bitquery top-traders"):
        pipeline.run_detection("MINT", None, _dt(-3600), _dt(0), 100)
    assert any("status = 'failed'" in cmd for cmd in commands)

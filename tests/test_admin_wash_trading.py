from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import main
from app.settings import settings
from app.wash_trading import pipeline


_BODY = {
    "token_mint": "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump",
    "pool_filter": None,
    "window_start": "2026-09-12T00:00:00Z",
    "window_end": "2026-09-13T00:00:00Z",
    "top_n": 50,
    "label": "stonkfun-test",
}


def test_create_run_requires_admin_auth(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-test-key")
    client = TestClient(main.app)
    assert client.post("/admin/wash-trading/runs", json=_BODY).status_code == 401


def test_create_run_is_503_when_admin_key_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", None)
    client = TestClient(main.app)
    response = client.post(
        "/admin/wash-trading/runs", json=_BODY, headers={"Authorization": "Bearer anything"},
    )
    assert response.status_code == 503


def test_create_run_calls_pipeline_and_returns_summary(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-test-key")

    def fake_run_detection(token_mint, pool_filter, window_start, window_end, top_n, label=None):
        assert token_mint == _BODY["token_mint"]
        assert top_n == 50
        assert label == "stonkfun-test"
        return pipeline.DetectionRun(
            run_id="run_abc", token_mint=token_mint, pool_filter=pool_filter,
            window_start=window_start, window_end=window_end, top_n=top_n,
            concentration=pipeline.ConcentrationStats(30_000_000.0, 2, 60.0, 100.0, 100.0, 100.0, 15_000_000.0, 4.5),
            round_trip_count=3,
            fan_out_clusters=[{"cluster_id": "c1", "funding_wallet": "FunderX", "wallets": ["A", "B"],
                                "cluster_size": 2, "cluster_volume_usd": 30_000_000.0}],
        )

    monkeypatch.setattr(main.wash_trading_pipeline, "run_detection", fake_run_detection)
    client = TestClient(main.app)
    response = client.post(
        "/admin/wash-trading/runs", json=_BODY, headers={"Authorization": "Bearer admin-test-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run_abc"
    assert body["round_trip_count"] == 3
    assert body["concentration"]["total_volume_usd"] == 30_000_000.0
    assert body["fan_out_clusters"][0]["funding_wallet"] == "FunderX"


def test_create_run_surfaces_a_pipeline_failure_as_502(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-test-key")

    def fake_run_detection(*_args, **_kwargs):
        raise RuntimeError("DUNE_API_KEY is not configured")

    monkeypatch.setattr(main.wash_trading_pipeline, "run_detection", fake_run_detection)
    client = TestClient(main.app)
    response = client.post(
        "/admin/wash-trading/runs", json=_BODY, headers={"Authorization": "Bearer admin-test-key"},
    )
    assert response.status_code == 502
    assert "DUNE_API_KEY" in response.json()["detail"]


def test_get_run_requires_admin_auth(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-test-key")
    client = TestClient(main.app)
    assert client.get("/admin/wash-trading/runs/run_abc").status_code == 401


def test_get_run_returns_404_when_not_found(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-test-key")

    class FakeQueryResult:
        def named_results(self):
            return iter([])

    class FakeClient:
        def query(self, _sql, parameters=None):
            return FakeQueryResult()

    monkeypatch.setattr(main.wash_trading_schema, "get_client", lambda: FakeClient())
    client = TestClient(main.app)
    response = client.get(
        "/admin/wash-trading/runs/missing", headers={"Authorization": "Bearer admin-test-key"},
    )
    assert response.status_code == 404


def test_get_run_returns_persisted_summary(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-test-key")

    class FakeQueryResult:
        def __init__(self, rows):
            self._rows = rows

        def named_results(self):
            return iter(self._rows)

    class FakeClient:
        def query(self, sql, parameters=None):
            if "wash_trading_runs" in sql:
                return FakeQueryResult([{"run_id": "run_abc", "status": "completed"}])
            if "wash_trading_wallet_stats" in sql:
                return FakeQueryResult([{"trader_id": "A", "total_volume_usd": 18_000_000.0}])
            return FakeQueryResult([{"cluster_id": "c1", "funding_wallet": "FunderX"}])

    monkeypatch.setattr(main.wash_trading_schema, "get_client", lambda: FakeClient())
    client = TestClient(main.app)
    response = client.get(
        "/admin/wash-trading/runs/run_abc", headers={"Authorization": "Bearer admin-test-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["run"]["run_id"] == "run_abc"
    assert body["wallet_stats"][0]["trader_id"] == "A"
    assert body["clusters"][0]["funding_wallet"] == "FunderX"


def test_get_wallet_drilldown_includes_nansen_label(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-test-key")

    class FakeQueryResult:
        def __init__(self, rows):
            self._rows = rows

        def named_results(self):
            return iter(self._rows)

    class FakeClient:
        def query(self, sql, parameters=None):
            if "wash_trading_trades" in sql:
                return FakeQueryResult([{"tx_id": "tx1", "is_round_trip_leg": True}])
            return FakeQueryResult([{"funding_wallet": "FunderX"}])

    monkeypatch.setattr(main.wash_trading_schema, "get_client", lambda: FakeClient())
    monkeypatch.setattr(main.nansen_enrich, "label_wallet", lambda _address, chain="solana": "Known: Jump Trading")
    client = TestClient(main.app)
    response = client.get(
        "/admin/wash-trading/runs/run_abc/wallets/WalletA", headers={"Authorization": "Bearer admin-test-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["round_trip_legs"] == [{"tx_id": "tx1", "is_round_trip_leg": True}]
    assert body["funding"][0]["funding_wallet"] == "FunderX"
    assert body["nansen_label"] == "Known: Jump Trading"

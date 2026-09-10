from fastapi.testclient import TestClient

from app.main import app
from app.provider_registry import get_provider_router
from app.settings import settings


def test_admin_provider_api_is_protected_and_persists_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "admin_api_key", "admin-test-key")
    monkeypatch.setattr(settings, "provider_overrides_path", str(tmp_path / "routing.json"))
    get_provider_router.cache_clear()
    client = TestClient(app)

    assert client.get("/admin/providers").status_code == 401
    headers = {"Authorization": "Bearer admin-test-key"}
    response = client.put(
        "/admin/providers/dexscreener_pair_search",
        headers=headers,
        json={"enabled": False, "priority": -2, "quota_per_minute": 12, "cost_usd": 0.01},
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["quota_per_minute"] == 12
    assert (tmp_path / "routing.json").exists()

    preview = client.post(
        "/admin/routes/preview",
        headers=headers,
        json={"request": "Research Apple stock", "capability": "equity_research", "chains": []},
    )
    assert preview.status_code == 200
    assert all(item["provider"] == "perplexity" for item in preview.json()["candidates"])


def test_equity_capability_is_not_registered_to_openai():
    get_provider_router.cache_clear()
    rows = get_provider_router().catalog()
    openai = next(row for row in rows if row["name"] == "openai_web_search")
    perplexity = [row for row in rows if row["provider"] == "perplexity"]
    assert "equity_research" not in openai["capabilities"]
    assert any("equity_research" in row["capabilities"] for row in perplexity)


def test_admin_intent_and_provider_sandboxes_are_protected(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-test-key")
    get_provider_router.cache_clear()
    client = TestClient(app)
    headers = {"Authorization": "Bearer admin-test-key"}

    intent = client.post(
        "/admin/intents/preview", headers=headers,
        json={"request": "Swap 0.1 SOL on Solana to USDC on Base"},
    )
    assert intent.status_code == 200
    assert intent.json()["intent"] == "cross_chain_swap"
    assert intent.json()["source"] == "rules"

    provider = client.post(
        "/admin/providers/test", headers=headers,
        json={"request": "Bitcoin price now", "capability": "finance_data", "live": False},
    )
    assert provider.status_code == 200
    assert provider.json()["mode"] == "dry_run"
    assert provider.json()["result"] is None


def test_live_intent_lab_requires_auth_and_only_classifies(monkeypatch):
    from app import main

    monkeypatch.setattr(settings, "admin_api_key", "test-admin")
    calls = []

    async def classify(state):
        calls.append(state)
        return {"intent": "research", "routing_decision": {"method": "embedding"}}

    monkeypatch.setattr(main, "resolve_intent_node", classify)
    client = TestClient(app)
    body = {"request": "Should I buy SOL?", "live": True}
    assert client.post("/admin/intents/preview", json=body).status_code == 401
    assert not calls
    result = client.post("/admin/intents/preview", json=body, headers={"Authorization": "Bearer test-admin"})
    assert result.status_code == 200
    assert result.json()["routing_decision"]["method"] == "embedding"
    assert len(calls) == 1

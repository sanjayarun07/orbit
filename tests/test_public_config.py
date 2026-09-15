from fastapi.testclient import TestClient

from app.main import _client_identity, app
from app.settings import settings
from starlette.requests import Request


def test_public_config_disables_privy_without_both_ids(monkeypatch):
    monkeypatch.setattr(settings, "privy_app_id", "app_test")
    monkeypatch.setattr(settings, "privy_client_id", None)

    response = TestClient(app).get("/config/public")

    assert response.status_code == 200
    assert response.json()["privy"] == {
        "enabled": False,
        "app_id": None,
        "client_id": None,
        "delegated_signing": False,
    }


def test_public_config_treats_example_placeholders_as_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "privy_app_id", "<your-privy-app-id>")
    monkeypatch.setattr(settings, "privy_client_id", "<your-privy-client-id>")
    payload = TestClient(app).get("/config/public").json()
    assert payload["privy"]["enabled"] is False


def test_public_config_exposes_only_browser_safe_wallet_policy(monkeypatch):
    monkeypatch.setattr(settings, "privy_app_id", "app_test")
    monkeypatch.setattr(settings, "privy_client_id", "client_test")

    payload = TestClient(app).get("/config/public").json()

    assert payload["privy"]["enabled"] is True
    assert payload["privy"]["app_id"] == "app_test"
    assert payload["privy"]["client_id"] == "client_test"
    assert payload["privy"]["delegated_signing"] is False
    assert payload["execution_policy"]["simulation_required"] is True
    assert payload["execution_policy"]["human_approval_required"] is True
    assert "secret" not in str(payload).lower()


def test_public_config_exposes_reown_identifier_and_lifi_availability(monkeypatch):
    monkeypatch.setattr(settings, "reown_project_id", "reown-public-project")
    monkeypatch.setattr(settings, "lifi_enabled", True)
    payload = TestClient(app).get("/config/public").json()
    assert payload["reown"] == {"enabled": True, "project_id": "reown-public-project"}
    assert payload["execution_providers"]["lifi_backup"] is True
    assert "api_key" not in str(payload).lower()


def test_coinbase_wallet_is_a_browser_safe_first_class_option():
    client = TestClient(app)
    payload = client.get("/config/public").json()
    assert payload["coinbase_wallet"] == {
        "enabled": True,
        "custody": "self_custody",
        "supports": ["smart_wallet", "extension", "mobile"],
    }
    assert client.get("/ui/coinbase.js").status_code == 200
    assert "connectCoinbaseBtn" in client.get("/ui/").text


def test_raw_tool_trajectory_exposure_defaults_off():
    assert type(settings).model_fields["expose_tool_trajectory"].default is False


def test_solana_rpc_proxy_rejects_unneeded_methods_without_calling_upstream():
    response = TestClient(app).post(
        "/rpc/solana",
        json={"jsonrpc": "2.0", "id": 1, "method": "getProgramAccounts", "params": []},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported Solana RPC method"


def test_solana_rpc_proxy_rejects_oversized_batches(monkeypatch):
    monkeypatch.setattr(settings, "rpc_max_batch_size", 2)
    payload = [
        {"jsonrpc": "2.0", "id": index, "method": "getBalance", "params": ["wallet"]}
        for index in range(3)
    ]
    response = TestClient(app).post("/rpc/solana", json=payload)
    assert response.status_code == 413
    assert response.json()["detail"] == "Solana RPC batch is too large"


def test_forwarded_identity_is_used_only_from_a_trusted_proxy(monkeypatch):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-forwarded-for", b"203.0.113.8")],
        "client": ("10.0.0.2", 1234),
        "server": ("test", 80),
        "scheme": "http",
        "query_string": b"",
    }
    request = Request(scope)
    monkeypatch.setattr(settings, "trusted_proxy_hosts", "")
    assert _client_identity(request) == "10.0.0.2"
    monkeypatch.setattr(settings, "trusted_proxy_hosts", "10.0.0.2")
    assert _client_identity(request) == "203.0.113.8"


def test_relay_how_it_works_chat_does_not_request_trade_fields(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", None)
    response = TestClient(app).post(
        "/chat",
        json={"message": "how relay bridge works?", "session_id": "relay-explanation-test"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "general"
    assert payload["cross_chain_swap"] is None
    assert payload["answer"].startswith("Relay is the routing and execution layer")
    assert "before I can prepare" not in payload["answer"]
    # Quick-action / suggestion chips are disabled -- responses carry none.
    assert payload["suggestions"] == []
    assert payload["quick_actions"] == []
    assert payload["session_revision"] == 1
    from tests.conftest import sign_in
    history_client = TestClient(app)
    sign_in(history_client)
    history = history_client.get("/chat/history/relay-explanation-test").json()
    assert history["context"]["revision"] == 1
    assert history["context"]["last_intent"] == "general"


def test_chat_rejects_stale_context_revision_instead_of_reclassifying(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", None)
    client = TestClient(app)
    session_id = "revision-conflict-test"
    first = client.post(
        "/chat",
        json={"message": "how relay bridge works?", "session_id": session_id},
    )
    assert first.status_code == 200
    assert first.json()["session_revision"] == 1

    stale = client.post(
        "/chat",
        json={
            "message": "Show more recent developments",
            "session_id": session_id,
            "context_revision": 0,
        },
    )
    assert stale.status_code == 409
    assert "changed" in stale.json()["detail"]


def test_chat_rejects_client_forged_quick_action_capabilities(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", None)
    client = TestClient(app)
    session_id = "forged-action-test"
    first = client.post(
        "/chat",
        json={"message": "how relay bridge works?", "session_id": session_id},
    ).json()
    # Quick actions are no longer emitted, so a client-supplied quick_action can
    # never be authorized by the latest response -- the server must still reject
    # a fabricated one rather than trusting its claimed intent/capabilities.
    forged = {
        "id": "forged.0",
        "prompt": "how relay bridge works?",
        "intent": "trade",
        "capabilities": ["swap", "token_resolve", "token_security"],
        "context_revision": first["session_revision"],
    }
    response = client.post(
        "/chat",
        json={
            "message": forged["prompt"],
            "session_id": session_id,
            "context_revision": first["session_revision"],
            "quick_action": forged,
        },
    )
    assert response.status_code == 409
    assert "not authorized" in response.json()["detail"]

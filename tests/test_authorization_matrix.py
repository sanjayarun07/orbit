"""Every route's authorization, declared once and enforced by this test.

From an external review: "I would not approve public money-moving
functionality without an explicit authorization matrix and adversarial
tests." Account APIs, admin APIs, execution endpoints and wallet auth had
different models and no single place said what each route required.

A new route must be classified here or the build fails -- that is the
point: the matrix cannot drift from the code.

Classes:
  public          no caller identity needed (health, quotes, public config)
  auth_entry      unauthenticated by design -- these ESTABLISH identity
  webhook         authenticated by provider signature, not by session
  user            a signed-in account of any kind (require_user) -- now only
                  for routes that genuinely accept API keys; see the two below
  browser         a signed-in BROWSER session (require_browser_session): API
                  keys are refused. Every route that manages the account.
  key:<scope>     a browser session or an API key holding <scope>
                  (require_scope). The only routes a key may use.
  admin           requires the admin API key
  owner_scoped    no account dependency, but access is bound to the
                  resource's own owner/secret at the handler (chat
                  sessions, trade plans, execution tracking)
"""
import inspect

import pytest
from fastapi.testclient import TestClient

from app.main import app

EXPECTED: dict[tuple[str, str], str] = {
    # --- identity establishment: unauthenticated on purpose --------------
    ("POST", "/auth/email/start"): "auth_entry",
    ("POST", "/auth/email/verify"): "auth_entry",
    ("POST", "/auth/wallet/challenge"): "auth_entry",
    ("POST", "/auth/wallet/verify"): "auth_entry",
    ("POST", "/auth/coinbase/challenge"): "auth_entry",
    ("POST", "/auth/coinbase/verify"): "auth_entry",
    ("POST", "/auth/logout"): "auth_entry",
    ("POST", "/auth/signout"): "auth_entry",
    # --- provider-signed ---------------------------------------------------
    ("POST", "/billing/webhook"): "webhook",
    # --- public read-only --------------------------------------------------
    ("GET", "/health"): "public",
    ("GET", "/readyz"): "public",
    ("GET", "/config/public"): "public",
    ("GET", "/capabilities"): "public",
    ("GET", "/billing/plans"): "public",
    ("GET", "/calendar"): "public",
    ("GET", "/home/highlights"): "public",
    ("GET", "/tokens/search"): "public",
    ("GET", "/knowledge/search"): "public",
    ("GET", "/knowledge/status"): "public",
    ("GET", "/knowledge/graph/{entity_id:path}"): "public",
    ("GET", "/chat/risk-charter/limits"): "public",
    ("POST", "/rpc/solana"): "public",
    ("POST", "/execution/lifi/quote"): "public",
    ("GET", "/execution/lifi/status/{tx_hash}"): "public",
    ("GET", "/executions/solana/{signature}"): "public",
    # --- bound to the resource's own owner ---------------------------------
    ("GET", "/me"): "owner_scoped",          # answers for anonymous callers too; returns only the CALLER's own state
    ("GET", "/home/suggestions"): "public",
    # Public on-chain data for an address the caller supplies explicitly.
    ("GET", "/portfolio/{wallet_address}"): "key:data",
    ("POST", "/portfolio/{wallet_address}/scenario"): "key:data",
    ("GET", "/wallet-health/{wallet_address}"): "key:data",
    ("POST", "/chat"): "owner_scoped",
    # The same turn as POST /chat, streamed: it runs execute_chat_turn itself,
    # so ownership is enforced identically -- the enforcement test proves it.
    ("POST", "/chat/stream"): "owner_scoped",
    ("POST", "/chat/feedback"): "owner_scoped",
    ("GET", "/chat/history/{session_id}"): "key:chat",
    ("DELETE", "/chat/history/{session_id}"): "browser",
    ("DELETE", "/chat/wallet/{session_id}"): "browser",
    ("GET", "/me/credits"): "key:data",
    ("GET", "/trade-plans/{plan_id}"): "owner_scoped",
    ("POST", "/trade-plans/{plan_id}/confirm"): "owner_scoped",
    ("POST", "/trade-plans/{plan_id}/wallet-transaction"): "owner_scoped",
    ("POST", "/trade-plans/{plan_id}/submit-wallet-transaction"): "owner_scoped",
    ("POST", "/executions/relay/{request_id}"): "owner_scoped",
    # Status of a third-party (Relay) bridge execution, keyed by Relay's own
    # unguessable request id; it exposes no account data, and the tracking
    # POST is what binds an execution to a conversation.
    ("GET", "/executions/relay/{request_id}"): "public",
    ("GET", "/executions/relay/session/{session_id}/{revision}"): "owner_scoped",
}

# Everything under these prefixes is classified by prefix.
# Account management is a browser's job; a key may only read data and chat.
PREFIX_RULES = [("/admin", "admin"), ("/me", "browser"), ("/billing/checkout", "browser"), ("/billing/portal", "browser")]
IGNORED_PREFIXES = ("/ui", "/mcp", "/openapi", "/docs", "/redoc", "/static")


def _routes():
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = (getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"}
        if not path or not methods or path.startswith(IGNORED_PREFIXES):
            continue
        for method in sorted(methods):
            yield method, path, route


def _declared(method: str, path: str) -> str | None:
    if (method, path) in EXPECTED:
        return EXPECTED[(method, path)]
    for prefix, cls in PREFIX_RULES:
        if path.startswith(prefix):
            return cls
    return None


def _guards(route) -> set[str]:
    endpoint = route.endpoint
    found = set()
    try:
        for param in inspect.signature(endpoint).parameters.values():
            dep = getattr(param.default, "dependency", None)
            if dep is not None:
                found.add(getattr(dep, "__name__", str(dep)))
    except (TypeError, ValueError):
        pass
    try:
        source = inspect.getsource(endpoint)
    except OSError:
        source = ""
    for marker in ("_require_admin", "_require_session_access", "_require_plan_access", "resolve_identity", "require_user",
                   "require_browser_session", "require_scope"):
        if marker in source:
            found.add(marker)
    return found


def test_every_route_is_classified():
    unclassified = [f"{m} {p}" for m, p, _ in _routes() if _declared(m, p) is None]
    assert unclassified == [], (
        f"unclassified routes: {unclassified}. Add them to EXPECTED in this file -- "
        "a route with no declared authorization class is exactly the gap this test exists to catch."
    )


@pytest.mark.parametrize("method,path,route", list(_routes()), ids=lambda v: v if isinstance(v, str) else "")
def test_route_enforcement_matches_its_declared_class(method, path, route):
    declared = _declared(method, path)
    guards = _guards(route)
    if declared == "user":
        assert "require_user" in guards, f"{method} {path} is declared user-scoped but has no require_user dependency"
    elif declared == "browser":
        assert "require_browser_session" in guards, (
            f"{method} {path} is declared browser-only but does not use require_browser_session; "
            "an API key could reach it"
        )
    elif declared.startswith("key:"):
        scope = declared.split(":", 1)[1]
        assert f"require_scope_{scope}" in guards, f"{method} {path} is declared key:{scope} but does not check that scope"
    elif declared == "admin":
        assert "_require_admin" in guards or "require_admin" in guards, f"{method} {path} is declared admin but is not admin-guarded"
    elif declared == "owner_scoped":
        assert guards & {"_require_session_access", "_require_plan_access", "resolve_identity"}, (
            f"{method} {path} is declared owner-scoped but checks no owner"
        )


def test_money_moving_endpoints_are_never_unguarded():
    """The three that can move funds must each bind to the plan's owner."""
    money = {"/trade-plans/{plan_id}/confirm", "/trade-plans/{plan_id}/wallet-transaction",
             "/trade-plans/{plan_id}/submit-wallet-transaction"}
    seen = set()
    for method, path, route in _routes():
        if path in money:
            seen.add(path)
            assert "_require_plan_access" in _guards(route), f"{path} does not enforce plan ownership"
            assert "require_browser_session" in _guards(route), (
                f"{path} accepts an API key; moving funds is a browser act and a data-only key "
                "belonging to an allowlisted account could otherwise have the server sign its plan"
            )
    assert seen == money, f"a money-moving endpoint disappeared or was renamed: {money - seen}"


def test_admin_routes_reject_an_unauthenticated_caller():
    client = TestClient(app)
    for method, path, _ in _routes():
        if not path.startswith("/admin") or "{" in path:
            continue
        response = client.request(method, path, json={})
        assert response.status_code in (401, 403), f"{method} {path} returned {response.status_code} without an admin key"


def test_user_routes_reject_an_unauthenticated_caller():
    client = TestClient(app)
    checked = 0
    for method, path, _ in _routes():
        if _declared(method, path) not in ("user", "browser") and not str(_declared(method, path)).startswith("key:") or "{" in path:
            continue
        response = client.request(method, path, json={})
        assert response.status_code in (401, 403), f"{method} {path} returned {response.status_code} while signed out"
        checked += 1
    assert checked >= 8


# --- adversarial: a plan id must not be a bearer token for execution ------

def test_another_caller_cannot_touch_someone_elses_trade_plan(monkeypatch):
    """confirmation_text is "CONFIRM {plan_id}", so before plan ownership
    existed, anyone who obtained a plan id held every secret the execution
    endpoints checked.

    Run in execution mode deliberately. In the default research deployment
    these routes refuse everyone with 403 before ownership is consulted, which
    would make this pass for the wrong reason -- ownership has to be what stops
    the intruder, on a deployment where execution is genuinely available.
    """
    import asyncio

    from app.settings import settings as app_settings
    monkeypatch.setattr(app_settings, "deployment_mode", "execution")
    monkeypatch.setattr(app_settings, "live_trading", True)

    from app import plans
    from app.models import SwapProposal, TokenInfo, TradePlan
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    plan = TradePlan(
        plan_id="plan-owned-by-alice", status="pending_confirmation", created_at=now,
        expires_at=now + timedelta(minutes=10), wallet_address="AliceWallet",
        proposal=SwapProposal(input_mint="So11111111111111111111111111111111111111112",
                              output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                              amount_atomic=1000, slippage_bps=50, reason="test fixture"),
        quote={}, input_token=TokenInfo(mint="So11111111111111111111111111111111111111112", symbol="SOL", name="Solana", decimals=9),
        output_token=TokenInfo(mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", symbol="USDC", name="USD Coin", decimals=6),
        simulation={"ok": True}, confirmation_text="CONFIRM plan-owned-by-alice",
        owner_account_id="user:alice",
    )

    async def fake_get_plan(plan_id):
        if plan_id == plan.plan_id:
            return plan
        raise KeyError(plan_id)

    # Patch the single real loader, not main's imported alias: the ownership
    # gate and every handler must read a plan through the same function, or the
    # gate can answer "no such plan" while the handler goes on to find one.
    monkeypatch.setattr("app.plans.get_plan", fake_get_plan)
    monkeypatch.setattr("app.main.get_plan", fake_get_plan)
    from tests.conftest import sign_in

    # An anonymous caller never reaches ownership: the execution routes refuse
    # it first (401), and that answer is the same for every plan id.
    anonymous = TestClient(app)
    for path in (f"/trade-plans/{plan.plan_id}/confirm", f"/trade-plans/{plan.plan_id}/wallet-transaction"):
        assert anonymous.post(path, json={"confirmation_text": plan.confirmation_text}).status_code == 401
    intruder = TestClient(app)   # a different, signed-in account: the real ownership test
    sign_in(intruder, email="intruder@example.com")
    body = {"confirmation_text": plan.confirmation_text}
    for method, path in [
        ("GET", f"/trade-plans/{plan.plan_id}"),
        ("POST", f"/trade-plans/{plan.plan_id}/confirm"),
        ("POST", f"/trade-plans/{plan.plan_id}/wallet-transaction"),
    ]:
        response = intruder.request(method, path, json=body)
        assert response.status_code == 404, f"{method} {path} leaked to a non-owner ({response.status_code})"

    signed = intruder.post(f"/trade-plans/{plan.plan_id}/submit-wallet-transaction",
                           json={**body, "signed_transaction": "AA=="})
    assert signed.status_code == 404


def test_plans_are_stamped_with_the_account_that_requested_them():
    from app import plans

    token = plans.set_plan_owner("user:someone")
    try:
        assert plans.plan_owner() == "user:someone"
    finally:
        plans.reset_plan_owner(token)
    assert plans.plan_owner() is None

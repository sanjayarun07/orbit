import asyncio
import hmac
import ipaddress
import logging
import os
import re
from contextlib import asynccontextmanager

# Configured before any other app module logs (MCP discovery, provider
# failures, routing decisions) so those records actually reach stdout instead
# of being silently dropped by the default root logger level.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.responses import JSONResponse
from app.limits import allow_auth_request
from app.public_activity import public_activity
from app.reconciliation import reconciliation_worker
from app import relay_tracking
from fastapi.staticfiles import StaticFiles

from app.execution import execute_confirmed_plan, prepare_wallet_transaction, submit_wallet_transaction
from app.graph import run_agent, resolve_intent_node
from app.capability_router import route_capabilities
from app.routing.controls import is_trade_confirmation
from app.experience import (
    advance_session_context,
    build_context_capsules,
    build_evidence_summary,
    with_resolved_token,
    build_gas_advisory,
    build_intent_lock,
    build_trade_readiness,
)
from app.answer_validator import validate_answer
from app.limits import acquire_chat_slot, allow_chat_request, allow_rpc_request, release_chat_slot
from app.lifi import get_quote as get_lifi_quote, get_status as get_lifi_status
from app.mcp_tools import close_mcp_gateway, discover_mcp_tools, get_mcp_registry
from app.metrics import increment, snapshot
from app.wash_trading import nansen_enrich, pipeline as wash_trading_pipeline, schema as wash_trading_schema
from app import accounts, api_keys, billing, billing_plans, credits, mcp_server, tool_outcomes, x402_gate
from app.identity import Identity, current_identity, require_user, resolve_identity, service_identity
from app.models import TRADING_CHAINS
from app.models import (
    AdminCreditGrant,
    AdminPlanUpdate,
    AgentResponse,
    ApiKeyCreate,
    CheckoutRequest,
    ChatRequest,
    ConfirmRequest,
    EmailSigninStart,
    EmailSigninVerify,
    PreferencesUpdate,
    IntentPreviewRequest,
    LifiQuoteRequest,
    ProviderPolicyUpdate,
    ProviderTestRequest,
    PortfolioScenarioRequest,
    RoutePreviewRequest,
    SignedTransactionRequest,
    WalletAuthChallengeRequest,
    WalletAuthVerifyRequest,
    WashTradingDetectionRequest,
)
from app.plans import get_plan, mark_plan_superseded
from app.routing.workflow import WorkflowState, WorkflowEvent, apply_event
from app.provider_registry import get_provider_router, save_provider_overrides
from app.settings import settings
from app.portfolio import build_portfolio_snapshot
from app.wallet_insights import portfolio_scenario, wallet_health
from app.wallet_auth import (
    COOKIE_NAME,
    SESSION_TTL,
    create_challenge,
    delete_auth_session,
    get_auth_session,
    verify_challenge,
)
from app.sessions import (
    acquire_session_turn,
    clear_history,
    commit_turn,
    get_messages,
    get_session_context,
    get_session_snapshot,
    history_text_from_messages,
)
from app.suggestions import structured_quick_actions, suggested_actions
from app.solana_rpc import rpc


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    discovery = asyncio.create_task(asyncio.to_thread(discover_mcp_tools))
    reconciliation = asyncio.create_task(reconciliation_worker())
    relay_reconciliation = asyncio.create_task(relay_tracking.worker())
    outcomes_refresh = asyncio.create_task(tool_outcomes.refresh_worker())
    try:
        async with mcp_server.mcp.session_manager.run():
            yield
    finally:
        reconciliation.cancel()
        relay_reconciliation.cancel()
        outcomes_refresh.cancel()
        await asyncio.gather(reconciliation, relay_reconciliation, outcomes_refresh, discovery, return_exceptions=True)
        await asyncio.to_thread(close_mcp_gateway)


app = FastAPI(title="Orbit Web3 Copilot", version="0.3.0", lifespan=lifespan)


@app.middleware("http")
async def x402_payment(request: Request, call_next):
    return await x402_gate.chat_payment_gate(request, call_next)


@app.middleware("http")
async def auth_admission(request: Request, call_next):
    if request.url.path.startswith("/auth/"):
        allowed, retry = await allow_auth_request(request.client.host if request.client else "anonymous")
        if not allowed:
            return JSONResponse({"detail": "Too many authentication requests"}, status_code=429, headers={"Retry-After": str(retry)})
    return await call_next(request)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")


@app.middleware("http")
async def mcp_admission(request: Request, call_next):
    # The MCP endpoint is the one surface an outside agent host drives directly;
    # a configured key gates it (the chat UI never needs it).
    if request.url.path.startswith("/mcp"):
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if supplied.startswith(api_keys.KEY_PREFIX):
            # A user's own key: the tool calls run as that user (credits, plan).
            try:
                identity = await resolve_identity(request)
            except HTTPException as exc:
                return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
            if not identity.plan.mcp or not identity.has_scope("mcp"):
                return JSONResponse({"error": "This API key or plan does not allow MCP access"}, status_code=403)
            token = current_identity.set(identity)
            try:
                return await call_next(request)
            finally:
                current_identity.reset(token)
        if settings.mcp_api_key:
            if not supplied or not hmac.compare_digest(supplied, settings.mcp_api_key):
                return JSONResponse({"error": "MCP requires a valid Authorization: Bearer key"}, status_code=401)
    return await call_next(request)


app.mount("/mcp", mcp_server.mcp.streamable_http_app(), name="mcp")

@app.get("/health")
async def health():
    return {"status": "ok", "counters": snapshot()}


@app.post("/auth/coinbase/challenge")
async def coinbase_auth_challenge(body: WalletAuthChallengeRequest, request: Request):
    """Create a short-lived, address-bound login message; no transaction is created."""
    host = request.headers.get("host", request.url.hostname or "Orbit").split("/", 1)[0]
    uri = f"{request.url.scheme}://{host}"
    try:
        return await create_challenge(body.address, host, uri, body.chain_id)
    except ValueError as exc:
        raise HTTPException(400, _safe_detail(exc, "Unsupported wallet network")) from exc


@app.post("/auth/coinbase/verify")
async def coinbase_auth_verify(
    body: WalletAuthVerifyRequest, response: Response, request: Request, identity: Identity = Depends(require_user)
):
    try:
        token, session = await verify_challenge(body.address, body.nonce, body.signature)
        await accounts.link_wallet(identity.user["id"], "evm", session["address"])
    except (ValueError, TypeError) as exc:
        raise HTTPException(401, _safe_detail(exc, "Wallet authentication failed")) from exc
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    return {"authenticated": True, **session}


@app.get("/auth/session")
async def auth_session(request: Request):
    session = await get_auth_session(request.cookies.get(COOKIE_NAME))
    return {"authenticated": bool(session), **(session or {})}


@app.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    await delete_auth_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"authenticated": False}


@app.post("/execution/lifi/quote")
async def lifi_quote(payload: LifiQuoteRequest):
    """Create a backup quote only; signing/submission remains wallet-controlled."""
    params = {
        "fromChain": payload.from_chain, "toChain": payload.to_chain,
        "fromToken": payload.from_token, "toToken": payload.to_token,
        "fromAmount": payload.from_amount, "fromAddress": payload.from_address,
    }
    if payload.to_address:
        params["toAddress"] = payload.to_address
    if payload.slippage is not None:
        params["slippage"] = str(payload.slippage)
    try:
        return await asyncio.to_thread(get_lifi_quote, params)
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, _safe_detail(exc, "LI.FI quote is currently unavailable")) from exc


@app.get("/execution/lifi/status/{tx_hash}")
async def lifi_status(tx_hash: str, from_chain: str | None = None, to_chain: str | None = None):
    if not re.fullmatch(r"(?:0x)?[A-Za-z0-9]{32,128}", tx_hash):
        raise HTTPException(400, "Invalid transaction hash")
    try:
        return await asyncio.to_thread(get_lifi_status, tx_hash, from_chain=from_chain, to_chain=to_chain)
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, _safe_detail(exc, "LI.FI status is currently unavailable")) from exc


_SOLANA_BROWSER_RPC_METHODS = {
    "getAccountInfo",
    "getBalance",
    "getBlockHeight",
    "getEpochInfo",
    "getLatestBlockhash",
    "getSignatureStatuses",
    "getTokenAccountBalance",
    "getTokenAccountsByOwner",
    "isBlockhashValid",
    "sendTransaction",
    "simulateTransaction",
}


def _client_identity(request: Request) -> str:
    """Trust forwarding headers only when the immediate peer is explicitly trusted."""
    peer = request.client.host if request.client else "anonymous"
    trusted = {item.strip() for item in settings.trusted_proxy_hosts.split(",") if item.strip()}
    if peer in trusted:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


def _safe_detail(exc: Exception, fallback: str) -> str:
    """Preserve short validation guidance without reflecting provider payloads or HTML."""
    detail = str(exc).strip()
    if not detail or len(detail) > 400 or "<!DOCTYPE" in detail or "jsonrpc" in detail.lower():
        return fallback
    return re.sub(r"\s+", " ", detail)


@app.post("/rpc/solana")
async def solana_browser_rpc(request: Request):
    """Proxy the small RPC surface needed by wallet adapters without leaking provider keys."""
    try:
        content_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError as exc:
        raise HTTPException(400, "Invalid Content-Length header") from exc
    if content_length > settings.rpc_max_body_bytes:
        raise HTTPException(413, "Solana RPC request is too large")
    body = await request.body()
    if len(body) > settings.rpc_max_body_bytes:
        raise HTTPException(413, "Solana RPC request is too large")
    try:
        import json
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Invalid JSON-RPC request") from exc
    calls = payload if isinstance(payload, list) else [payload]
    if len(calls) > settings.rpc_max_batch_size:
        raise HTTPException(413, "Solana RPC batch is too large")
    if not calls or any(
        not isinstance(call, dict) or call.get("method") not in _SOLANA_BROWSER_RPC_METHODS
        for call in calls
    ):
        raise HTTPException(400, "Unsupported Solana RPC method")
    allowed, retry_after = await allow_rpc_request(_client_identity(request), len(calls))
    if not allowed:
        increment("rpc_rate_limited")
        raise HTTPException(429, "Too many RPC requests", headers={"Retry-After": str(retry_after)})
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            upstream = await client.post(settings.solana_rpc_url, json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Configured Solana RPC is unavailable") from exc
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json").split(";", 1)[0],
    )


@app.get("/config/public")
async def public_config():
    """Return browser-safe feature configuration; never expose signing secrets."""
    privy_enabled = bool(
        settings.privy_app_id
        and settings.privy_client_id
        and not settings.privy_app_id.startswith("<")
        and not settings.privy_client_id.startswith("<")
    )
    return {
        "privy": {
            "enabled": privy_enabled,
            "app_id": settings.privy_app_id if privy_enabled else None,
            "client_id": settings.privy_client_id if privy_enabled else None,
            "delegated_signing": False,
        },
        "reown": {
            "enabled": bool(settings.reown_project_id),
            "project_id": settings.reown_project_id,
        },
        "coinbase_wallet": {
            "enabled": True,
            "custody": "self_custody",
            "supports": ["smart_wallet", "extension", "mobile"],
        },
        "execution_providers": {
            "jupiter": True,
            "relay": True,
            "lifi_backup": settings.lifi_enabled,
        },
        "x402": x402_gate.public_config(),
        "accounts": {
            "enabled": True,
            "product_name": settings.product_name,
            "trial_credits": billing_plans.ANONYMOUS.trial_credits,
            "email_configured": bool(settings.resend_api_key),
        },
        "execution_policy": {
            "live_trading": settings.live_trading,
            "max_trade_usd": settings.max_trade_usd,
            "max_slippage_bps": settings.max_slippage_bps,
            "max_price_impact_pct": settings.max_price_impact_pct,
            "simulation_required": True,
            "human_approval_required": True,
        },
    }


@app.get("/capabilities")
async def capabilities():
    """Inspect discovered providers and routing metadata without tool schemas."""
    registry = get_mcp_registry()
    return {
        "tools": [*get_provider_router().catalog(), *registry.catalog()],
        "mcp_discovery_ready": registry.ready,
    }


def _require_admin(request: Request) -> None:
    if not settings.admin_api_key:
        raise HTTPException(503, "ADMIN_API_KEY is not configured")
    authorization = request.headers.get("Authorization", "")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not supplied or not hmac.compare_digest(supplied, settings.admin_api_key):
        raise HTTPException(401, "Invalid admin credentials")


@app.get("/admin/providers")
async def admin_providers(request: Request):
    _require_admin(request)
    return {"tools": get_provider_router().catalog(), "overrides": get_provider_router().overrides()}


@app.get("/admin/tools/outcomes")
async def admin_tool_outcomes(request: Request):
    """Per-tool grounded-success rates and the ranking adjustment each earns."""
    _require_admin(request)
    return {
        "window_days": settings.tool_outcome_window_days,
        "prior_rate": settings.tool_outcome_prior_rate,
        "prior_weight": settings.tool_outcome_prior_weight,
        "weight": settings.provider_outcome_weight,
        "tools": tool_outcomes.snapshot(),
    }


@app.put("/admin/providers/{tool_name}")
async def update_admin_provider(tool_name: str, body: ProviderPolicyUpdate, request: Request):
    _require_admin(request)
    try:
        get_provider_router().configure(tool_name, body.model_dump(exclude_none=True))
        save_provider_overrides()
    except KeyError as exc:
        raise HTTPException(404, f"Unknown provider tool: {tool_name}") from exc
    return next(row for row in get_provider_router().catalog() if row["name"] == tool_name)


@app.delete("/admin/providers/{tool_name}")
async def reset_admin_provider(tool_name: str, request: Request):
    _require_admin(request)
    get_provider_router().reset(tool_name)
    save_provider_overrides()
    return {"status": "reset", "tool": tool_name}


@app.put("/admin/provider-groups/{provider}")
async def update_admin_provider_group(provider: str, body: ProviderPolicyUpdate, request: Request):
    _require_admin(request)
    if body.enabled is None:
        raise HTTPException(400, "Provider group updates require enabled=true or enabled=false")
    try:
        count = get_provider_router().configure_provider(provider, body.enabled)
        save_provider_overrides()
    except KeyError as exc:
        raise HTTPException(404, f"Unknown provider: {provider}") from exc
    return {"provider": provider, "enabled": body.enabled, "updated_tools": count}


@app.post("/admin/routes/preview")
async def preview_admin_route(body: RoutePreviewRequest, request: Request):
    _require_admin(request)
    return {
        "request": body.request,
        "capability": body.capability,
        "candidates": get_provider_router().preview(body.request, body.capability, tuple(body.chains)),
    }


@app.post("/admin/intents/preview")
async def preview_admin_intent(body: IntentPreviewRequest, request: Request):
    """Dry-run deterministic classification without spending model/provider tokens."""
    _require_admin(request)
    if body.live:
        return await resolve_intent_node({"request": body.request, "history": body.conversation_history, "session_context": {}})
    route = route_capabilities(body.request)
    if route is None:
        return {
            "request": body.request,
            "resolved": False,
            "intent": None,
            "capabilities": [],
            "chains": [],
            "source": "model_fallback_required",
            "note": "No high-confidence deterministic rule matched; production would use the DSPy classifier with conversation context.",
        }
    return {
        "request": body.request,
        "resolved": True,
        "intent": route.intent,
        "capabilities": list(route.capabilities),
        "chains": list(route.chains),
        "source": route.source,
        "confidence": route.confidence,
        "mode": route.mode,
        "execution_provider": route.execution_provider,
        "missing_fields": list(route.missing_fields),
        "reason": route.reason,
        "note": "Dry run only; no provider was called and no quota was consumed.",
    }


@app.post("/admin/providers/test")
async def test_admin_provider(body: ProviderTestRequest, request: Request):
    """Preview or explicitly invoke the read-only provider route in the admin lab."""
    _require_admin(request)
    router = get_provider_router()
    candidates = router.preview(body.request, body.capability, tuple(body.chains))
    if not body.live:
        return {"mode": "dry_run", "candidates": candidates, "result": None}
    result = await asyncio.to_thread(
        router.route,
        body.request,
        body.capability,
        tuple(body.chains),
        provider=body.provider,
    )
    return {
        "mode": "live_read_only",
        "candidates": candidates,
        "result": {
            "provider": result.provider,
            "tool": result.tool,
            "cached": result.cached,
            "attempted": list(result.attempted),
            "failures": list(result.failures),
            "output": result.output,
        },
    }


@app.post("/admin/wash-trading/runs")
async def create_wash_trading_run(body: WashTradingDetectionRequest, request: Request):
    """Kick off a wash-trading/wallet-clustering detection run (Phase 1:
    concentration stats, round-trip detection, single-hop funding fan-out).
    Every output here is a lead for a human analyst to review, never an
    automated verdict -- see app/wash_trading/pipeline.py's module docstring.
    """
    _require_admin(request)
    try:
        run = await asyncio.to_thread(
            wash_trading_pipeline.run_detection,
            body.token_mint, body.pool_filter, body.window_start, body.window_end, body.top_n, body.label,
        )
    except RuntimeError as exc:
        # DUNE_API_KEY / BITQUERY_API_KEY / CLICKHOUSE_HOST unconfigured, or a
        # Dune execution failure -- see app/dune_tools.py's DuneQueryError.
        raise HTTPException(502, _safe_detail(exc, "Wash-trading detection run failed")) from exc
    return {
        "run_id": run.run_id,
        "token_mint": run.token_mint,
        "pool_filter": run.pool_filter,
        "window_start": run.window_start.isoformat(),
        "window_end": run.window_end.isoformat(),
        "top_n": run.top_n,
        "concentration": vars(run.concentration),
        "round_trip_count": run.round_trip_count,
        "fan_out_clusters": run.fan_out_clusters,
    }


@app.get("/admin/wash-trading/runs/{run_id}")
async def get_wash_trading_run(run_id: str, request: Request):
    _require_admin(request)

    def _read() -> dict:
        client = wash_trading_schema.get_client()
        run_rows = client.query(
            "SELECT * FROM wash_trading_runs WHERE run_id = {run_id:String} ORDER BY created_at DESC LIMIT 1",
            parameters={"run_id": run_id},
        ).named_results()
        run_row = next(iter(run_rows), None)
        if run_row is None:
            return {}
        stats_rows = list(client.query(
            "SELECT * FROM wash_trading_wallet_stats WHERE run_id = {run_id:String} ORDER BY total_volume_usd DESC",
            parameters={"run_id": run_id},
        ).named_results())
        cluster_rows = list(client.query(
            "SELECT * FROM wash_trading_clusters WHERE run_id = {run_id:String} ORDER BY cluster_volume_usd DESC",
            parameters={"run_id": run_id},
        ).named_results())
        return {"run": run_row, "wallet_stats": stats_rows, "clusters": cluster_rows}

    result = await asyncio.to_thread(_read)
    if not result:
        raise HTTPException(404, f"No wash-trading run found for run_id={run_id}")
    return result


@app.get("/admin/wash-trading/runs/{run_id}/wallets/{wallet}")
async def get_wash_trading_wallet(run_id: str, wallet: str, request: Request):
    _require_admin(request)

    def _read() -> dict:
        client = wash_trading_schema.get_client()
        trades = list(client.query(
            "SELECT * FROM wash_trading_trades WHERE run_id = {run_id:String} AND trader_id = {wallet:String} ORDER BY block_time",
            parameters={"run_id": run_id, "wallet": wallet},
        ).named_results())
        round_trips = [row for row in trades if row.get("is_round_trip_leg")]
        funding = list(client.query(
            "SELECT * FROM wash_trading_funding_edges WHERE run_id = {run_id:String} AND wallet = {wallet:String} LIMIT 1",
            parameters={"run_id": run_id, "wallet": wallet},
        ).named_results())
        return {"trades": trades, "round_trip_legs": round_trips, "funding": funding}

    result = await asyncio.to_thread(_read)
    label = await asyncio.to_thread(nansen_enrich.label_wallet, wallet)
    return {**result, "nansen_label": label}


@app.get("/portfolio/{wallet_address}")
async def portfolio(wallet_address: str, identity: Identity = Depends(require_user)):
    try:
        return await build_portfolio_snapshot(wallet_address)
    except ValueError as exc:
        raise HTTPException(400, _safe_detail(exc, "Invalid wallet request")) from exc
    except Exception as exc:
        error_id = str(uuid4())
        logger.exception("Portfolio request failed [%s]", error_id)
        raise HTTPException(502, f"Portfolio provider is unavailable (reference {error_id})") from exc


@app.get("/wallet-health/{wallet_address}")
async def wallet_health_report(wallet_address: str, identity: Identity = Depends(require_user)):
    try:
        return wallet_health(await build_portfolio_snapshot(wallet_address))
    except ValueError as exc:
        raise HTTPException(400, _safe_detail(exc, "Invalid wallet request")) from exc
    except Exception as exc:
        raise HTTPException(502, _safe_detail(exc, "Wallet health data is unavailable")) from exc


@app.post("/portfolio/{wallet_address}/scenario")
async def portfolio_scenario_report(wallet_address: str, body: PortfolioScenarioRequest, identity: Identity = Depends(require_user)):
    try:
        snapshot = await build_portfolio_snapshot(wallet_address)
        return portfolio_scenario(snapshot, body.change_pct, body.symbol)
    except ValueError as exc:
        raise HTTPException(400, _safe_detail(exc, "Invalid scenario")) from exc
    except Exception as exc:
        raise HTTPException(502, _safe_detail(exc, "Portfolio scenario data is unavailable")) from exc


@app.post("/chat", response_model=AgentResponse)
async def chat(body: ChatRequest, request: Request):
    return await execute_chat_turn(body, await resolve_identity(request))


async def execute_chat_turn(body: ChatRequest, identity: Identity | str) -> AgentResponse:
    """One chat turn with the same admission, per-session locking, budgets and
    persistence as POST /chat. Also the entry point for the MCP server.

    `identity` decides rate limits, sign-in gates and credits: a signed-in user
    or API key is charged from its ledger, an anonymous visitor from the trial,
    and a service caller (a plain string, e.g. the MCP server without a user
    key) is never charged."""
    if isinstance(identity, str):
        identity = current_identity.get() or service_identity(identity)
    session_id = body.session_id or str(uuid4())
    if body.wallet_address and not identity.signed_in and identity.kind != "service":
        raise HTTPException(401, {"error": "sign_in_required", "message": "Sign in with your email to use a wallet, trade, or keep history."})
    if identity.api_key is not None and not identity.has_scope("chat"):
        raise HTTPException(403, "This API key does not have the chat scope")
    turn_id = str(uuid4())
    reserved = 0
    if identity.kind != "service":
        try:
            reserved = await credits.reserve(identity.account_id, turn_id)
        except credits.InsufficientCredits as exc:
            increment("chat_insufficient_credits")
            raise HTTPException(402, {
                "error": "insufficient_credits", "balance": exc.balance, "required": exc.required,
                "plan": identity.plan.id, "signed_in": identity.signed_in,
                "message": ("You've used your trial credits. Sign in to get 100 free credits every month."
                            if not identity.signed_in else "You're out of credits for this month. Upgrade or buy a credit pack."),
            })
    try:
        response = await _execute_chat_turn(body, identity, session_id)
    except BaseException:
        if reserved:
            await credits.release(identity.account_id, turn_id, reserved)
        raise
    if reserved:
        cost, kind = credits.turn_cost(response.intent, response.trajectory, response.team_report)
        charged = await credits.settle(identity.account_id, turn_id, reserved, cost, kind)
        response.credits = {"charged": charged, "kind": kind, "balance": await credits.balance(identity.account_id)}
    return response


async def _execute_chat_turn(body: ChatRequest, identity: Identity, session_id: str) -> AgentResponse:
    allowed, retry_after = await allow_chat_request(identity.rate_limit_key)
    if not allowed:
        increment("chat_rate_limited")
        raise HTTPException(
            429,
            "Too many chat requests; please retry shortly",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        await acquire_chat_slot()
    except asyncio.TimeoutError as exc:
        increment("chat_queue_timeouts")
        raise HTTPException(503, "Chat service is busy; please retry shortly") from exc
    increment("chat_requests")
    session_lease = None
    try:
        try:
            session_lease = await acquire_session_turn(session_id)
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                409,
                "Another request is already updating this chat. Wait for it to finish and retry.",
            ) from exc
        messages, session_context = await get_session_snapshot(session_id)
        history = history_text_from_messages(messages)
        current_revision = int(session_context.get("revision", 0))
        if body.context_revision is not None and body.context_revision != current_revision:
            raise HTTPException(
                409,
                "This chat changed in another tab or request. Refresh the conversation before continuing.",
            )
        action = body.quick_action.model_dump() if body.quick_action else None
        # A quick action is authoritative only for the exact context revision
        # that produced it. Never reinterpret a stale action as free text.
        if action and (
            action["context_revision"] != current_revision
            or action["prompt"] != body.message
        ):
            raise HTTPException(
                409,
                "This quick action belongs to an older response. Use an action from the latest response.",
            )
        if action:
            latest_actions = next(
                (
                    item.get("quick_actions") or []
                    for item in reversed(messages)
                    if item.get("role") == "assistant"
                    and int(item.get("session_revision") or -1) == current_revision
                ),
                [],
            )
            authorized = next(
                (item for item in latest_actions if item.get("id") == action["id"]),
                None,
            )
            if authorized is None or body.quick_action.model_dump() != authorized:
                raise HTTPException(
                    409,
                    "This quick action is not authorized by the latest response. Refresh the conversation.",
                )
            # Route only with the server-persisted command, never client claims.
            action = authorized
        # Invalidate the old approval before potentially slow inference. A new
        # turn must not leave its previous quote executable during generation.
        active_plan_id = (session_context.get("active_workflow") or {}).get("plan_id")
        if not active_plan_id and session_context.get("active_workflow"):
            active_plan_id = next((
                (item.get("trade_plan") or {}).get("plan_id")
                for item in reversed(messages) if item.get("trade_plan")
            ), None)
        if active_plan_id and not is_trade_confirmation(body.message):
            await mark_plan_superseded(active_plan_id)
        if body.team_mode is not None:
            session_context = {**session_context, "team_mode": body.team_mode}
        charter_fields = body.risk_charter_fields
        if charter_fields is not None:
            if charter_fields.is_empty():
                raise HTTPException(400, "Choose at least one rule for the risk charter.")
            if charter_fields.max_trade_usd is not None and charter_fields.max_trade_usd > settings.max_trade_usd:
                raise HTTPException(400, f"Max per trade cannot exceed the built-in cap of ${settings.max_trade_usd:,.2f}.")
            if charter_fields.max_slippage_bps is not None and charter_fields.max_slippage_bps > settings.max_slippage_bps:
                raise HTTPException(400, f"Max slippage cannot exceed the built-in cap of {settings.max_slippage_bps} bps.")
            # The canonical text is what the chat phrase path would have stored,
            # so the transcript, the chip and the Risk agent all see one rendering.
            body.message = f"set my risk charter: {charter_fields.render()}"
        run = await asyncio.wait_for(
            run_agent(
                body.message,
                body.wallet_address or "",
                history,
                session_context,
                action,
            ),
            timeout=settings.chat_execution_timeout_seconds,
        )
        increment(f"intent_{run.intent}")
        answer, trajectory, plan = run
        client_trajectory = trajectory if settings.expose_tool_trajectory else public_activity(trajectory)
        # Quick-action / suggestion chips are disabled: they were often generic
        # and unrelated to the query. Empty lists render nothing (the UI only
        # shows the "Quick next actions" section when quick_actions is non-empty).
        suggestions: list[str] = []
        intent_lock = build_intent_lock(plan, run.cross_chain_swap, body.wallet_address)
        context_capsules = with_resolved_token(
            build_context_capsules(
                body.message, history, answer, plan, run.cross_chain_swap, body.wallet_address
            ),
            run.resolved_token,
        )
        evidence = build_evidence_summary(trajectory)
        # Step-7 answer validation: provenance / freshness / grounding of the
        # surfaced answer against the tool evidence. Advisory only -- attached for
        # the client and monitoring, never blocks or rewrites the answer.
        validation = validate_answer(body.message, answer, trajectory, run.intent)
        if validation is not None and validation.status == "warn":
            increment("answer_validation_warn")
        # Close the loop: credit or debit every tool that ran, so the router's
        # ranking learns from what the answer could actually use.
        try:
            await tool_outcomes.record_turn(trajectory, validation)
        except Exception:
            logger.warning("tool outcome recording failed", exc_info=True)
        trade_readiness = build_trade_readiness(plan)
        gas_advisory = build_gas_advisory(run.cross_chain_swap)
        next_context = advance_session_context(
            session_context,
            body.message,
            body.wallet_address,
            run.intent,
            run.capabilities,
            context_capsules,
            intent_lock,
            run.cross_chain_swap,
        )
        # A pending token disambiguation lives exactly one turn: set when this
        # turn asked which chain a symbol is on, otherwise cleared (the follow-up
        # consumes it before the graph runs -- see resolve_pending_token).
        next_context["pending_token"] = run.pending_token
        next_context["pending_wallet_request"] = run.pending_wallet_request
        if charter_fields is not None:
            next_context["risk_charter_fields"] = charter_fields.model_dump()
        if next_context.get("active_workflow") and plan:
            next_context["active_workflow"]["plan_id"] = plan.plan_id
        old_workflow = WorkflowState.from_context(session_context.get("active_workflow"))
        new_workflow = WorkflowState.from_context(next_context.get("active_workflow"))
        event = WorkflowEvent.TOPIC_CHANGE if new_workflow is None else WorkflowEvent.REPLACE
        next_workflow, superseded_plan = apply_event(old_workflow, event, new_workflow)
        if superseded_plan:
            await mark_plan_superseded(superseded_plan)
        next_context["active_workflow"] = next_workflow.as_context() if next_workflow else None
        quick_actions: list = []
        assistant_metadata = {
            "routing_decision": getattr(run, "routing_decision", None),
            "trajectory": client_trajectory,
            "trade_plan": plan.model_dump(mode="json") if plan else None,
            "suggestions": suggestions,
            "quick_actions": [item.model_dump() for item in quick_actions],
            "session_revision": next_context["revision"],
            "intent": run.intent,
            "capabilities": run.capabilities,
            "cross_chain_swap": run.cross_chain_swap.model_dump() if run.cross_chain_swap else None,
            "context_capsules": [item.model_dump() for item in context_capsules],
            "intent_lock": intent_lock.model_dump() if intent_lock else None,
            "evidence": evidence.model_dump(mode="json") if evidence else None,
            "trade_readiness": trade_readiness.model_dump() if trade_readiness else None,
            "gas_advisory": gas_advisory.model_dump() if gas_advisory else None,
            "risk_assessment": run.risk_assessment.model_dump() if run.risk_assessment else None,
            "team_report": run.team_report,
            "validation": validation.model_dump() if validation else None,
        }
        await commit_turn(
            session_id,
            body.message,
            answer,
            assistant_metadata,
            next_context,
        )
        return AgentResponse(
            answer=answer,
            trade_plan=plan,
            trajectory=client_trajectory,
            session_id=session_id,
            suggestions=suggestions,
            quick_actions=quick_actions,
            session_revision=next_context["revision"],
            intent=run.intent,
            capabilities=run.capabilities,
            cross_chain_swap=run.cross_chain_swap,
            context_capsules=context_capsules,
            intent_lock=intent_lock,
            evidence=evidence,
            trade_readiness=trade_readiness,
            gas_advisory=gas_advisory,
            risk_assessment=run.risk_assessment,
            team_report=run.team_report,
            validation=validation,
            team_mode=bool(next_context.get("team_mode")),
            risk_charter=next_context.get("risk_charter") or None,
            risk_charter_fields=next_context.get("risk_charter_fields") or None,
        )
    except asyncio.TimeoutError as exc:
        increment("chat_timeouts")
        raise HTTPException(504, "The request took too long; please retry") from exc
    except ValueError as exc:
        increment("chat_errors")
        raise HTTPException(400, _safe_detail(exc, "The request could not be completed")) from exc
    except HTTPException:
        raise
    except Exception as exc:
        increment("chat_errors")
        error_id = str(uuid4())
        logger.exception("Chat request failed [%s]", error_id)
        raise HTTPException(500, f"Chat request failed (reference {error_id})") from exc
    finally:
        if session_lease is not None:
            await session_lease.release()
        release_chat_slot()


# ----------------------------------------------------------------------------
# Accounts, credits, API keys
# ----------------------------------------------------------------------------

def _set_user_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        accounts.USER_COOKIE, token, max_age=accounts.USER_SESSION_TTL, httponly=True,
        secure=request.url.scheme == "https", samesite="lax", path="/",
    )


async def _me_payload(identity: Identity) -> dict:
    balance = await credits.balance(identity.account_id)
    if not identity.signed_in:
        return {"authenticated": False, "plan": identity.plan.public(), "credits": {"balance": balance}}
    user = identity.user
    return {
        "authenticated": True,
        "user": {
            "id": user["id"], "email": user["email"], "display_name": user.get("display_name"),
            "created_at": user.get("created_at"), "preferences": user.get("preferences") or {},
        },
        "plan": identity.plan.public(),
        "credits": {"balance": balance},
        "wallets": await accounts.list_wallets(user["id"]),
        "api_key": {"id": identity.api_key["id"], "name": identity.api_key["name"]} if identity.api_key else None,
        "billing": {
            "configured": billing.configured(),
            "has_billing_account": bool(user.get("stripe_customer_id")),
            "subscription_status": user.get("subscription_status"),
        },
    }


@app.post("/auth/email/start")
async def email_signin_start(body: EmailSigninStart, request: Request):
    """Email a one-time sign-in link. Always answers the same way so an address
    can't be probed; the dev link is only present without an email provider."""
    base = settings.public_base_url or f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    try:
        result = await accounts.start_email_signin(body.email, base)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@app.post("/auth/email/verify")
async def email_signin_verify(body: EmailSigninVerify, response: Response, request: Request):
    try:
        user, token, created = await accounts.sign_in_with_token(body.token)
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    _set_user_cookie(response, request, token)
    identity = await resolve_identity_for_user(user, request)
    return {**await _me_payload(identity), "created": created}


async def resolve_identity_for_user(user: dict, request: Request) -> Identity:
    from app.identity import _identity_for_user, client_ip
    return await _identity_for_user(user, client_ip(request))


@app.post("/auth/signout")
async def signout(request: Request, response: Response):
    await accounts.delete_user_session(request.cookies.get(accounts.USER_COOKIE))
    await delete_auth_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(accounts.USER_COOKIE, path="/")
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"authenticated": False}


@app.get("/me")
async def me(request: Request):
    return await _me_payload(await resolve_identity(request))


@app.get("/me/credits")
async def my_credits(identity: Identity = Depends(require_user)):
    return {
        "balance": await credits.balance(identity.account_id),
        "plan": identity.plan.public(),
        "ledger": await credits.history(identity.account_id),
        "costs": {
            "chat": settings.credit_cost_chat_turn, "tool_turn": settings.credit_cost_tool_turn,
            "deep_dive": settings.credit_cost_deep_dive, "trade": settings.credit_cost_trade_turn,
            "team_desk": settings.credit_cost_team_turn,
        },
    }


@app.put("/me/preferences")
async def update_preferences(body: PreferencesUpdate, identity: Identity = Depends(require_user)):
    fields: dict = {}
    if body.display_name is not None:
        fields["display_name"] = body.display_name.strip()[:60] or None
    prefs = body.model_dump(exclude_none=True, exclude={"display_name"})
    if prefs:
        fields["preferences"] = prefs
    user = await accounts.update_user(identity.user["id"], **fields)
    identity.user = user
    return await _me_payload(identity)


@app.get("/me/api-keys")
async def list_api_keys(identity: Identity = Depends(require_user)):
    return {"keys": await api_keys.list_for_user(identity.user["id"]), "allowed": identity.plan.api_keys}


@app.post("/me/api-keys", status_code=201)
async def create_api_key(body: ApiKeyCreate, identity: Identity = Depends(require_user)):
    if not identity.plan.api_keys:
        raise HTTPException(403, {"error": "upgrade_required", "message": "API keys are available on Pro and Max plans."})
    if identity.api_key is not None:
        raise HTTPException(403, "Keys can only be managed from a signed-in browser session")
    try:
        record, secret = await api_keys.create(identity.user["id"], body.name, body.scopes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"key": record, "secret": secret}


@app.delete("/me/api-keys/{key_id}")
async def revoke_api_key(key_id: str, identity: Identity = Depends(require_user)):
    if identity.api_key is not None:
        raise HTTPException(403, "Keys can only be managed from a signed-in browser session")
    if not await api_keys.revoke(identity.user["id"], key_id):
        raise HTTPException(404, "API key not found")
    return {"revoked": True}


@app.put("/admin/users/{email}/plan")
async def admin_set_plan(email: str, body: AdminPlanUpdate, request: Request):
    """Back-office plan override (support, comps, and the way a plan is set
    before Stripe webhooks exist). The monthly allowance for the new plan is
    granted on the user's next request."""
    _require_admin(request)
    if body.plan_id not in billing_plans.PLANS or body.plan_id == "anonymous":
        raise HTTPException(400, f"Unknown plan {body.plan_id!r}")
    user = await accounts.get_user_by_email(email)
    if user is None:
        raise HTTPException(404, "No account with that email")
    user = await accounts.update_user(user["id"], plan_id=body.plan_id)
    return {"email": user["email"], "plan_id": user["plan_id"]}


@app.post("/admin/users/{email}/credits")
async def admin_grant_credits(email: str, body: AdminCreditGrant, request: Request):
    """Grant (positive) or claw back (negative) credits with an audit reason.
    `reference` makes the grant idempotent -- repeating it changes nothing."""
    _require_admin(request)
    user = await accounts.get_user_by_email(email)
    if user is None:
        raise HTTPException(404, "No account with that email")
    account_id = credits.user_account_id(user["id"])
    reference = body.reference or str(uuid4())
    applied = await credits.append(account_id, body.amount, f"admin:{body.reason}", "admin", reference, {"by": "admin"})
    return {"email": user["email"], "applied": applied, "balance": await credits.balance(account_id), "reference": reference}


@app.get("/billing/plans")
async def billing_catalog():
    return {"plans": billing_plans.catalog(), "trial_credits": billing_plans.ANONYMOUS.trial_credits, **billing.public_config()}


def _public_base(request: Request) -> str:
    return settings.public_base_url or f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


@app.post("/billing/checkout")
async def billing_checkout(body: CheckoutRequest, request: Request, identity: Identity = Depends(require_user)):
    """A Stripe-hosted Checkout URL. Orbit never handles the card or wallet."""
    if identity.api_key is not None:
        raise HTTPException(403, "Billing is managed from a signed-in browser session")
    try:
        return await billing.create_checkout(identity.user, body.kind, body.item_id, _public_base(request))
    except billing.BillingNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/billing/portal")
async def billing_portal(request: Request, identity: Identity = Depends(require_user)):
    if identity.api_key is not None:
        raise HTTPException(403, "Billing is managed from a signed-in browser session")
    try:
        return {"url": await billing.create_portal(identity.user, _public_base(request))}
    except billing.BillingNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/billing/webhook")
async def billing_webhook(request: Request):
    """Stripe -> Orbit. Verified with the signing secret, recorded by event id,
    applied idempotently; always 200 once verified so Stripe stops retrying."""
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = billing._api_construct_event(payload, signature)
    except billing.BillingNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except billing.WebhookError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        result = await billing.handle_event(event)
    except Exception as exc:
        # A handler bug must surface as a retryable failure, not a silent 200.
        raise HTTPException(500, "Webhook handling failed") from exc
    increment(f"stripe_{result.get('status', 'unknown')}")
    return {"received": True, **result}


@app.get("/chat/risk-charter/limits")
async def risk_charter_limits():
    """The built-in caps a charter can only tighten, and the chains it may name."""
    return {
        "max_trade_usd": settings.max_trade_usd,
        "max_slippage_bps": settings.max_slippage_bps,
        "max_price_impact_pct": settings.max_price_impact_pct,
        "chains": list(TRADING_CHAINS),
    }


@app.get("/chat/history/{session_id}")
async def chat_history(session_id: str, identity: Identity = Depends(require_user)):
    messages = await get_messages(session_id)
    if not settings.expose_tool_trajectory:
        messages = [dict(item, trajectory=public_activity(item.get("trajectory"))) for item in messages]
    return {
        "messages": messages,
        "context": await get_session_context(session_id),
    }


@app.delete("/chat/history/{session_id}")
async def clear_chat_history(session_id: str, identity: Identity = Depends(require_user)):
    try:
        lease = await acquire_session_turn(session_id)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            409, "This chat is still processing a request. Wait for it to finish before deleting it."
        ) from exc
    try:
        await clear_history(session_id)
        return {"status": "cleared"}
    finally:
        await lease.release()


@app.get("/trade-plans/{plan_id}")
async def trade_plan(plan_id: str):
    try:
        return await get_plan(plan_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/trade-plans/{plan_id}/confirm")
async def confirm(plan_id: str, body: ConfirmRequest):
    try:
        return await execute_confirmed_plan(plan_id, body.confirmation_text)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, _safe_detail(exc, "Trade request could not be completed")) from exc


@app.post("/trade-plans/{plan_id}/wallet-transaction")
async def wallet_transaction(plan_id: str, body: ConfirmRequest):
    try:
        return await prepare_wallet_transaction(plan_id, body.confirmation_text)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, _safe_detail(exc, "Trade request could not be completed")) from exc


@app.post("/trade-plans/{plan_id}/submit-wallet-transaction")
async def submit_signed_wallet_transaction(plan_id: str, body: SignedTransactionRequest):
    try:
        return await submit_wallet_transaction(
            plan_id, body.confirmation_text, body.signed_transaction
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, _safe_detail(exc, "Trade request could not be completed")) from exc


@app.get("/executions/solana/{signature}")
async def solana_execution_status(signature: str):
    """Execution-concierge status for a previously submitted Solana transaction."""
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{64,96}", signature):
        raise HTTPException(400, "Invalid Solana transaction signature")
    try:
        payload = await rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
    except Exception as exc:
        raise HTTPException(502, _safe_detail(exc, "Solana status provider is unavailable")) from exc
    value = (payload or {}).get("value", [None])[0] if isinstance(payload, dict) else None
    if value is None:
        return {"signature": signature, "status": "not_found", "finalized": False}
    error = value.get("err")
    confirmation = value.get("confirmationStatus") or "processed"
    return {
        "signature": signature,
        "status": "failed" if error else confirmation,
        "finalized": not error and confirmation == "finalized",
        "confirmations": value.get("confirmations"),
        "slot": value.get("slot"),
        "error": error,
    }


@app.post("/executions/relay/{request_id}")
async def track_relay_execution(request_id: str, request: Request):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id):
        raise HTTPException(400, "Invalid Relay request identifier")
    allowed, retry = await allow_chat_request(_client_identity(request))
    if not allowed:
        raise HTTPException(429, "Execution tracking rate limit", headers={"Retry-After": str(retry)})
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "Invalid execution context") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "Invalid execution context")
    session_id, revision = body.get("session_id"), body.get("revision")
    if session_id is not None:
        if not isinstance(session_id, str) or len(session_id) > 128 or not isinstance(revision, int) or revision < 0:
            raise HTTPException(400, "Invalid execution context")
        lease = await acquire_session_turn(session_id)
        try:
            context = await get_session_context(session_id)
            if context.get("revision") != revision or not context.get("active_workflow"):
                raise HTTPException(409, "This trade belongs to an earlier request")
            return await relay_tracking.track(request_id, session_id, revision)
        finally:
            await lease.release()
    return await relay_tracking.track(request_id)


@app.get("/executions/relay/session/{session_id}/{revision}")
async def relay_execution_for_turn(session_id: str, revision: int):
    return await relay_tracking.for_turn(session_id, revision)


@app.get("/executions/relay/{request_id}")
async def relay_execution_status(request_id: str):
    status = await relay_tracking.get_status(request_id)
    if status is None:
        raise HTTPException(404, "Unknown Relay execution")
    return status

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
from fastapi import FastAPI, HTTPException, Request
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
from app.models import (
    AgentResponse,
    ChatRequest,
    ConfirmRequest,
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
    try:
        yield
    finally:
        reconciliation.cancel()
        relay_reconciliation.cancel()
        await asyncio.gather(reconciliation, relay_reconciliation, discovery, return_exceptions=True)
        await asyncio.to_thread(close_mcp_gateway)


app = FastAPI(title="Orbit Web3 Copilot", version="0.3.0", lifespan=lifespan)


@app.middleware("http")
async def auth_admission(request: Request, call_next):
    if request.url.path.startswith("/auth/"):
        allowed, retry = await allow_auth_request(request.client.host if request.client else "anonymous")
        if not allowed:
            return JSONResponse({"detail": "Too many authentication requests"}, status_code=429, headers={"Retry-After": str(retry)})
    return await call_next(request)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

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
    body: WalletAuthVerifyRequest, response: Response, request: Request
):
    try:
        token, session = await verify_challenge(body.address, body.nonce, body.signature)
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
async def portfolio(wallet_address: str):
    try:
        return await build_portfolio_snapshot(wallet_address)
    except ValueError as exc:
        raise HTTPException(400, _safe_detail(exc, "Invalid wallet request")) from exc
    except Exception as exc:
        error_id = str(uuid4())
        logger.exception("Portfolio request failed [%s]", error_id)
        raise HTTPException(502, f"Portfolio provider is unavailable (reference {error_id})") from exc


@app.get("/wallet-health/{wallet_address}")
async def wallet_health_report(wallet_address: str):
    try:
        return wallet_health(await build_portfolio_snapshot(wallet_address))
    except ValueError as exc:
        raise HTTPException(400, _safe_detail(exc, "Invalid wallet request")) from exc
    except Exception as exc:
        raise HTTPException(502, _safe_detail(exc, "Wallet health data is unavailable")) from exc


@app.post("/portfolio/{wallet_address}/scenario")
async def portfolio_scenario_report(wallet_address: str, body: PortfolioScenarioRequest):
    try:
        snapshot = await build_portfolio_snapshot(wallet_address)
        return portfolio_scenario(snapshot, body.change_pct, body.symbol)
    except ValueError as exc:
        raise HTTPException(400, _safe_detail(exc, "Invalid scenario")) from exc
    except Exception as exc:
        raise HTTPException(502, _safe_detail(exc, "Portfolio scenario data is unavailable")) from exc


@app.post("/chat", response_model=AgentResponse)
async def chat(body: ChatRequest, request: Request):
    session_id = body.session_id or str(uuid4())
    identity = _client_identity(request)
    allowed, retry_after = await allow_chat_request(identity)
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
        context_capsules = build_context_capsules(
            body.message, history, answer, plan, run.cross_chain_swap, body.wallet_address
        )
        evidence = build_evidence_summary(trajectory)
        # Step-7 answer validation: provenance / freshness / grounding of the
        # surfaced answer against the tool evidence. Advisory only -- attached for
        # the client and monitoring, never blocks or rewrites the answer.
        validation = validate_answer(body.message, answer, trajectory, run.intent)
        if validation is not None and validation.status == "warn":
            increment("answer_validation_warn")
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


@app.get("/chat/history/{session_id}")
async def chat_history(session_id: str):
    messages = await get_messages(session_id)
    if not settings.expose_tool_trajectory:
        messages = [dict(item, trajectory=public_activity(item.get("trajectory"))) for item in messages]
    return {
        "messages": messages,
        "context": await get_session_context(session_id),
    }


@app.delete("/chat/history/{session_id}")
async def clear_chat_history(session_id: str):
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

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
from datetime import datetime, timezone
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
from app import accounts, api_keys, billing, billing_plans, credits, emailer, event_calendar, feedback, home_highlights, mcp_server, notifications, tasks, tasks_nl, tool_outcomes, x402_gate
from app.knowledge import ingest as kb_ingest, registry as kb_registry, retrieval as kb_retrieval
from app.knowledge import store as kb_store, tool as kb_tool
from app.identity import Identity, current_identity, require_user, resolve_identity, service_identity
from app.models import TRADING_CHAINS, RiskCharterFields
from app.models import (
    AdminCreditGrant,
    AdminPlanUpdate,
    AgentResponse,
    ApiKeyCreate,
    CheckoutRequest,
    DeleteAccountRequest,
    FeedbackRequest,
    InboxRead,
    TaskCreate,
    TaskUpdate,
    TeamAccept,
    TeamInvite,
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
from app.jupiter import jupiter as _jupiter_client


async def jupiter_search_tokens(query: str) -> list[dict]:
    return await _jupiter_client.search_tokens(query)


logger = logging.getLogger(__name__)


async def _warm_knowledge() -> None:
    """Keep the resolver snapshot the router reads warm (it is I/O-free by
    design, so it has to be refreshed here on the main loop)."""
    while True:
        try:
            await kb_tool.resolver(force=True)
        except Exception:
            logger.warning("knowledge: resolver refresh failed", exc_info=True)
        await asyncio.sleep(120)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    discovery = asyncio.create_task(asyncio.to_thread(discover_mcp_tools))
    reconciliation = asyncio.create_task(reconciliation_worker())
    relay_reconciliation = asyncio.create_task(relay_tracking.worker())
    outcomes_refresh = asyncio.create_task(tool_outcomes.refresh_worker())
    task_worker = asyncio.create_task(tasks.worker())
    kb_tool.set_loop(asyncio.get_running_loop())
    kb_warm = asyncio.create_task(_warm_knowledge())
    kb_worker = asyncio.create_task(kb_ingest.worker())
    try:
        async with mcp_server.mcp.session_manager.run():
            yield
    finally:
        reconciliation.cancel()
        relay_reconciliation.cancel()
        outcomes_refresh.cancel()
        task_worker.cancel()
        kb_worker.cancel()
        kb_warm.cancel()
        kb_tool.set_loop(None)
        await asyncio.gather(reconciliation, relay_reconciliation, outcomes_refresh, task_worker, kb_worker, kb_warm, discovery, return_exceptions=True)
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


@app.middleware("http")
async def ui_revalidation(request: Request, call_next):
    # The UI is a handful of hand-edited files that change with every deploy.
    # Without this header browsers cache them heuristically and keep showing a
    # stale page (seen live: the admin page rendered its previous design for a
    # day). no-cache + the ETag StaticFiles already sends = one cheap 304 per load.
    response = await call_next(request)
    if request.url.path.startswith("/ui") and response.status_code == 200:
        response.headers["Cache-Control"] = "no-cache"
    return response


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


@app.get("/home/highlights")
async def home_highlights_cards():
    """Today's crypto and stock-market highlights for the welcome tiles.
    Cached server-side; anonymous, since the welcome screen is."""
    return await asyncio.to_thread(home_highlights.get_highlights)


@app.get("/calendar")
async def market_calendar(days: int = 7):
    """Scheduled market-moving events for the next `days` days (macro,
    earnings, crypto catalysts, unlocks). Cached server-side; public."""
    return await asyncio.to_thread(event_calendar.get_calendar, max(1, min(days, 30)))


@app.get("/home/suggestions")
async def home_suggestions(request: Request):
    """Category rows for the home chips. "My wallet" appears for a signed-in
    user with a linked or default wallet, built from their largest holdings."""
    identity = await resolve_identity(request)
    holdings: list[str] = []
    if identity.signed_in:
        wallet = ((identity.user.get("preferences") or {}).get("default_wallet")) or next(
            (w["address"] for w in await accounts.list_wallets(identity.user["id"]) if not w["address"].startswith("0x")), None,
        )
        if wallet and not str(wallet).startswith("0x"):
            try:
                snapshot = await asyncio.wait_for(build_portfolio_snapshot(wallet), timeout=8)
                holdings = ["SOL"] + [h.get("symbol") for h in snapshot.get("holdings", []) if h.get("usd_value")]
            except Exception:
                holdings = ["SOL"]
    return {"categories": await asyncio.to_thread(home_highlights.suggestions, holdings or None)}


_TOKEN_MAJORS = [("BTC", "Bitcoin"), ("ETH", "Ethereum"), ("SOL", "Solana"), ("BNB", "BNB"), ("XRP", "XRP"), ("DOGE", "Dogecoin"), ("ADA", "Cardano"), ("AVAX", "Avalanche"), ("LINK", "Chainlink"), ("SUI", "Sui"), ("TON", "Toncoin")]


@app.get("/tokens/search")
async def token_search(q: str = ""):
    """$TICKER autocomplete for the composer: majors plus Jupiter's verified
    registry, verified first. Public and cheap; no keys involved."""
    query = re.sub(r"[^A-Za-z0-9]", "", q or "")[:12].upper()
    if len(query) < 1:
        return {"tokens": []}
    out = [{"symbol": s, "name": n, "mint": None, "verified": True, "chain": "multi"} for s, n in _TOKEN_MAJORS if s.startswith(query)]
    try:
        matches = await asyncio.wait_for(jupiter_search_tokens(query), timeout=4)
    except Exception:
        matches = []
    seen = {t["symbol"] for t in out}
    ranked = sorted(matches or [], key=lambda m: (0 if "verified" in (m.get("tags") or []) else 1, 0 if str(m.get("symbol") or "").upper().startswith(query) else 1, -(m.get("organicScore") or 0)))
    for item in ranked:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol or symbol in seen or not (symbol.startswith(query) or query in str(item.get("name") or "").upper()):
            continue
        seen.add(symbol)
        out.append({"symbol": symbol, "name": item.get("name"), "mint": item.get("id"), "verified": "verified" in (item.get("tags") or []),
                    "chain": "solana", "price": item.get("usdPrice")})
        if len(out) >= 8:
            break
    return {"tokens": out[:8]}


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
        charged = await credits.settle(
            identity.account_id, turn_id, reserved, cost, kind, api_key_id=identity.api_key["id"] if identity.api_key else None,
        )
        balance = await credits.balance(identity.account_id)
        response.credits = {"charged": charged, "kind": kind, "balance": balance}
        if identity.signed_in:
            try:
                await notifications.maybe_low_credit_alert(identity.team_owner or identity.user, balance)
            except Exception:
                logger.warning("low-credit alert failed", exc_info=True)
    if identity.signed_in:
        # Conversations belong to the account: export, delete-all and account
        # deletion need this mapping (session ids are otherwise client-chosen).
        try:
            await accounts.touch_chat_session(identity.user["id"], response.session_id)
        except Exception:
            logger.warning("chat session ownership update failed", exc_info=True)
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
        # A signed-in user's saved risk charter is the default for every new
        # conversation (the card in any conversation updates that default).
        default_fields = ((identity.user or {}).get("preferences") or {}).get("risk_charter_fields") if identity.signed_in else None
        if default_fields and current_revision == 0 and not session_context.get("risk_charter") and charter_fields is None:
            try:
                seeded = RiskCharterFields(**default_fields)
                if not seeded.is_empty():
                    session_context = {**session_context, "risk_charter": seeded.render(), "risk_charter_fields": seeded.model_dump()}
            except (TypeError, ValueError):
                logger.warning("ignoring malformed saved risk charter for user %s", identity.user["id"])
        if charter_fields is not None and identity.signed_in and not charter_fields.is_empty():
            await accounts.update_user(identity.user["id"], preferences={"risk_charter_fields": charter_fields.model_dump()})
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
        task_reply = None
        if identity.signed_in and action is None and tasks_nl.is_task_control(body.message):
            if body.tz_offset_min is not None and ((identity.user.get("preferences") or {}).get("tz_offset_min") != body.tz_offset_min):
                identity.user = await accounts.update_user(identity.user["id"], preferences={"tz_offset_min": body.tz_offset_min}) or identity.user
            tz = body.tz_offset_min if body.tz_offset_min is not None else int((identity.user.get("preferences") or {}).get("tz_offset_min") or 0)
            task_reply = await tasks_nl.handle(body.message, identity.user, tz)
        if task_reply is not None:
            from app.graph import AgentRun
            # No trajectory: a task control is a plain (1-credit) turn, not a tool turn.
            run = AgentRun(answer=task_reply, trajectory=None, trade_plan=None, intent="general", capabilities=[])
        else:
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
        "notifications": notifications.preferences(user),
        "team": await _team_payload(identity),
    }


async def _team_payload(identity: Identity) -> dict:
    user = identity.user
    owner = identity.team_owner
    if owner is not None:
        return {"role": "member", "owner": {"id": owner["id"], "email": owner["email"], "display_name": owner.get("display_name")},
                "seats": identity.plan.seats, "members": [], "invites": []}
    members = await accounts.list_team_members(user["id"]) if identity.plan.seats > 1 else []
    return {
        "role": "owner" if identity.plan.seats > 1 else None,
        "owner": None,
        "seats": identity.plan.seats,
        "members": members,
        "invites": await accounts.pending_invites_for(user["email"]),
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
        user, token, created = await accounts.sign_in_with_token(
            body.token, ip=_client_identity(request), user_agent=request.headers.get("user-agent"),
        )
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


@app.get("/me/usage")
async def my_usage(days: int = 30, identity: Identity = Depends(require_user)):
    """Credits spent per day and per feature (and per API key) for the account
    that is billed -- the team owner's pool for a member."""
    report = await credits.usage(identity.account_id, days)
    keys = {k["id"]: k["name"] for k in await api_keys.list_for_user(identity.user["id"])}
    report["by_api_key"] = [{"id": kid, "name": keys.get(kid, "revoked key"), "charged": total} for kid, total in report["by_api_key"].items()]
    return report


@app.get("/me/invoices")
async def my_invoices(identity: Identity = Depends(require_user)):
    try:
        return {"invoices": await billing.list_invoices(identity.team_owner or identity.user)}
    except billing.BillingNotConfigured:
        return {"invoices": [], "configured": False}


@app.get("/me/sessions")
async def my_sessions(request: Request, identity: Identity = Depends(require_user)):
    return {"sessions": await accounts.list_user_sessions(identity.user["id"], request.cookies.get(accounts.USER_COOKIE))}


@app.post("/me/sessions/revoke-all")
async def revoke_my_sessions(request: Request, identity: Identity = Depends(require_user)):
    """Sign out everywhere except this browser."""
    revoked = await accounts.revoke_user_sessions(identity.user["id"], keep_token=request.cookies.get(accounts.USER_COOKIE))
    return {"revoked": revoked}


@app.get("/me/export")
async def export_my_data(identity: Identity = Depends(require_user)):
    """Everything Orbit holds about the account, as one JSON document."""
    user = identity.user
    conversations = []
    for session_id in await accounts.list_chat_sessions(user["id"]):
        messages = await get_messages(session_id)
        if messages:
            conversations.append({"session_id": session_id, "messages": [
                {k: m.get(k) for k in ("role", "content", "created_at", "intent")} for m in messages
            ]})
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": {k: user.get(k) for k in ("id", "email", "display_name", "plan_id", "created_at", "preferences")},
        "wallets": await accounts.list_wallets(user["id"]),
        "api_keys": await api_keys.list_for_user(user["id"]),
        "credits": {"balance": await credits.balance(identity.account_id), "ledger": await credits.history(identity.account_id, limit=1000)},
        "conversations": conversations,
    }


@app.delete("/me/conversations")
async def delete_my_conversations(identity: Identity = Depends(require_user)):
    session_ids = await accounts.list_chat_sessions(identity.user["id"])
    for session_id in session_ids:
        await clear_history(session_id)
    await accounts.forget_chat_sessions(identity.user["id"])
    return {"deleted": len(session_ids)}


@app.delete("/me")
async def delete_my_account(body: DeleteAccountRequest, request: Request, response: Response, identity: Identity = Depends(require_user)):
    """Delete the account: conversations, wallets, API keys, sessions and team
    links go; the credit ledger stays as an anonymous financial record."""
    user = identity.user
    if identity.api_key is not None:
        raise HTTPException(403, "Delete the account from a signed-in browser session")
    if body.confirm_email.strip().lower() != user["email"]:
        raise HTTPException(400, "Type your account email exactly to confirm deletion")
    for session_id in await accounts.list_chat_sessions(user["id"]):
        await clear_history(session_id)
    for key in await api_keys.list_for_user(user["id"]):
        await api_keys.revoke(user["id"], key["id"])
    await accounts.revoke_user_sessions(user["id"])
    await accounts.delete_user(user["id"])
    await delete_auth_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(accounts.USER_COOKIE, path="/")
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"deleted": True}


# ---- Team members (plans with seats) ----

def _require_team_owner(identity: Identity) -> None:
    if identity.team_owner is not None:
        raise HTTPException(403, "Only the team owner manages members")
    if identity.plan.seats <= 1:
        raise HTTPException(403, {"error": "upgrade_required", "message": f"Team members are included in the {billing_plans.MAX.name} plan."})


@app.get("/me/team")
async def my_team(identity: Identity = Depends(require_user)):
    return await _team_payload(identity)


@app.post("/me/team/invites", status_code=201)
async def invite_team_member(body: TeamInvite, request: Request, identity: Identity = Depends(require_user)):
    _require_team_owner(identity)
    try:
        email = accounts.normalize_email(body.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if email == identity.user["email"]:
        raise HTTPException(400, "You are already the owner of this team")
    members = await accounts.list_team_members(identity.user["id"])
    if len([m for m in members if m["email"] != email]) + 1 >= identity.plan.seats:
        raise HTTPException(400, f"All {identity.plan.seats} seats are in use (owner + {identity.plan.seats - 1} members)")
    record = await accounts.invite_team_member(identity.user["id"], email, body.role)
    owner_name = identity.user.get("display_name") or identity.user["email"]
    link = f"{_public_base(request)}/ui/?team=1"
    await emailer.send_email(
        email, f"{owner_name} invited you to their {settings.product_name} team",
        f"<p><strong>{owner_name}</strong> added you to their {settings.product_name} team ({identity.plan.name} plan, shared credits).</p>"
        f"<p><a href=\"{link}\">Sign in with this email address to accept</a>.</p>",
        text=f"{owner_name} invited you to their {settings.product_name} team. Sign in with this email at {link} to accept.",
    )
    return record


@app.delete("/me/team/members/{email}")
async def remove_team_member(email: str, identity: Identity = Depends(require_user)):
    _require_team_owner(identity)
    if not await accounts.remove_team_member(identity.user["id"], email):
        raise HTTPException(404, "No such member")
    return {"removed": True}


@app.post("/me/team/accept")
async def accept_team_invite(body: TeamAccept, identity: Identity = Depends(require_user)):
    if identity.plan.seats > 1 and await accounts.list_team_members(identity.user["id"]):
        raise HTTPException(400, "You own a team; remove your members before joining another")
    user = await accounts.accept_team_invite(identity.user, body.owner_id)
    if user is None:
        raise HTTPException(404, "No pending invite from that team for your email")
    return {"joined": True, "owner_id": body.owner_id}


@app.post("/me/team/leave")
async def leave_team(identity: Identity = Depends(require_user)):
    if identity.team_owner is None:
        raise HTTPException(400, "You are not a member of a team")
    await accounts.leave_team(identity.user)
    return {"left": True}


# ---- Admin & ops: accounts, billing, business metrics ----

async def _admin_user(email: str) -> dict:
    user = await accounts.get_user_by_email(email)
    if user is None:
        raise HTTPException(404, "No account with that email")
    return user


@app.get("/admin/users")
async def admin_list_users(request: Request, q: str = "", limit: int = 25):
    _require_admin(request)
    users = await accounts.search_users(q, limit)
    out = []
    for user in users:
        out.append({
            "id": user["id"], "email": user["email"], "display_name": user.get("display_name"), "plan_id": user.get("plan_id"),
            "subscription_status": user.get("subscription_status"), "created_at": user.get("created_at"),
            "team_owner_id": user.get("team_owner_id"), "balance": await credits.balance(credits.user_account_id(user["id"])),
        })
    return {"users": out}


@app.get("/admin/users/{email}")
async def admin_user_detail(email: str, request: Request):
    """Everything support needs on one screen: plan, balance, ledger, usage,
    keys, devices, team, and the account's recent feedback-worthy turns."""
    _require_admin(request)
    user = await _admin_user(email)
    account_id = credits.user_account_id(user["id"])
    return {
        "user": {k: user.get(k) for k in ("id", "email", "display_name", "plan_id", "subscription_status", "stripe_customer_id",
                                          "stripe_subscription_id", "team_owner_id", "created_at", "preferences")},
        "plan": billing_plans.get_plan(user.get("plan_id")).public(),
        "credits": {"balance": await credits.balance(account_id), "ledger": await credits.history(account_id, limit=100)},
        "usage": await credits.usage(account_id, 30),
        "api_keys": await api_keys.list_for_user(user["id"]),
        "sessions": await accounts.list_user_sessions(user["id"]),
        "wallets": await accounts.list_wallets(user["id"]),
        "team": {"members": await accounts.list_team_members(user["id"]), "invites": await accounts.pending_invites_for(user["email"])},
        "conversations": len(await accounts.list_chat_sessions(user["id"])),
    }


@app.post("/admin/users/{email}/api-keys/{key_id}/revoke")
async def admin_revoke_key(email: str, key_id: str, request: Request):
    _require_admin(request)
    user = await _admin_user(email)
    if not await api_keys.revoke(user["id"], key_id):
        raise HTTPException(404, "API key not found or already revoked")
    return {"revoked": True}


@app.post("/admin/users/{email}/sessions/revoke")
async def admin_revoke_sessions(email: str, request: Request):
    """Sign the account out everywhere (compromised account, support request)."""
    _require_admin(request)
    user = await _admin_user(email)
    return {"revoked": await accounts.revoke_user_sessions(user["id"])}


@app.get("/admin/billing/events")
async def admin_billing_events(request: Request, limit: int = 50):
    _require_admin(request)
    return {"events": await billing.list_events(limit), "configured": billing.configured()}


@app.post("/admin/billing/events/{event_id}/replay")
async def admin_replay_event(event_id: str, request: Request):
    _require_admin(request)
    if not re.fullmatch(r"evt_[A-Za-z0-9_]{6,64}", event_id):
        raise HTTPException(400, "Not a Stripe event id")
    try:
        return await billing.replay_event(event_id)
    except billing.BillingNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, _safe_detail(exc, "Stripe could not return that event")) from exc


@app.get("/admin/metrics/business")
async def admin_business_metrics(request: Request, days: int = 30):
    """MRR, plan mix, conversion, credits burned per feature, failed payments,
    sign-ups per day -- the numbers a founder checks every morning."""
    _require_admin(request)
    stats = await accounts.user_stats()
    by_plan = stats["by_plan"]
    paid = sum(n for plan_id, n in by_plan.items() if billing_plans.get_plan(plan_id).price_usd_month > 0)
    mrr = sum(n * billing_plans.get_plan(plan_id).price_usd_month for plan_id, n in by_plan.items())
    usage = await credits.usage_all(days)
    failed = stats["by_subscription_status"].get("past_due", 0) + stats["by_subscription_status"].get("unpaid", 0)
    return {
        "days": days,
        "users": {"total": stats["total"], "paid": paid, "free": stats["total"] - paid, "by_plan": by_plan,
                  "team_members": stats["team_members"], "conversion_pct": round(100 * paid / stats["total"], 2) if stats["total"] else 0.0},
        "mrr_usd": round(mrr, 2),
        "subscriptions": stats["by_subscription_status"],
        "failed_payments": failed,
        "signups_by_day": stats["signups_by_day"],
        "credits": usage,
        "stripe": {"configured": billing.configured(), "recent_events": await billing.list_events(10)},
        "feedback": {"up": snapshot().get("feedback_up", 0), "down": snapshot().get("feedback_down", 0), "none": snapshot().get("feedback_none", 0)},
        "tools": {"rated": [row for row in tool_outcomes.snapshot() if row["feedback_up"] or row["feedback_down"]][:20]},
    }


# ---- Tasks (reminders, alerts, briefs) and the inbox ----

@app.get("/me/tasks")
async def my_tasks(identity: Identity = Depends(require_user)):
    items = await tasks.list_tasks(identity.user["id"])
    return {"tasks": [tasks.public(t) for t in items], "limit": tasks.TASK_LIMITS.get(identity.plan.id, 3), "unread": await tasks.unread_count(identity.user["id"])}


@app.post("/me/tasks", status_code=201)
async def create_my_task(body: TaskCreate, identity: Identity = Depends(require_user)):
    try:
        task = await tasks.create_task(identity.user, body.kind, body.spec, body.schedule, body.channel, body.tz_offset_min, body.title)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc) or "Invalid task") from exc
    return tasks.public(task)


@app.patch("/me/tasks/{task_id}")
async def update_my_task(task_id: str, body: TaskUpdate, identity: Identity = Depends(require_user)):
    fields = body.model_dump(exclude_none=True)
    current = await tasks.get_task(task_id)
    if current is None or current["user_id"] != identity.user["id"]:
        raise HTTPException(404, "Task not found")
    if fields.get("status") == "active" and current["status"] != "active":
        fields["next_run_at"] = tasks.next_run(current["schedule"], current.get("tz_offset_min", 0))
    task = await tasks.update_task(task_id, identity.user["id"], **fields)
    return tasks.public(task)


@app.delete("/me/tasks/{task_id}")
async def delete_my_task(task_id: str, identity: Identity = Depends(require_user)):
    if not await tasks.delete_task(task_id, identity.user["id"]):
        raise HTTPException(404, "Task not found")
    return {"deleted": True}


@app.post("/me/tasks/{task_id}/run")
async def run_my_task_now(task_id: str, identity: Identity = Depends(require_user)):
    """Fire a task immediately (a test send for briefs and reminders)."""
    current = await tasks.get_task(task_id)
    if current is None or current["user_id"] != identity.user["id"]:
        raise HTTPException(404, "Task not found")
    return await tasks.run_task(current)


@app.get("/me/inbox")
async def my_inbox(identity: Identity = Depends(require_user)):
    return {"items": await tasks.inbox(identity.user["id"]), "unread": await tasks.unread_count(identity.user["id"])}


@app.post("/me/inbox/read")
async def read_my_inbox(body: InboxRead, identity: Identity = Depends(require_user)):
    return {"marked": await tasks.mark_read(identity.user["id"], body.ids), "unread": await tasks.unread_count(identity.user["id"])}


# ---- Knowledge service ----

@app.get("/knowledge/search")
async def knowledge_search(q: str, limit: int = 6):
    """Hybrid retrieval over the knowledge base: passages with citations."""
    if not q.strip():
        raise HTTPException(400, "q is required")
    hits, plan = await kb_retrieval.search(q, limit=max(1, min(limit, 20)), resolver=await kb_tool.resolver())
    context, citations = kb_retrieval.build_context(hits)
    return {
        "query": q,
        "entities": [{"id": r.entity.id, "name": r.entity.canonical_name, "type": r.entity.entity_type, "confidence": r.confidence, "method": r.method} for r in plan.entities],
        "graph_expanded": plan.graph_expanded,
        "hits": [{"score": round(h.score, 4), "sources": h.sources, "protocol": h.protocol_name, "title": h.document_title, "url": h.document_url, "heading": h.chunk.heading, "content": h.chunk.content[:600]} for h in hits],
        "citations": citations,
    }


@app.get("/knowledge/status")
async def knowledge_status():
    store = await kb_store.get_store()
    return {**await store.stats(), "ingestion": kb_ingest.status(), "embedder": kb_tool.get_embedder_name()}


@app.get("/admin/knowledge/protocols")
async def admin_knowledge_protocols(request: Request, limit: int = 100):
    """The registry as ingested: slugs (what the ingest endpoint takes), TVL, sources."""
    _require_admin(request)
    store = await kb_store.get_store()
    out = []
    for p in await store.list_protocols(limit=max(1, min(limit, 500))):
        out.append({"id": p.id, "slug": p.slug, "name": p.name, "symbol": p.symbol, "category": p.category, "tvl_usd": p.tvl_usd, "chains": p.chains,
                    "docs_url": kb_registry.guess_docs_url(p), "github_org": p.github_org, "defillama_slug": p.defillama_slug})
    return {"protocols": out}


@app.post("/admin/knowledge/bootstrap")
async def admin_knowledge_bootstrap(request: Request, limit: int = 50):
    """Fill the protocol registry from DefiLlama (top N by TVL) with entities and edges."""
    _require_admin(request)
    result = await kb_registry.bootstrap(limit=max(1, min(limit, 500)))
    await kb_tool.resolver(force=True)
    return result


@app.post("/admin/knowledge/ingest/{protocol_slug}")
async def admin_knowledge_ingest(protocol_slug: str, request: Request):
    """Run every applicable connector for one protocol now."""
    _require_admin(request)
    try:
        results = await kb_ingest.run_protocol(f"protocol:{protocol_slug}")
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    await kb_tool.resolver(force=True)
    return {"results": [r.__dict__ for r in results]}


@app.post("/admin/knowledge/tick")
async def admin_knowledge_tick(request: Request, limit: int = 5):
    """One ingestion pass over due (protocol, source) pairs."""
    _require_admin(request)
    results = await kb_ingest.tick(limit=max(1, min(limit, 50)))
    await kb_tool.resolver(force=True)
    return {"results": [r.__dict__ for r in results]}


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


@app.post("/chat/feedback")
async def chat_feedback(body: FeedbackRequest, request: Request):
    """Rate one assistant turn. The tools behind that turn are read from the
    stored conversation and their outcome counters move by the rating (or its
    change), which feeds ProviderRouter's ranking -- see app/tool_outcomes.py."""
    identity = await resolve_identity(request)
    try:
        result = await feedback.rate(body.session_id, body.session_revision, body.rating, body.comment, identity.account_id)
    except feedback.TurnNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    increment(f"feedback_{body.rating}")
    return result


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

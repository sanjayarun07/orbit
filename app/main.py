import asyncio
import hmac
import ipaddress
import logging
import os
import time
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
from fastapi.responses import FileResponse, Response
from fastapi.responses import JSONResponse
from app.limits import allow_auth_request
from app.public_activity import public_activity
from app.reconciliation import reconciliation_worker
from app import relay_tracking
from fastapi.staticfiles import StaticFiles

from app import deployment, execution_policy, plan_access, task_scheduling
from app.deployment import ExecutionDisabledError
from app.service_errors import ServiceError, safe_detail as _safe_detail
from app.execution import execute_confirmed_plan, prepare_wallet_transaction, submit_wallet_transaction
from app.graph import resolve_intent_node
from app.capability_router import route_capabilities
from app import limits
from app.limits import allow_chat_request, allow_rpc_request
from app.lifi import get_quote as get_lifi_quote, get_status as get_lifi_status
from app.mcp_tools import close_mcp_gateway, discover_mcp_tools, get_mcp_registry
from app.metrics import increment, snapshot
from app.wash_trading import nansen_enrich, pipeline as wash_trading_pipeline, schema as wash_trading_schema
from app import accounts, api_keys, billing, billing_plans, credits, emailer, event_calendar, feedback, home_highlights, mcp_server, notifications, research_gaps, session_access, tasks, tool_outcomes, x402_gate
from app.knowledge import ingest as kb_ingest, registry as kb_registry, retrieval as kb_retrieval
from app.knowledge import store as kb_store, tool as kb_tool
from app.identity import Identity, current_identity, require_browser_session, require_scope, require_user, resolve_identity
from app.models import TRADING_CHAINS
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
    WalletChallengeRequest,
    WalletVerifyRequest,
    WashTradingDetectionRequest,
)
from app.plans import get_plan
from app.provider_registry import get_provider_router, save_provider_overrides
from app.db import get_pg_pool, get_redis
from app.settings import settings
from app.portfolio import build_portfolio_snapshot
from app.wallet_insights import portfolio_scenario, wallet_health
from app.wallet_auth import (
    COOKIE_NAME,
    create_challenge,
    create_solana_challenge,
    delete_auth_session,
    verify_challenge,
    verify_solana_challenge,
)
from app.sessions import acquire_session_turn, clear_history, get_messages, get_session_context
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
    # Before anything starts serving: refuse to run an unsafe configuration.
    # Raising here aborts startup, which is the point -- a production instance
    # with development conveniences left on should never accept a request.
    for warning in deployment.enforce():
        logger.warning("startup configuration warning: %s", warning)
    logger.info(
        "deployment mode=%s execution_enabled=%s custodial_signing=%s environment=%s",
        deployment.deployment_mode(), deployment.execution_enabled(),
        deployment.custodial_signing_enabled(), settings.environment,
    )
    discovery = asyncio.create_task(asyncio.to_thread(discover_mcp_tools))
    reconciliation = asyncio.create_task(reconciliation_worker())
    relay_reconciliation = asyncio.create_task(relay_tracking.worker())
    outcomes_refresh = asyncio.create_task(tool_outcomes.refresh_worker())
    task_worker = asyncio.create_task(tasks.worker())
    kb_tool.set_loop(asyncio.get_running_loop())
    kb_warm = asyncio.create_task(_warm_knowledge())
    kb_worker = asyncio.create_task(kb_ingest.worker())
    _workers.update({
        "reconciliation": reconciliation, "relay_reconciliation": relay_reconciliation,
        "tool_outcomes": outcomes_refresh, "tasks": task_worker, "knowledge_ingest": kb_worker,
    })
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


@app.exception_handler(ServiceError)
async def service_error_response(request: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


@app.exception_handler(ExecutionDisabledError)
async def execution_disabled_response(request: Request, exc: ExecutionDisabledError):
    """403 with the reason intact, for every transport and every provider.

    A handler rather than a per-route except so a money-moving route added
    later is covered by the gate without anyone remembering to wrap it.
    """
    return JSONResponse(
        status_code=403,
        content={"detail": str(exc), "deployment_mode": deployment.deployment_mode()},
    )



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


@app.get("/ui/knowledge.html", include_in_schema=False)
async def knowledge_admin_page():
    """Dedicated knowledge workspace using the shared administration shell."""
    return FileResponse(STATIC_DIR / "admin.html")


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

_workers: dict[str, "asyncio.Task"] = {}


@app.get("/health")
async def health():
    """Liveness only: the process is up and serving. Deliberately cheap and
    dependency-free -- use /readyz to decide whether to send traffic."""
    return {"status": "ok", "counters": snapshot()}


async def _check(name: str, coro, required: bool) -> dict:
    started = time.monotonic()
    try:
        detail = await asyncio.wait_for(coro, timeout=settings.readiness_check_timeout_seconds)
        ok = True
    except asyncio.TimeoutError:
        detail, ok = f"timed out after {settings.readiness_check_timeout_seconds}s", False
    except Exception as exc:
        detail, ok = f"{type(exc).__name__}: {str(exc)[:160]}", False
    return {"name": name, "ok": ok, "required": required, "detail": detail,
            "latency_ms": round((time.monotonic() - started) * 1000)}


async def _check_knowledge_snapshot() -> str:
    """The router's knowledge-base anchor reads an in-memory resolver snapshot
    warmed at startup and every two minutes. When it is empty -- before the
    first warm, or after a refresh that failed and only logged -- every
    protocol question silently falls to the speech model and is answered from
    the model's memory instead of the indexed corpus. Reported as degraded, so
    that state is visible instead of silent."""
    pool = await get_pg_pool()
    if pool is None:
        return "not configured (no knowledge base without Postgres)"
    res = kb_tool.snapshot()
    count = len(res) if res is not None else 0
    if not count:
        raise RuntimeError("empty: the knowledge-base anchor is absent; protocol questions fall to the model")
    return f"{count} entities"


async def _check_postgres() -> str:
    pool = await get_pg_pool()
    if pool is None:
        return "not configured (in-memory fallback)"
    value = await pool.fetchval("SELECT 1")
    if value != 1:
        raise RuntimeError("unexpected response")
    return "query ok"


async def _check_redis() -> str:
    redis = await get_redis()
    if redis is None:
        return "not configured (in-memory fallback)"
    await redis.ping()
    return "ping ok"


async def _check_model() -> str:
    """Reachability of the answer model's provider, without spending a call:
    a missing key is the failure this catches at deploy time."""
    if not settings.openai_api_key and not settings.model.startswith(("hosted_vllm/", "openai/gpt-4.1-mini-local")):
        raise RuntimeError("no model credentials configured")
    return f"credentials present for {settings.model}"


def _worker_health() -> list[dict]:
    rows = []
    # Required workers are reported even when absent: a process whose startup
    # never registered them is not ready, and silence would hide that.
    for name in _REQUIRED_WORKERS - set(_workers):
        rows.append({"name": name, "ok": False, "required": True, "detail": "not started"})
    for name, task in _workers.items():
        if task is None:
            state, ok = "missing", False
        elif task.cancelled():
            state, ok = "cancelled", False
        elif task.done():
            exc = task.exception() if not task.cancelled() else None
            disabled_by = _DISABLED_BY_FLAG.get(name)
            if exc is None and disabled_by is not None and disabled_by():
                # It returned at once because its feature flag is off: an
                # operator's decision, not a death. Reporting it as degraded
                # made "turned off" and "died" indistinguishable on the one
                # page that exists to tell them apart.
                state, ok = f"disabled ({_DISABLED_FLAG_NAMES[name]}=false)", True
            else:
                state, ok = (f"stopped: {type(exc).__name__}" if exc else "stopped"), False
        else:
            state, ok = "running", True
        rows.append({"name": name, "ok": ok, "required": name in _REQUIRED_WORKERS, "detail": state})
    return rows


_REQUIRED_WORKERS = {"reconciliation", "relay_reconciliation", "tasks"}
# Optional workers that exit immediately when their flag is off.
_DISABLED_BY_FLAG = {"knowledge_ingest": lambda: not settings.knowledge_ingest_enabled}
_DISABLED_FLAG_NAMES = {"knowledge_ingest": "KNOWLEDGE_INGEST_ENABLED"}


@app.get("/readyz")
async def readyz(response: Response):
    """Readiness: can this instance actually serve? Checks the datastores it
    is configured for, model credentials, and that the workers that must not
    silently die (execution reconciliation, relay tracking, scheduled tasks)
    are still running. 503 when any REQUIRED check fails, so a load balancer
    or deploy gate can act on it."""
    checks = list(await asyncio.gather(
        _check("postgres", _check_postgres(), required=bool(settings.database_url)),
        _check("redis", _check_redis(), required=bool(settings.redis_url)),
        _check("model_credentials", _check_model(), required=True),
        _check("knowledge_snapshot", _check_knowledge_snapshot(), required=False),
    ))
    checks.extend(_worker_health())
    failed = [c["name"] for c in checks if c["required"] and not c["ok"]]
    degraded = [c["name"] for c in checks if not c["required"] and not c["ok"]]
    status = "ready" if not failed else "not_ready"
    if failed:
        response.status_code = 503
    return {"status": status, "failed": failed, "degraded": degraded, "checks": checks,
            "live_trading": settings.live_trading, "version": _release_version(),
            "routing_backend": settings.routing_backend,
            "environment": settings.environment,
            "deployment": deployment.public_status(),
            # Warnings only: anything fatal stopped the process at startup, so a
            # running instance can never report one here.
            "config_warnings": [
                {"code": p.code, "setting": p.setting, "message": p.message}
                for p in deployment.audit() if p.severity != deployment.FATAL
            ]}


def _release_version() -> str:
    """The commit this instance is running, for release evidence."""
    env = os.environ.get("ORBIT_RELEASE") or os.environ.get("GIT_COMMIT")
    if env:
        return env
    head = Path(__file__).resolve().parents[1] / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            ref_path = head.parent / ref.removeprefix("ref: ")
            return ref_path.read_text().strip()[:12]
        return ref[:12]
    except OSError:
        return "unknown"


_EVM_CHAIN_NAMES = {1: "ethereum", 10: "optimism", 56: "bsc", 137: "polygon", 8453: "base", 42161: "arbitrum", 43114: "avalanche"}


def _wallet_chain_label(chain: str) -> str:
    if chain == "solana":
        return "solana"
    try:
        chain_id = int(chain)
    except ValueError as exc:
        raise HTTPException(400, "Unknown chain") from exc
    return _EVM_CHAIN_NAMES.get(chain_id, f"evm-{chain_id}")


async def _finish_wallet_signin(chain: str, address: str, request: Request, response: Response,
                                wallet_type: str = accounts.EOA) -> dict:
    """After a signature is verified: a wallet already linked to an account
    always signs the caller in as that account's owner (a verified signature
    can only ever come from the real key-holder, so this never lets one
    account steal another's wallet). A new wallet either links to the
    caller's current signed-in session, or -- with no session at all --
    creates a fresh, email-less account: wallet-only sign-in, exactly like
    the email magic link, minus the email."""
    # find_wallet_owner, not get_user_by_wallet: for EVM the address is the
    # identity on every network, so switching networks in the wallet -- or
    # arriving through the Coinbase entry point, which labels the chain "evm"
    # rather than "ethereum" -- resumes the same account instead of making one.
    # An API key must never become a browser session. A restricted key that
    # could link its holder's own wallet to the key owner's account would come
    # back out as a full cookie session, with which it could mint an
    # unrestricted key -- and the wallet link would outlive the original key.
    # Wallet sign-in is a browser flow; a key has no business in it at all.
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if bearer.startswith(api_keys.KEY_PREFIX):
        raise HTTPException(403, "Wallet sign-in is a browser flow. An API key cannot link a wallet or open a session.")

    existing_owner = await accounts.find_wallet_owner(chain, address, wallet_type)
    if existing_owner is not None:
        user, created = existing_owner, False
    else:
        current = await resolve_identity(request)
        # `signed_in` is true for an API-key identity too, so the cookie-session
        # check is the one that matters here, not a convenience.
        if current.signed_in and current.api_key is None:
            await accounts.link_wallet(current.user["id"], chain, address, wallet_type)
            user, created = current.user, False
        else:
            user = await accounts.create_wallet_user(chain, address, wallet_type)
            created = True
    token = await accounts.create_user_session(user["id"], ip=_client_identity(request), user_agent=request.headers.get("user-agent"))
    _set_user_cookie(response, request, token)
    identity = await resolve_identity_for_user(user, request)
    return {**await _me_payload(identity), "created": created}


@app.post("/auth/coinbase/challenge")
async def coinbase_auth_challenge(body: WalletAuthChallengeRequest, request: Request):
    """EVM-only, fixed shape: the Coinbase Wallet SDK bundle (app/static/
    coinbase.js) calls this by name right after every connect, with no
    server-side change needed on the frontend -- keep the path and body
    shape exactly as it expects. Other wallets go through /auth/wallet/*."""
    host = request.headers.get("host", request.url.hostname or "Orbit").split("/", 1)[0]
    uri = f"{request.url.scheme}://{host}"
    try:
        return await create_challenge(body.address, host, uri, body.chain_id)
    except ValueError as exc:
        raise HTTPException(400, _safe_detail(exc, "Unsupported wallet network")) from exc


@app.post("/auth/coinbase/verify")
async def coinbase_auth_verify(body: WalletAuthVerifyRequest, response: Response, request: Request):
    try:
        _token, verified = await verify_challenge(body.address, body.nonce, body.signature)
    except (ValueError, TypeError) as exc:
        raise HTTPException(401, _safe_detail(exc, "Wallet authentication failed")) from exc
    # The chain comes from the VERIFIED challenge, never from this request.
    # "evm" used to be the label here, which collapsed every network into one
    # bucket -- harmless for an EOA, but for a contract wallet it is exactly the
    # cross-network merge that must not happen, since Coinbase Smart Wallet is
    # a contract wallet.
    chain = _wallet_chain_label(str(verified["chain_id"]))
    return await _finish_wallet_signin(chain, body.address.lower(), request, response, verified["wallet_type"])


@app.post("/auth/wallet/challenge")
async def wallet_auth_challenge(body: WalletChallengeRequest, request: Request):
    """The general pair: any wallet, EVM or Solana. No sign-in required to
    call this -- proving control of an address is itself how you sign in."""
    host = request.headers.get("host", request.url.hostname or "Orbit").split("/", 1)[0]
    uri = f"{request.url.scheme}://{host}"
    try:
        if body.chain == "solana":
            return await create_solana_challenge(body.address, host, uri)
        return await create_challenge(body.address, host, uri, int(body.chain))
    except ValueError as exc:
        raise HTTPException(400, _safe_detail(exc, "Unsupported wallet or network")) from exc


@app.post("/auth/wallet/verify")
async def wallet_auth_verify(body: WalletVerifyRequest, response: Response, request: Request):
    requested = _wallet_chain_label(body.chain)
    address = body.address if requested == "solana" else body.address.lower()
    wallet_type = accounts.EOA
    try:
        if requested == "solana":
            await verify_solana_challenge(address, body.nonce, body.signature)
            chain = "solana"
        else:
            _token, verified = await verify_challenge(address, body.nonce, body.signature)
            wallet_type = verified["wallet_type"]
            # The network an identity is scoped to must come from the challenge
            # that was actually verified, not from this request. The challenge
            # fixes which chain's validator checked a contract signature, so
            # taking the chain from the body let a Base-validated signature be
            # presented as Ethereum and resume an Ethereum-linked account.
            chain = _wallet_chain_label(str(verified["chain_id"]))
            if chain != requested:
                raise HTTPException(400, "This signature was issued for a different network. Request a fresh challenge.")
    except (ValueError, TypeError) as exc:
        raise HTTPException(401, _safe_detail(exc, "Wallet authentication failed")) from exc
    return await _finish_wallet_signin(chain, address, request, response, wallet_type)


@app.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    """The Coinbase Wallet bundle calls this (not /auth/signout) on
    disconnect or account switch; make it a real, full sign-out too."""
    await accounts.delete_user_session(request.cookies.get(accounts.USER_COOKIE))
    await delete_auth_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(accounts.USER_COOKIE, path="/")
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"authenticated": False}


def _require_execution_mode() -> None:
    """Refuse a money-moving route before it reads any state.

    A dependency rather than a check inside the handler for two reasons: it runs
    ahead of the ownership lookup, so a research deployment never touches the
    plan store just to say no; and it is visible in the route signature, so the
    authorization matrix can see which routes carry it.
    """
    deployment.require_execution_enabled("Trade execution")


@app.post("/execution/lifi/quote")
async def lifi_quote(payload: LifiQuoteRequest, _mode: None = Depends(_require_execution_mode)):
    """Create a backup quote only; signing/submission remains wallet-controlled."""
    # A LI.FI quote carries a ready-to-sign `transactionRequest`, so it is a
    # money-moving entry point in the same sense the Jupiter routes are. That
    # was the asymmetry: LIVE_TRADING gated Jupiter and left this one open.
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
        # Research mode reports every provider off, so the browser hides what
        # the server would refuse. Relay signs entirely in the browser against
        # its own API, so this is what stops it in the shipped UI -- the server
        # cannot refuse a swap it is never asked about. See docs.
        "execution_providers": {
            "jupiter": deployment.execution_enabled(),
            "relay": deployment.execution_enabled(),
            "lifi_backup": settings.lifi_enabled and deployment.execution_enabled(),
        },
        "deployment": deployment.public_status(),
        "x402": x402_gate.public_config(),
        "accounts": {
            "enabled": True,
            "product_name": settings.product_name,
            "trial_credits": billing_plans.ANONYMOUS.trial_credits,
            "email_configured": bool(settings.resend_api_key),
            # The browser hides prices and purchase controls, and says so.
            "closed_beta": settings.closed_beta,
            "closed_beta_monthly_credits": settings.closed_beta_monthly_credits if settings.closed_beta else None,
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
async def admin_providers(_admin: None = Depends(_require_admin)):
    return {"tools": get_provider_router().catalog(), "overrides": get_provider_router().overrides()}


@app.get("/admin/tools/outcomes")
async def admin_tool_outcomes(_admin: None = Depends(_require_admin)):
    """Per-tool grounded-success rates and the ranking adjustment each earns."""
    return {
        "window_days": settings.tool_outcome_window_days,
        "prior_rate": settings.tool_outcome_prior_rate,
        "prior_weight": settings.tool_outcome_prior_weight,
        "weight": settings.provider_outcome_weight,
        "tools": tool_outcomes.snapshot(),
    }


@app.put("/admin/providers/{tool_name}")
async def update_admin_provider(tool_name: str, body: ProviderPolicyUpdate, _admin: None = Depends(_require_admin)):
    try:
        get_provider_router().configure(tool_name, body.model_dump(exclude_none=True))
        save_provider_overrides()
    except KeyError as exc:
        raise HTTPException(404, f"Unknown provider tool: {tool_name}") from exc
    return next(row for row in get_provider_router().catalog() if row["name"] == tool_name)


@app.delete("/admin/providers/{tool_name}")
async def reset_admin_provider(tool_name: str, _admin: None = Depends(_require_admin)):
    get_provider_router().reset(tool_name)
    save_provider_overrides()
    return {"status": "reset", "tool": tool_name}


@app.put("/admin/provider-groups/{provider}")
async def update_admin_provider_group(provider: str, body: ProviderPolicyUpdate, _admin: None = Depends(_require_admin)):
    if body.enabled is None:
        raise HTTPException(400, "Provider group updates require enabled=true or enabled=false")
    try:
        count = get_provider_router().configure_provider(provider, body.enabled)
        save_provider_overrides()
    except KeyError as exc:
        raise HTTPException(404, f"Unknown provider: {provider}") from exc
    return {"provider": provider, "enabled": body.enabled, "updated_tools": count}


@app.post("/admin/routes/preview")
async def preview_admin_route(body: RoutePreviewRequest, request: Request, _admin: None = Depends(_require_admin)):
    return {
        "request": body.request,
        "capability": body.capability,
        "candidates": get_provider_router().preview(body.request, body.capability, tuple(body.chains)),
    }


@app.post("/admin/intents/preview")
async def preview_admin_intent(body: IntentPreviewRequest, request: Request, _admin: None = Depends(_require_admin)):
    """Dry-run deterministic classification without spending model/provider tokens."""
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
async def test_admin_provider(body: ProviderTestRequest, request: Request, _admin: None = Depends(_require_admin)):
    """Preview or explicitly invoke the read-only provider route in the admin lab."""
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
async def create_wash_trading_run(body: WashTradingDetectionRequest, _admin: None = Depends(_require_admin)):
    """Kick off a wash-trading/wallet-clustering detection run (Phase 1:
    concentration stats, round-trip detection, single-hop funding fan-out).
    Every output here is a lead for a human analyst to review, never an
    automated verdict -- see app/wash_trading/pipeline.py's module docstring.
    """
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
async def get_wash_trading_run(run_id: str, _admin: None = Depends(_require_admin)):

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
async def get_wash_trading_wallet(run_id: str, wallet: str, _admin: None = Depends(_require_admin)):

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
async def portfolio(wallet_address: str, identity: Identity = Depends(require_scope('data'))):
    try:
        return await build_portfolio_snapshot(wallet_address)
    except ValueError as exc:
        raise HTTPException(400, _safe_detail(exc, "Invalid wallet request")) from exc
    except Exception as exc:
        error_id = str(uuid4())
        logger.exception("Portfolio request failed [%s]", error_id)
        raise HTTPException(502, f"Portfolio provider is unavailable (reference {error_id})") from exc


@app.get("/wallet-health/{wallet_address}")
async def wallet_health_report(wallet_address: str, identity: Identity = Depends(require_scope('data'))):
    try:
        return wallet_health(await build_portfolio_snapshot(wallet_address))
    except ValueError as exc:
        raise HTTPException(400, _safe_detail(exc, "Invalid wallet request")) from exc
    except Exception as exc:
        raise HTTPException(502, _safe_detail(exc, "Wallet health data is unavailable")) from exc


@app.post("/portfolio/{wallet_address}/scenario")
async def portfolio_scenario_report(wallet_address: str, body: PortfolioScenarioRequest, identity: Identity = Depends(require_scope('data'))):
    try:
        snapshot = await build_portfolio_snapshot(wallet_address)
        return portfolio_scenario(snapshot, body.change_pct, body.symbol)
    except ValueError as exc:
        raise HTTPException(400, _safe_detail(exc, "Invalid scenario")) from exc
    except Exception as exc:
        raise HTTPException(502, _safe_detail(exc, "Portfolio scenario data is unavailable")) from exc


@app.post("/chat", response_model=AgentResponse)
async def chat(body: ChatRequest, request: Request):
    return await execution_policy.execute_chat_turn(body, await resolve_identity(request))


async def _require_session_access(session_id: str, identity: Identity | None, claim: bool = False) -> None:
    try:
        await session_access.require_session_access(session_id, identity, claim=claim)
    except session_access.SessionAccessDenied as exc:
        raise HTTPException(404, "Conversation not found") from exc


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
async def my_credits(identity: Identity = Depends(require_scope('data'))):
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
async def update_preferences(body: PreferencesUpdate, identity: Identity = Depends(require_browser_session)):
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
async def list_api_keys(identity: Identity = Depends(require_browser_session)):
    return {"keys": await api_keys.list_for_user(identity.user["id"]), "allowed": identity.plan.api_keys}


@app.post("/me/api-keys", status_code=201)
async def create_api_key(body: ApiKeyCreate, identity: Identity = Depends(require_browser_session)):
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
async def revoke_api_key(key_id: str, identity: Identity = Depends(require_browser_session)):
    if identity.api_key is not None:
        raise HTTPException(403, "Keys can only be managed from a signed-in browser session")
    if not await api_keys.revoke(identity.user["id"], key_id):
        raise HTTPException(404, "API key not found")
    return {"revoked": True}


@app.put("/admin/users/{email}/plan")
async def admin_set_plan(email: str, body: AdminPlanUpdate, _admin: None = Depends(_require_admin)):
    """Back-office plan override (support, comps, and the way a plan is set
    before Stripe webhooks exist). The monthly allowance for the new plan is
    granted on the user's next request."""
    if body.plan_id not in billing_plans.PLANS or body.plan_id == "anonymous":
        raise HTTPException(400, f"Unknown plan {body.plan_id!r}")
    user = await accounts.get_user_by_email(email)
    if user is None:
        raise HTTPException(404, "No account with that email")
    user = await accounts.update_user(user["id"], plan_id=body.plan_id)
    return {"email": user["email"], "plan_id": user["plan_id"]}


@app.post("/admin/users/{email}/credits")
async def admin_grant_credits(email: str, body: AdminCreditGrant, _admin: None = Depends(_require_admin)):
    """Grant (positive) or claw back (negative) credits with an audit reason.
    `reference` makes the grant idempotent -- repeating it changes nothing."""
    user = await accounts.get_user_by_email(email)
    if user is None:
        raise HTTPException(404, "No account with that email")
    account_id = credits.user_account_id(user["id"])
    reference = body.reference or str(uuid4())
    applied = await credits.append(account_id, body.amount, f"admin:{body.reason}", "admin", reference, {"by": "admin"})
    return {"email": user["email"], "applied": applied, "balance": await credits.balance(account_id), "reference": reference}


@app.get("/me/usage")
async def my_usage(days: int = 30, identity: Identity = Depends(require_browser_session)):
    """Credits spent per day and per feature (and per API key) for the account
    that is billed -- the team owner's pool for a member."""
    report = await credits.usage(identity.account_id, days)
    keys = {k["id"]: k["name"] for k in await api_keys.list_for_user(identity.user["id"])}
    report["by_api_key"] = [{"id": kid, "name": keys.get(kid, "revoked key"), "charged": total} for kid, total in report["by_api_key"].items()]
    return report


@app.get("/me/invoices")
async def my_invoices(identity: Identity = Depends(require_browser_session)):
    try:
        return {"invoices": await billing.list_invoices(identity.team_owner or identity.user)}
    except billing.BillingNotConfigured:
        return {"invoices": [], "configured": False}


@app.get("/me/sessions")
async def my_sessions(request: Request, identity: Identity = Depends(require_browser_session)):
    return {"sessions": await accounts.list_user_sessions(identity.user["id"], request.cookies.get(accounts.USER_COOKIE))}


@app.post("/me/sessions/revoke-all")
async def revoke_my_sessions(request: Request, identity: Identity = Depends(require_browser_session)):
    """Sign out everywhere except this browser."""
    revoked = await accounts.revoke_user_sessions(identity.user["id"], keep_token=request.cookies.get(accounts.USER_COOKIE))
    return {"revoked": revoked}


@app.get("/me/export")
async def export_my_data(identity: Identity = Depends(require_browser_session)):
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


@app.get("/me/conversations")
async def my_conversations(identity: Identity = Depends(require_browser_session)):
    """The signed-in account's own conversations, newest first, titled by
    their first user message -- what the sidebar shows for a signed-in user
    instead of whatever this browser happened to cache. Empty sessions are
    left out."""
    out = []
    for row in await accounts.list_chat_sessions_with_times(identity.user["id"]):
        messages = await get_messages(row["session_id"])
        if not messages:
            continue
        first_user = next((m.get("content") for m in messages if m.get("role") == "user" and m.get("content")), None)
        title = (first_user or "New chat").strip().splitlines()[0][:48]
        out.append({"session_id": row["session_id"], "title": title, "updated_at": row["last_used"], "messages": len(messages)})
    return {"conversations": out}


async def _delete_conversations_under_lease(user_id: str) -> tuple[list[str], list[str]]:
    """Delete an account's conversations: transcript AND ownership go together,
    inside the turn lease, so there is no instant at which a turn can commit
    private history to a conversation whose ownership is about to vanish. A
    turn that starts during deletion waits for the lease and then finds an
    unowned, empty conversation. Conversations busy with a turn are kept and
    reported; they remain the account's to delete again."""
    deleted, busy = [], []
    for session_id in await accounts.list_chat_sessions(user_id):
        try:
            lease = await acquire_session_turn(session_id)
        except asyncio.TimeoutError:
            busy.append(session_id)
            continue
        try:
            await clear_history(session_id)
            await accounts.forget_chat_sessions(user_id, [session_id])
            deleted.append(session_id)
        finally:
            await lease.release()
    return deleted, busy


@app.delete("/me/conversations")
async def delete_my_conversations(identity: Identity = Depends(require_browser_session)):
    """Delete every conversation the account owns -- with the same guarantee
    single deletion gives. Deleting one conversation takes its turn lease, so
    an in-flight turn cannot commit history back after the delete; this route
    used to skip the lease, remove the ownership mapping and report success
    while a running turn could still write. Now each conversation is deleted
    under its lease; ones busy with a turn are reported, kept, and remain the
    account's to delete again."""
    deleted, busy = await _delete_conversations_under_lease(identity.user["id"])
    return {"deleted": len(deleted), "busy": len(busy),
            **({"message": f"{len(busy)} conversation(s) are still processing a request and were kept; delete again once they finish."} if busy else {})}


@app.delete("/me")
async def delete_my_account(body: DeleteAccountRequest, request: Request, response: Response, identity: Identity = Depends(require_browser_session)):
    """Delete the account: conversations, wallets, API keys, sessions and team
    links go; the credit ledger stays as an anonymous financial record."""
    user = identity.user
    if identity.api_key is not None:
        raise HTTPException(403, "Delete the account from a signed-in browser session")
    # A wallet-only account has no email; accept any of its linked wallet
    # addresses as the confirmation value instead.
    confirm = body.confirm_email.strip().lower()
    accepted = {(user.get("email") or "").lower()} | {w["address"].lower() for w in await accounts.list_wallets(user["id"])}
    accepted.discard("")
    if confirm not in accepted:
        raise HTTPException(400, "Type your account email (or a linked wallet address) exactly to confirm deletion")
    # Cancel billing BEFORE the account is destroyed. Deleting first strips the
    # Stripe customer and subscription ids while the subscription keeps
    # renewing, leaving a recurring charge nobody can map back to a person.
    # A failure here is recoverable, so it stops the deletion rather than being
    # swallowed -- the user can retry, and their data is still intact.
    try:
        await billing.cancel_subscription_for(user)
    except billing.SubscriptionCancelFailed as exc:
        raise HTTPException(409, str(exc)) from exc
    _deleted, busy = await _delete_conversations_under_lease(user["id"])
    if busy:
        raise HTTPException(409, f"{len(busy)} conversation(s) are still processing a request. Wait for them to finish, then delete the account.")
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
async def my_team(identity: Identity = Depends(require_browser_session)):
    return await _team_payload(identity)


@app.post("/me/team/invites", status_code=201)
async def invite_team_member(body: TeamInvite, request: Request, identity: Identity = Depends(require_browser_session)):
    _require_team_owner(identity)
    try:
        email = accounts.normalize_email(body.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if email == identity.user["email"]:
        raise HTTPException(400, "You are already the owner of this team")
    members = await accounts.list_team_members(identity.user["id"])
    # The seat count and the insert are serialised per team inside accounts;
    # a pre-check here would be the very read-then-write the review raced.
    try:
        record = await accounts.invite_team_member_within_seats(identity.user["id"], email, identity.plan.seats, body.role)
    except accounts.SeatsExhausted as exc:
        raise HTTPException(400, str(exc)) from exc
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
async def remove_team_member(email: str, identity: Identity = Depends(require_browser_session)):
    _require_team_owner(identity)
    if not await accounts.remove_team_member(identity.user["id"], email):
        raise HTTPException(404, "No such member")
    return {"removed": True}


@app.post("/me/team/accept")
async def accept_team_invite(body: TeamAccept, identity: Identity = Depends(require_browser_session)):
    if identity.plan.seats > 1 and await accounts.list_team_members(identity.user["id"]):
        raise HTTPException(400, "You own a team; remove your members before joining another")
    user = await accounts.accept_team_invite(identity.user, body.owner_id)
    if user is None:
        raise HTTPException(404, "No pending invite from that team for your email")
    return {"joined": True, "owner_id": body.owner_id}


@app.post("/me/team/leave")
async def leave_team(identity: Identity = Depends(require_browser_session)):
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
async def admin_list_users(q: str = "", limit: int = 25, _admin: None = Depends(_require_admin)):
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
async def admin_user_detail(email: str, _admin: None = Depends(_require_admin)):
    """Everything support needs on one screen: plan, balance, ledger, usage,
    keys, devices, team, and the account's recent feedback-worthy turns."""
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
async def admin_revoke_key(email: str, key_id: str, _admin: None = Depends(_require_admin)):
    user = await _admin_user(email)
    if not await api_keys.revoke(user["id"], key_id):
        raise HTTPException(404, "API key not found or already revoked")
    return {"revoked": True}


@app.post("/admin/users/{email}/sessions/revoke")
async def admin_revoke_sessions(email: str, _admin: None = Depends(_require_admin)):
    """Sign the account out everywhere (compromised account, support request)."""
    user = await _admin_user(email)
    return {"revoked": await accounts.revoke_user_sessions(user["id"])}


@app.get("/admin/billing/events")
async def admin_billing_events(limit: int = 50, _admin: None = Depends(_require_admin)):
    return {"events": await billing.list_events(limit), "configured": billing.configured()}


@app.post("/admin/billing/events/{event_id}/replay")
async def admin_replay_event(event_id: str, _admin: None = Depends(_require_admin)):
    if not re.fullmatch(r"evt_[A-Za-z0-9_]{6,64}", event_id):
        raise HTTPException(400, "Not a Stripe event id")
    try:
        return await billing.replay_event(event_id)
    except billing.BillingNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, _safe_detail(exc, "Stripe could not return that event")) from exc


@app.get("/admin/metrics/business")
async def admin_business_metrics(days: int = 30, _admin: None = Depends(_require_admin)):
    """MRR, plan mix, conversion, credits burned per feature, failed payments,
    sign-ups per day -- the numbers a founder checks every morning."""
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
async def my_tasks(identity: Identity = Depends(require_browser_session)):
    return await task_scheduling.list_for_user(identity)


@app.post("/me/tasks", status_code=201)
async def create_my_task(body: TaskCreate, identity: Identity = Depends(require_browser_session)):
    task = await task_scheduling.create_task(identity.user, body.kind, body.spec, body.schedule, body.channel, body.tz_offset_min, body.title)
    return tasks.public(task)


@app.patch("/me/tasks/{task_id}")
async def update_my_task(task_id: str, body: TaskUpdate, identity: Identity = Depends(require_browser_session)):
    task = await task_scheduling.update_task(task_id, identity.user["id"], user=identity.user, **body.model_dump(exclude_none=True))
    return tasks.public(task)


@app.delete("/me/tasks/{task_id}")
async def delete_my_task(task_id: str, identity: Identity = Depends(require_browser_session)):
    await task_scheduling.delete_task(task_id, identity.user["id"])
    return {"deleted": True}


@app.post("/me/tasks/{task_id}/run")
async def run_my_task_now(task_id: str, identity: Identity = Depends(require_browser_session)):
    return await task_scheduling.run_now(task_id, identity.user["id"])


@app.get("/me/inbox")
async def my_inbox(identity: Identity = Depends(require_browser_session)):
    return {"items": await tasks.inbox(identity.user["id"]), "unread": await tasks.unread_count(identity.user["id"])}


@app.post("/me/inbox/read")
async def read_my_inbox(body: InboxRead, identity: Identity = Depends(require_browser_session)):
    return {"marked": await tasks.mark_read(identity.user["id"], body.ids), "unread": await tasks.unread_count(identity.user["id"])}


# ---- Knowledge service ----

@app.get("/knowledge/search")
async def knowledge_search(q: str, request: Request, limit: int = 6):
    """Hybrid retrieval over the knowledge base: passages with citations.

    Public, and it reaches a paid embedder and optional reranker, so it gets
    the same admission a chat turn does: a size bound, the caller's rate
    bucket, the network's rate bucket, a concurrency cap, and a daily ceiling
    on how much the whole deployment will spend from this route."""
    if not q.strip():
        raise HTTPException(400, "q is required")
    if len(q) > settings.knowledge_search_max_query_chars:
        raise HTTPException(413, f"Queries are limited to {settings.knowledge_search_max_query_chars} characters")
    identity = await resolve_identity(request)
    allowed, retry_after = await limits.allow_knowledge_search(identity.rate_limit_key, identity.ip)
    if not allowed:
        raise HTTPException(429, "Too many knowledge searches; please retry shortly", headers={"Retry-After": str(retry_after)})
    if not await limits.spend_daily_budget("knowledge-search", settings.knowledge_search_daily_budget):
        raise HTTPException(429, "The knowledge search budget for today is exhausted", headers={"Retry-After": "3600"})
    try:
        await asyncio.wait_for(limits._knowledge_slots.acquire(), timeout=settings.request_queue_timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise HTTPException(503, "Knowledge search is busy; please retry shortly") from exc
    try:
        hits, plan = await kb_retrieval.search(q, limit=max(1, min(limit, 20)), resolver=await kb_tool.resolver())
    finally:
        limits._knowledge_slots.release()
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
    return {**await store.stats(), "ingestion": kb_ingest.status(), "embedder": kb_tool.get_embedder_name(), "reranker": kb_retrieval.get_reranker().name}


@app.get("/admin/knowledge/protocols")
async def admin_knowledge_protocols(limit: int = 100, _admin: None = Depends(_require_admin)):
    """The registry as ingested: slugs (what the ingest endpoint takes), TVL, sources."""
    store = await kb_store.get_store()
    out = []
    for p in await store.list_protocols(limit=max(1, min(limit, 500))):
        out.append({"id": p.id, "slug": p.slug, "name": p.name, "symbol": p.symbol, "category": p.category, "tvl_usd": p.tvl_usd, "chains": p.chains,
                    "docs_url": kb_registry.guess_docs_url(p), "github_org": p.github_org, "governance_url": p.governance_url, "forum_url": p.forum_url, "defillama_slug": p.defillama_slug})
    return {"protocols": out}


@app.get("/admin/research/gaps")
async def admin_research_gaps(days: int = 30, _admin: None = Depends(_require_admin)):
    """The fall-through log: research questions only web search could answer,
    counted by the data topic they needed. The business case for a paid
    source, or the next free connector, in numbers."""
    return await research_gaps.summary(days=max(1, min(days, 365)))


@app.get("/admin/knowledge/overview")
async def admin_knowledge_overview(_admin: None = Depends(_require_admin)):
    """Everything the knowledge dashboard shows: totals, per-protocol coverage
    by source, source health, recent ingestion runs, retrieval config."""
    store = await kb_store.get_store()
    coverage = await store.coverage()
    runs = await store.recent_runs(limit=60)
    stats = await store.stats()
    with_docs = sum(1 for c in coverage if c["chunks"] >= 10)
    failing = [{"protocol_id": c["id"], "source": s["source"], "error": s.get("error")} for c in coverage for s in c["sources"] if s.get("error")]
    return {
        "totals": {**stats, "protocols_with_docs": with_docs, "failing_sources": len(failing)},
        "config": {"embedder": kb_tool.get_embedder_name(), "reranker": kb_retrieval.get_reranker().name, "registry_limit": settings.knowledge_registry_limit,
                   "docs_page_budget": settings.knowledge_docs_page_budget, "in_process_worker": settings.knowledge_ingest_enabled},
        "coverage": coverage, "runs": runs, "failing": failing[:50], "ingestion": kb_ingest.status(),
    }


@app.post("/admin/knowledge/derive")
async def admin_knowledge_derive(_admin: None = Depends(_require_admin)):
    """Recompute derived edges: COMPETITOR_OF (category + shared chain) and
    corroborated INTEGRATES_WITH (mentions in two or more independent documents)."""
    from app.knowledge import derive as kb_derive

    result = await kb_derive.derive_all()
    await kb_tool.resolver(force=True)
    return result


@app.get("/knowledge/graph/{entity_id:path}")
async def knowledge_graph(entity_id: str, relation: str | None = None):
    """Live edges around one entity, e.g. protocol:aave-v3 — competitors,
    integrations, chains, category — with confidence and provenance."""
    store = await kb_store.get_store()
    entity_ids = {e.id for e in await store.list_entities()}
    if entity_id not in entity_ids:
        raise HTTPException(404, f"unknown entity {entity_id}")
    rels = await store.neighbors(entity_id, relation=relation)
    # One line per edge; per-document mention rows fold into an evidence count.
    folded: dict[tuple[str, str, str], dict] = {}
    for r in sorted(rels, key=lambda r: -r.confidence):
        key = (r.relation, r.source_entity_id, r.target_entity_id)
        if key not in folded:
            folded[key] = {"relation": r.relation, "source": r.source_entity_id, "target": r.target_entity_id, "confidence": r.confidence,
                           "evidence": 0, "documents": [], "metadata": r.metadata}
        entry = folded[key]
        if r.source_document_id:
            entry["evidence"] += 1
            if len(entry["documents"]) < 10:
                entry["documents"].append(r.source_document_id)
    edges = sorted(folded.values(), key=lambda e: (e["relation"], -e["confidence"]))
    return {"entity": entity_id, "edges": edges}


@app.post("/admin/knowledge/bootstrap")
async def admin_knowledge_bootstrap(limit: int = 50, _admin: None = Depends(_require_admin)):
    """Fill the protocol registry from DefiLlama (top N by TVL) with entities and edges."""
    result = await kb_registry.bootstrap(limit=max(1, min(limit, 500)))
    await kb_tool.resolver(force=True)
    return result


@app.post("/admin/knowledge/ingest/{protocol_slug}")
async def admin_knowledge_ingest(protocol_slug: str, _admin: None = Depends(_require_admin)):
    """Run every applicable connector for one protocol now."""
    try:
        results = await kb_ingest.run_protocol(f"protocol:{protocol_slug}")
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    await kb_tool.resolver(force=True)
    return {"results": [r.__dict__ for r in results]}


@app.post("/admin/knowledge/tick")
async def admin_knowledge_tick(limit: int = 5, _admin: None = Depends(_require_admin)):
    """One ingestion pass over due (protocol, source) pairs."""
    results = await kb_ingest.tick(limit=max(1, min(limit, 50)))
    await kb_tool.resolver(force=True)
    return {"results": [r.__dict__ for r in results]}


@app.get("/billing/plans")
async def billing_catalog():
    return {"plans": billing_plans.catalog(), "trial_credits": billing_plans.ANONYMOUS.trial_credits, **billing.public_config()}


def _public_base(request: Request) -> str:
    return settings.public_base_url or f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


@app.post("/billing/checkout")
async def billing_checkout(body: CheckoutRequest, request: Request, identity: Identity = Depends(require_browser_session)):
    """A Stripe-hosted Checkout URL. Orbit never handles the card or wallet."""
    if identity.api_key is not None:
        raise HTTPException(403, "Billing is managed from a signed-in browser session")
    try:
        return await billing.create_checkout(identity.user, body.kind, body.item_id, _public_base(request))
    except billing.BillingNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except billing.SubscriptionExists as exc:
        # Not a validation error: the request is well-formed, the account is
        # simply already subscribed. The UI sends this case to the portal.
        raise HTTPException(409, {"error": "subscription_exists", "message": str(exc), "portal": True}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/billing/portal")
async def billing_portal(request: Request, identity: Identity = Depends(require_browser_session)):
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
        await _require_session_access(body.session_id, identity)
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
async def chat_history(session_id: str, identity: Identity = Depends(require_scope('chat'))):
    await _require_session_access(session_id, identity, claim=True)
    messages = await get_messages(session_id)
    if not settings.expose_tool_trajectory:
        messages = [dict(item, trajectory=public_activity(item.get("trajectory"))) for item in messages]
    return {
        "messages": messages,
        "context": await get_session_context(session_id),
    }


@app.delete("/chat/wallet/{session_id}")
async def forget_chat_wallet(session_id: str, identity: Identity = Depends(require_browser_session)):
    """The client disconnected its wallet: stop remembering it for this
    conversation (and drop any request parked for a wallet)."""
    from app.sessions import get_session_context, save_session_context

    await _require_session_access(session_id, identity)
    context = await get_session_context(session_id)
    context["connected_wallet"] = None
    context["pending_wallet_request"] = None
    await save_session_context(session_id, context)
    return {"status": "forgotten"}


@app.delete("/chat/history/{session_id}")
async def clear_chat_history(session_id: str, identity: Identity = Depends(require_browser_session)):
    await _require_session_access(session_id, identity)
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
async def trade_plan(plan_id: str, request: Request):
    await _require_plan_access(plan_id, request)
    try:
        return await get_plan(plan_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


async def _require_plan_access(plan_id: str, request: Request) -> None:
    """A trade plan belongs to the individual who asked for it. Without this,
    plan_id is a bearer token for execution: confirmation_text is just
    "CONFIRM {plan_id}". The rule itself lives in app/plan_access.py so the
    MCP server enforces the same one rather than a second copy of it."""
    try:
        await plan_access.require_plan_access(plan_id, await resolve_identity(request))
    except plan_access.PlanAccessDenied as exc:
        raise HTTPException(404, "Trade plan not found") from exc


# Preparing, submitting or having the server sign a transaction is a browser
# act. An API key may quote through chat; it may not move funds. Without this a
# data-only key belonging to an allowlisted account could have the server sign
# that account's plan, because scope was never consulted on the way to the
# custodial check -- only ownership and entitlement, both of which the key
# inherits from its owner.
@app.post("/trade-plans/{plan_id}/confirm")
async def confirm(plan_id: str, body: ConfirmRequest, request: Request,
                  _mode: None = Depends(_require_execution_mode),
                  _browser: Identity = Depends(require_browser_session)):
    await _require_plan_access(plan_id, request)
    identity = await resolve_identity(request)
    try:
        return await execute_confirmed_plan(plan_id, body.confirmation_text, identity.principal_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, _safe_detail(exc, "Trade request could not be completed")) from exc


@app.post("/trade-plans/{plan_id}/wallet-transaction")
async def wallet_transaction(plan_id: str, body: ConfirmRequest, request: Request,
                             _mode: None = Depends(_require_execution_mode),
                  _browser: Identity = Depends(require_browser_session)):
    await _require_plan_access(plan_id, request)
    try:
        return await prepare_wallet_transaction(plan_id, body.confirmation_text)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, _safe_detail(exc, "Trade request could not be completed")) from exc


@app.post("/trade-plans/{plan_id}/submit-wallet-transaction")
async def submit_signed_wallet_transaction(plan_id: str, body: SignedTransactionRequest, request: Request,
                                           _mode: None = Depends(_require_execution_mode),
                  _browser: Identity = Depends(require_browser_session)):
    await _require_plan_access(plan_id, request)
    try:
        return await submit_wallet_transaction(
            plan_id, body.confirmation_text, body.signed_transaction
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, _safe_detail(exc, "Trade request could not be completed")) from exc


@app.get("/executions/solana/{signature}")
async def solana_execution_status(signature: str):
    return await execution_policy.solana_execution_status(signature)


@app.post("/executions/relay/{request_id}")
async def track_relay_execution(request_id: str, request: Request,
                                _mode: None = Depends(_require_execution_mode)):
    # Claiming a Relay execution is the step immediately before wallet
    # approval, so it is a money-moving entry point and belongs behind the
    # same gate as the Jupiter and LI.FI routes. The GET status routes below
    # are deliberately not gated: an operator still has to reconcile swaps
    # that happened before a deployment was switched to research mode.
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
            # Attaching tracking to a conversation requires the same ownership
            # gate every other session-scoped route uses -- a revision match
            # alone let anyone holding a session id bind to it.
            await _require_session_access(session_id, await resolve_identity(request))
            context = await get_session_context(session_id)
            if context.get("revision") != revision or not context.get("active_workflow"):
                raise HTTPException(409, "This trade belongs to an earlier request")
            return await relay_tracking.track(request_id, session_id, revision)
        finally:
            await lease.release()
    return await relay_tracking.track(request_id)


@app.get("/executions/relay/session/{session_id}/{revision}")
async def relay_execution_for_turn(session_id: str, revision: int, request: Request):
    await _require_session_access(session_id, await resolve_identity(request))
    return await relay_tracking.for_turn(session_id, revision)


@app.get("/executions/relay/{request_id}")
async def relay_execution_status(request_id: str):
    status = await relay_tracking.get_status(request_id)
    if status is None:
        raise HTTPException(404, "Unknown Relay execution")
    return status

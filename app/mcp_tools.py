"""Extensible MCP gateway with persistent sessions, caching and tool routing.

MCP transport work lives on one background event loop. Each server owns a small
set of long-lived worker sessions, avoiding a new TCP/TLS handshake and MCP
initialize exchange for every tool call. Sync wrappers remain compatible with
DSPy while concurrent callers are coalesced and bounded.
"""

import asyncio
import concurrent.futures
import contextvars
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import redis.asyncio as redis
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from app.call_budget import charge_and_check
from app.metrics import increment
from app.capability_router import infer_tool_capabilities, infer_tool_chains, infer_tool_risk
from app.settings import settings
from app.tool_results import compact_tool_result, text_from_mcp_content

logger = logging.getLogger(__name__)
_ENV_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")
_WORD = re.compile(r"[a-z0-9]+")


def normalize_tool_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Adapt flattened model arguments to MCP tools with one object envelope."""
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    if required == ["request"] and set(properties) == {"request"} and "request" not in arguments:
        return {"request": arguments}
    return arguments


class _AsyncRunner:
    """One reusable event loop for sync DSPy tool functions."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run, name="mcp-gateway", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coroutine, timeout: float | None = None):
        self._ensure_started()
        assert self._loop is not None
        # asyncio.run_coroutine_threadsafe does NOT propagate contextvars
        # across the thread boundary onto this runner's own persistent
        # background loop -- a ContextVar set on the calling thread (e.g.
        # app.call_budget's per-turn budget) would be invisible inside
        # `coroutine`, silently no-op'ing anything that relies on it
        # (verified live: a budget of 0 failed to stop a real MCP call
        # before this fix). Capture the caller's context here (still on the
        # calling thread, so it's the correct one) and create the task
        # under that context on the target loop, so any ContextVar reads
        # deep inside `coroutine` see the caller's values.
        ctx = contextvars.copy_context()
        result_future: concurrent.futures.Future = concurrent.futures.Future()

        def _submit() -> None:
            try:
                task = ctx.run(self._loop.create_task, coroutine)
            except Exception as exc:  # pragma: no cover -- task creation itself failing
                result_future.set_exception(exc)
                return

            def _resolve(done_task: asyncio.Task) -> None:
                if done_task.cancelled():
                    result_future.cancel()
                    return
                exc = done_task.exception()
                if exc is not None:
                    result_future.set_exception(exc)
                else:
                    result_future.set_result(done_task.result())

            task.add_done_callback(_resolve)

        self._loop.call_soon_threadsafe(_submit)
        return result_future.result(timeout=timeout)

    def submit(self, coroutine) -> concurrent.futures.Future:
        self._ensure_started()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def stop(self) -> None:
        if self._loop is None or self._thread is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)


def _placeholder_values() -> dict[str, str]:
    allowed = {item.strip() for item in settings.mcp_env_allowlist.split(",") if item.strip()}
    values = {key: os.environ[key] for key in allowed if key in os.environ}
    configured = settings.model_dump()
    for key in allowed:
        value = configured.get(key.lower())
        if value is not None:
            values.setdefault(key, str(value))
    return values


def _resolve_env_placeholders(value: Any, lookup: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _ENV_PLACEHOLDER.sub(lambda match: lookup.get(match.group(1), ""), value)
    if isinstance(value, dict):
        return {key: _resolve_env_placeholders(item, lookup) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env_placeholders(item, lookup) for item in value]
    return value


def _load_config() -> dict[str, Any]:
    path = Path(settings.mcp_config_path) if settings.mcp_config_path else Path("mcp.json")
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text()).get("mcpServers", {})
        return _resolve_env_placeholders(raw, _placeholder_values())
    except Exception:
        logger.warning("Failed to parse MCP config at %s", path, exc_info=True)
        return {}


@asynccontextmanager
async def _connect(server_config: dict[str, Any]):
    if "url" in server_config:
        timeout = httpx.Timeout(connect=15, read=30, write=15, pool=15)
        async with httpx.AsyncClient(headers=server_config.get("headers"), timeout=timeout) as client:
            async with streamable_http_client(
                server_config["url"], http_client=client
            ) as (read, write, _get_session_id):
                yield read, write
    else:
        params = StdioServerParameters(
            command=server_config["command"],
            args=server_config.get("args", []),
            env=server_config.get("env") or None,
        )
        async with stdio_client(params) as (read, write):
            yield read, write


class _ServerWorker:
    """Own a transport for its full lifetime so AnyIO contexts stay in one task."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.ready: asyncio.Future | None = None

    async def ensure_started(self) -> None:
        if self.task is not None and not self.task.done():
            assert self.ready is not None
            await self.ready
            return
        loop = asyncio.get_running_loop()
        self.ready = loop.create_future()
        self.task = asyncio.create_task(self._serve())
        await self.ready

    async def _serve(self) -> None:
        try:
            async with _connect(self.config) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert self.ready is not None
                    self.ready.set_result(None)
                    while True:
                        operation, tool_name, arguments, future = await self.queue.get()
                        if operation == "close":
                            future.set_result(None)
                            return
                        try:
                            if operation == "list":
                                value = (await session.list_tools()).tools
                            else:
                                result = await session.call_tool(tool_name, arguments)
                                value = (text_from_mcp_content(result.content), bool(result.isError))
                            if not future.cancelled():
                                future.set_result(value)
                        except Exception as exc:
                            if not future.cancelled():
                                future.set_exception(exc)
        except BaseException as exc:
            if self.ready is not None and not self.ready.done():
                self.ready.set_exception(exc)
            while not self.queue.empty():
                *_, future = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(exc)
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def execute(self, operation: str, tool_name: str = "", arguments: dict | None = None):
        await self.ensure_started()
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((operation, tool_name, arguments or {}, future))
        return await future

    async def close(self) -> None:
        if self.task is None or self.task.done():
            return
        future = asyncio.get_running_loop().create_future()
        await self.queue.put(("close", "", {}, future))
        await future
        await self.task


class _ServerPool:
    def __init__(self, config: dict[str, Any]) -> None:
        size = max(1, settings.mcp_connections_per_server)
        self.workers = [_ServerWorker(config) for _ in range(size)]
        self._next = 0

    async def execute(self, operation: str, tool_name: str = "", arguments: dict | None = None):
        worker = self.workers[self._next]
        if operation != "list":
            self._next = (self._next + 1) % len(self.workers)
        return await worker.execute(operation, tool_name, arguments)

    async def close(self) -> None:
        await asyncio.gather(*(worker.close() for worker in self.workers), return_exceptions=True)


class MCPGateway:
    def __init__(self, configs: dict[str, dict[str, Any]]) -> None:
        self.runner = _AsyncRunner()
        self.configs = configs
        self.pools: dict[str, _ServerPool] = {}
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._redis: redis.Redis | None = None

    def _pool(self, server_name: str) -> _ServerPool:
        if server_name not in self.pools:
            self.pools[server_name] = _ServerPool(self.configs[server_name])
        return self.pools[server_name]

    async def list_tools(self, server_name: str) -> list[Any]:
        return await asyncio.wait_for(
            self._pool(server_name).execute("list"),
            timeout=settings.mcp_call_timeout_seconds,
        )

    @staticmethod
    def _key(server_name: str, tool_name: str, arguments: dict) -> str:
        def normalize(value):
            if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
                return value.lower()
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [normalize(item) for item in value]
            return value

        payload = json.dumps(
            [server_name, tool_name, normalize(arguments)],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    async def _redis_client(self) -> redis.Redis | None:
        if self._redis is not None or not settings.redis_url:
            return self._redis
        try:
            client = redis.from_url(settings.redis_url, decode_responses=True)
            await client.ping()
            self._redis = client
        except Exception:
            self._redis = None
        return self._redis

    @staticmethod
    def _ttl_for(tool_name: str) -> int:
        return settings.mcp_tool_cache_ttl_seconds.get(tool_name, settings.mcp_cache_ttl_seconds)

    async def _cache_get(self, key: str, tool_name: str) -> str | None:
        item = self._cache.get(key)
        if item is not None:
            expires, value = item
            if expires > time.monotonic():
                self._cache.move_to_end(key)
                return value
            self._cache.pop(key, None)
        client = await self._redis_client()
        if client is not None:
            try:
                value = await client.get(f"mcp:result:{key}")
                if value is not None:
                    self._remember(key, value, self._ttl_for(tool_name))
                    return value
            except Exception:
                logger.debug("MCP Redis cache read failed", exc_info=True)
        return None

    def _remember(self, key: str, value: str, ttl: int) -> None:
        self._cache[key] = (time.monotonic() + ttl, value)
        self._cache.move_to_end(key)
        while len(self._cache) > settings.mcp_cache_max_entries:
            self._cache.popitem(last=False)

    async def _cache_set(self, key: str, value: str, ttl: int) -> None:
        self._remember(key, value, ttl)
        client = await self._redis_client()
        if client is not None:
            try:
                await client.setex(f"mcp:result:{key}", ttl, value)
            except Exception:
                logger.debug("MCP Redis cache write failed", exc_info=True)

    async def _uncached_call(self, server_name: str, tool_name: str, arguments: dict) -> str:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_mcp_calls))
        async with self._semaphore:
            increment("mcp_provider_calls")
            text, is_error = await asyncio.wait_for(
                self._pool(server_name).execute("call", tool_name, arguments),
                timeout=settings.mcp_call_timeout_seconds,
            )
            if is_error:
                raise RuntimeError(text or f"MCP tool {tool_name} returned an error")
            return compact_tool_result(text)

    async def call(self, server_name: str, tool_name: str, arguments: dict) -> str:
        key = self._key(server_name, tool_name, arguments)
        cached = await self._cache_get(key, tool_name)
        if cached is not None:
            increment("mcp_cache_hits")
            return cached
        increment("mcp_cache_misses")
        task = self._inflight.get(key)
        creator = task is None
        if creator:
            # Only a genuine new external call is charged against the
            # turn's budget -- a cache hit above never reaches here, and a
            # call already in flight (the `else` branch, coalesced into the
            # same task) is the same external call, not a second one.
            if not charge_and_check():
                increment("mcp_budget_skips")
                return "MCP tool call failed: per-turn data budget reached"
            task = asyncio.create_task(self._uncached_call(server_name, tool_name, arguments))
            self._inflight[key] = task
        else:
            increment("mcp_coalesced_calls")
        try:
            result = await asyncio.shield(task)
            if creator:
                await self._cache_set(key, result, self._ttl_for(tool_name))
            return result
        finally:
            if creator:
                self._inflight.pop(key, None)

    async def close_async(self) -> None:
        await asyncio.gather(*(pool.close() for pool in self.pools.values()), return_exceptions=True)
        if self._redis is not None:
            await self._redis.aclose()

    def close(self) -> None:
        try:
            self.runner.run(self.close_async(), timeout=5)
        finally:
            self.runner.stop()


class MCPToolRegistry:
    """Discover tools once and choose only relevant schemas for each request."""

    def __init__(self, gateway: MCPGateway) -> None:
        self.gateway = gateway
        self._tools: list[Any] = []
        self._by_base_name: dict[str, Any] = {}
        self._discovery_lock = threading.Lock()
        self._discovered = False

    def discover(self) -> None:
        with self._discovery_lock:
            if self._discovered:
                return
            discovered: list[Any] = []
            by_base_name: dict[str, Any] = {}
            for server_name in self.gateway.configs:
                try:
                    tools = self.gateway.runner.run(
                        self.gateway.list_tools(server_name),
                        timeout=settings.mcp_call_timeout_seconds + 2,
                    )
                except Exception:
                    logger.warning("Could not connect to MCP server '%s'; skipping", server_name, exc_info=True)
                    continue
                for tool in tools:
                    wrapper = self._make_wrapper(
                        server_name,
                        tool.name,
                        tool.description or "",
                        getattr(tool, "inputSchema", None) or {},
                    )
                    discovered.append(wrapper)
                    by_base_name.setdefault(tool.name, wrapper)
            self._tools = discovered
            self._by_base_name = by_base_name
            self._discovered = True

    @property
    def ready(self) -> bool:
        return self._discovered

    def _make_wrapper(self, server_name: str, tool_name: str, description: str, schema: dict):
        def call(**kwargs) -> str:
            started = time.monotonic()
            try:
                arguments = normalize_tool_arguments(schema, kwargs)
                result = self.gateway.runner.run(
                    self.gateway.call(server_name, tool_name, arguments),
                    timeout=settings.mcp_call_timeout_seconds + 5,
                )
                call.mcp_stats["calls"] += 1
                elapsed_ms = (time.monotonic() - started) * 1000
                previous = call.mcp_stats["latency_ewma_ms"]
                call.mcp_stats["latency_ewma_ms"] = elapsed_ms if previous is None else previous * 0.8 + elapsed_ms * 0.2
                return result
            except Exception as exc:
                call.mcp_stats["calls"] += 1
                call.mcp_stats["failures"] += 1
                increment("mcp_errors")
                logger.warning("MCP tool %s.%s failed", server_name, tool_name, exc_info=True)
                return (
                    "MCP tool call failed: Provider request failed. "
                    "Check provider configuration, quota, and request parameters."
                )

        call.__name__ = f"mcp_{server_name}_{tool_name}"
        call.__doc__ = f"[MCP:{server_name}] {description} Input schema: {json.dumps(schema)}"
        call.mcp_server = server_name
        call.mcp_tool_name = tool_name
        call.mcp_description = description
        call.mcp_input_schema = schema
        call.mcp_capabilities = infer_tool_capabilities(tool_name, description)
        call.mcp_chains = infer_tool_chains(description, schema)
        call.mcp_risk = infer_tool_risk(tool_name, description)
        call.mcp_stats = {"calls": 0, "failures": 0, "latency_ewma_ms": None}
        return call

    def all(self) -> list[Any]:
        return list(self._tools)

    def get(self, base_name: str) -> Any | None:
        return self._by_base_name.get(base_name)

    def catalog(self) -> list[dict[str, Any]]:
        """Return safe operational metadata without exposing arguments or credentials."""
        items = []
        for tool in self._tools:
            stats = dict(getattr(tool, "mcp_stats", {}))
            calls = int(stats.get("calls") or 0)
            failures = int(stats.get("failures") or 0)
            items.append(
                {
                    "provider": getattr(tool, "mcp_server", "unknown"),
                    "tool": getattr(tool, "mcp_tool_name", tool.__name__),
                    "capabilities": list(getattr(tool, "mcp_capabilities", ())),
                    "chains": list(getattr(tool, "mcp_chains", ())),
                    "risk": getattr(tool, "mcp_risk", "read_only"),
                    "calls": calls,
                    "reliability": round((calls - failures) / calls, 4) if calls else None,
                    "latency_ewma_ms": round(stats["latency_ewma_ms"], 1)
                    if stats.get("latency_ewma_ms") is not None
                    else None,
                }
            )
        return items

    def select(
        self,
        request: str,
        limit: int | None = None,
        capabilities: tuple[str, ...] | list[str] = (),
        chains: tuple[str, ...] | list[str] = (),
        allow_execution: bool = False,
    ) -> list[Any]:
        maximum = limit or settings.max_mcp_tools_per_request
        query = set(_WORD.findall(request.lower()))
        aliases = {
            "portfolio": {"portfolio", "holding", "holdings", "balance", "wallet"},
            "transaction": {"transaction", "transactions", "tx", "activity"},
            "label": {"label", "labels", "identity", "entity"},
            "pnl": {"pnl", "profit", "loss", "performance"},
            "ohlcv": {"price", "chart", "ohlcv", "candle", "candles"},
            "holder": {"holder", "holders", "ownership"},
            "flow": {"flow", "flows", "inflow", "outflow"},
            "counterpart": {"counterparty", "counterparties"},
            "search": {"search", "research", "token", "protocol"},
        }
        expanded = set(query)
        for category, words in aliases.items():
            if query & words:
                expanded.add(category)
        ranked: list[tuple[int, str, Any]] = []
        for tool in self._tools:
            name = tool.mcp_tool_name.lower()
            description = tool.mcp_description.lower()
            tool_capabilities = set(getattr(tool, "mcp_capabilities", infer_tool_capabilities(name, description)))
            tool_chains = set(getattr(tool, "mcp_chains", ()))
            risk = getattr(tool, "mcp_risk", infer_tool_risk(name, description))
            if risk == "financial_execution" and not allow_execution:
                continue
            # Native Bitcoin has no contract address on the chains supported by
            # Nansen token OHLCV. Let live web search handle plain BTC queries;
            # wrapped-BTC contract requests can still use this tool.
            if (
                name == "token_ohlcv"
                and query & {"bitcoin", "btc"}
                and not re.search(r"0x[0-9a-fA-F]{40}", request)
            ):
                continue
            haystack = set(_WORD.findall(f"{name} {description}"))
            score = len(expanded & haystack)
            for category in expanded:
                if category in name:
                    score += 3
            score += 8 * len(set(capabilities) & tool_capabilities)
            if chains and tool_chains:
                covered = len(set(chains) & tool_chains)
                if covered == 0:
                    score -= 4
                else:
                    score += 3 * covered
            stats = getattr(tool, "mcp_stats", {})
            calls = int(stats.get("calls") or 0)
            failures = int(stats.get("failures") or 0)
            if calls:
                score -= round(4 * failures / calls)
            latency = stats.get("latency_ewma_ms")
            if latency and latency > 2000:
                score -= min(3, int(latency // 2000))
            if score > 0:
                ranked.append((score, name, tool))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in ranked[:maximum]]


_gateway = MCPGateway(_load_config())
_registry = MCPToolRegistry(_gateway)


def discover_mcp_tools() -> None:
    """Discover configured MCP capabilities after application startup begins."""
    _registry.discover()


def get_mcp_registry() -> MCPToolRegistry:
    return _registry


def load_mcp_tools() -> list[Any]:
    """Backward-compatible accessor used by existing integrations/tests."""
    return _registry.all()


def close_mcp_gateway() -> None:
    _gateway.close()

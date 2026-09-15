# DSPy Solana ReAct POC

A minimal Minara-style Solana assistant. DSPy ReAct can inspect balances and reason
with read-only tools. It can propose a Jupiter swap, but signing is isolated behind
a deterministic, expiring confirmation endpoint.

## Safety model

- The LLM never receives a private key.
- The signing function is not registered as a DSPy tool.
- The agent cannot execute trades by choosing a tool.
- Quotes expire after 120 seconds by default.
- Confirmation text must match the exact plan ID.
- Live execution is disabled by default.
- Tickers are not silently converted into mint addresses.
- Both mint addresses are verified against Jupiter's token registry.
- Jupiter Shield warnings are returned with the plan.
- USD notional and quote price-impact limits are enforced before confirmation.
- The unsigned swap transaction must simulate successfully before a plan is returned.

This is a POC, not a production custody system. Use a newly created, low-value wallet.
For production, replace the environment-held key with Turnkey, Privy, Fireblocks, or
another policy-controlled signer and persist plans/audit events in PostgreSQL.

## Run

```bash
cp .env.example .env
# Add the model provider key and optionally a Jupiter API key.
python --version  # must be Python 3.10-3.13
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -e '.[dev]'
npm install
npm run build:web
uvicorn app.main:app --reload --port 8000
```

If an earlier broad dependency resolution was interrupted, remove only this project's
`.venv`, recreate it, and run the commands above. The dependencies are pinned so pip
does not backtrack through dozens of releases.

Keep `LIVE_TRADING=false` while testing the chat and quote flow.

`RELAY_API_KEY` is an optional server-side credential for production Relay rate
limits. Leave it blank for anonymous local quotes and never expose it to browser code.

### Docker

The container build is self-contained: a Node stage bundles `web/*.ts`, a Python
stage resolves the pinned dependencies, and the runtime image is a non-root
`python:3.11-slim` that serves `app.main:app` behind a `/health` check. Runtime
state that the app writes (`role_memory.json`, `provider-overrides.json`) lives in
the `/app/data` volume; `.env` is never copied into the image.

```bash
cp .env.example .env            # set OPENAI_API_KEY and POSTGRES_PASSWORD at minimum
docker compose up --build -d    # api + postgres + redis, memory fallback disabled
curl -s localhost:8000/health
docker compose logs -f api
```

`docker compose` forces `ALLOW_MEMORY_FALLBACK=false` and points the API at its
own Postgres and Redis; the schema is created on first start. To run the image
against managed services instead, pass `DATABASE_URL` and `REDIS_URL` yourself:

```bash
docker build -t orbit-api .
docker run --rm -p 8000:8000 --env-file .env \
  -e DATABASE_URL=... -e REDIS_URL=... -e FORWARDED_ALLOW_IPS=<proxy-ip> \
  -v orbit-data:/app/data orbit-api
```

`UVICORN_WORKERS` defaults to 1 per container; scale with replicas rather than
workers so the in-process semantic caches and the two reconciliation workers are
not duplicated inside one instance. Set `FORWARDED_ALLOW_IPS` (uvicorn) and
`TRUSTED_PROXY_HOSTS` (application rate limits) to the reverse proxy's address.
Any command passed to the image replaces uvicorn, e.g.
`docker run --rm orbit-api python -c "import app.main"`.

The web UI is available at `http://localhost:8000/ui/`. Natural-language Solana-only
buys and swaps use Jupiter. Any route involving another chain uses Relay, including
same-chain EVM swaps. Complete Relay requests are quoted automatically and rendered as
the same compact review/approval flow as Jupiter; incomplete requests stay conversational
and ask only for missing details. The browser bundle is generated from `web/relay.ts`;
rerun `npm run build:web` after changing the wallet or Relay frontend. Ambiguous meme-coin
symbols require an exact contract or mint, and signing always requires a separate confirm
click.

### Privy embedded wallets

Set `PRIVY_APP_ID` and `PRIVY_CLIENT_ID` to enable email-code onboarding through
Privy's vanilla browser SDK. A successful login creates or restores both an Ethereum
and Solana embedded wallet; Relay automatically uses the address and provider matching
the selected source chain. Phantom, MetaMask, and read-only addresses remain available.

Only the public app/client identifiers are returned by `/config/public`. Privy app
secrets and authorization-key private keys must never be placed in browser settings.
Delegated signing is intentionally disabled: V1 transactions remain user-initiated,
use fresh quotes, and require the explicit **Confirm swap** action. Add backend signing
only after configuring Privy signer policies, secure key custody, spend/chain/token
limits, simulation, and auditable approval thresholds.

Set `REOWN_PROJECT_ID` to add Reown AppKit/WalletConnect as an external EVM-wallet
fallback. The project ID is a public browser identifier, but the metadata origin must
match the domain configured in Reown. Reown supplies the EIP-1193 provider to the same
Relay approval flow; it does not add an independent execution path.

Set `PERPLEXITY_API_KEY` to route live web, URL, public professional, and financial
queries through Perplexity Agent API. Obvious requests are mapped directly to one
hosted tool, cached, and coalesced to avoid an extra planning-model call. Per-tool
costs are configurable and exposed through `/capabilities`; estimated spend is
reported in `/health` as micro-USD counters. Perplexity sandbox is deliberately not
registered. When the key is absent, general live search continues to use OpenAI's
hosted web search, while MCP and on-chain routes are unchanged.

## Example

```bash
curl -s http://localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{
    "message":"Check my balance and prepare a swap of 0.01 SOL into the token mint <MINT>, using at most 1% slippage",
    "wallet_address":"<WALLET>"
  }'
```

If a plan is returned, inspect the input/output mints, atomic quantities, expected
output, price impact, route and fees. Execution requires the exact returned text:

```bash
curl -s http://localhost:8000/trade-plans/<PLAN_ID>/confirm \
  -H 'content-type: application/json' \
  -d '{"confirmation_text":"CONFIRM <PLAN_ID>"}'
```

The confirmation endpoint will still refuse to sign unless `LIVE_TRADING=true`, the
private key exists and its public key matches the wallet used for the quote.

## Next POC increments

1. Persist plans, trajectories and audit events in PostgreSQL.
2. Add exact pre/post token balance deltas to the simulation response.
3. Replace environment-key signing with a policy-controlled signer.
4. Add take-profit/stop-loss monitoring as deterministic jobs, not free-running LLM loops.
5. Build a labelled evaluation set and optimize only after baseline metrics exist.

## Production scaling

The application is stateless across HTTP workers when Redis and PostgreSQL are
configured. MCP servers are discovered once per worker, connections are reused,
identical concurrent calls are coalesced, and successful results are cached in
Redis. Only MCP tools relevant to the current request are exposed to the research
agent; common wallet-portfolio lookups bypass the planning loop entirely. Tool
observations and conversation context are bounded before reaching the model.

For production, set `ALLOW_MEMORY_FALLBACK=false`, provide shared `REDIS_URL` and
`DATABASE_URL` values, keep `LIVE_TRADING=false` until the signing policy is ready,
and run multiple workers behind a load balancer. Set `TRUSTED_PROXY_HOSTS` to the
exact immediate proxy IPs if forwarded client addresses should drive rate limits.
Do not expose `/rpc/solana` without an upstream gateway/WAF; the application also
enforces per-client request, body, and batch limits. Start with:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

Tune worker count and the `MAX_CONCURRENT_*` settings to the model, Nansen, RPC,
and Jupiter quotas—not just CPU capacity. `/health` exposes aggregate counters for
provider calls, MCP cache hits/misses, coalesced calls, errors, rate limits, and
queue timeouts. In a multi-worker deployment, scrape each worker and aggregate in
your monitoring system.

MCP configuration can interpolate only variables named in `MCP_ENV_ALLOWLIST`.
Set `EXPOSE_TOOL_TRAJECTORY=false` when tool activity cards are not required for
authenticated users. Rotate credentials immediately if an `.env` file is ever
shared, logged, or copied outside the deployment secret store.

Adding another MCP only requires another entry in `mcp.json`. Its tools are
discovered and become eligible for request-time shortlisting automatically. For a
high-volume, unambiguous operation, add a deterministic adapter in
`direct_mcp_request`; everything else continues through the generic research agent.

## Intent and capability routing

Every chat is resolved to a workflow intent (`general`, `research`, `portfolio`,
`trade`, or `cross_chain_swap`) plus provider-independent capabilities such as
`market_data`, `wallet_intelligence`, `token_security`, and `token_discovery`.
High-confidence requests use deterministic rules; ambiguous conversational
follow-ups use the small DSPy classifier with chat history.

Each chat also has a canonical context snapshot stored separately from display
history. The snapshot contains a monotonic revision, connected wallet, focused
token/wallet entity, last intent/capabilities, and active trade workflow. Routing
uses this state before prose history, so a provider result or URL cannot silently
replace the active entity. Cancellation clears the active workflow, while a new
incomplete trade replaces the previous route with a `collecting_details` workflow.

Quick-action buttons carry a typed action payload (intent, capabilities, entity,
chain, wallet scope, and context revision). The server verifies the complete payload
against the actions persisted on the latest assistant response; the browser cannot
invent capabilities or execution intent. Stale actions and stale tab revisions fail
with HTTP 409 instead of being reinterpreted as free text.

Turns are serialized per session with a Redis distributed lock (or a local lock in
development). The user message, assistant response, response artifacts, and next
context snapshot are committed in one Redis transaction. `GET
/chat/history/{session_id}` returns both display messages and the canonical context.
History restoration preserves typed actions and enables them only on the newest
response. Older sessions are migrated lazily by reconstructing state using the same
focus-selection rule as the live reducer.

The original user utterance and the server-resolved contextual prompt are separate
graph fields. Deterministic routing evaluates the original request first; resolved
entities are used only where a follow-up genuinely needs them. Explicit research or
portfolio requests close natural-language trade collection, while already-created
quote cards remain reviewable through their immutable plan IDs.

The MCP registry infers capabilities, chain coverage, and risk from every newly
discovered tool. It ranks only the best matching schemas using semantic overlap,
capability/chain fit, observed latency, and reliability. Financial-execution tools
are excluded from research agents. `/capabilities` exposes safe catalog and health
metadata, while chat responses include the selected intent and capabilities for UI
observability.

## Multi-provider routing

Read-only API providers sit behind `ProviderRouter`, below the capability planner and
above vendor adapters. The agent requests `market_data`, `token_discovery`,
`token_security`, `wallet_intelligence`, `defi_data`, `listing_events`,
`finance_data`, `web_research`, `url_fetch`, or `people_intelligence`; it does not
need to select a vendor. Candidate tools are filtered by capability, chain,
configuration, risk and request shape, then ranked using provider priority, recent
reliability, latency and configured invocation cost.

The router enforces a rolling per-tool quota, opens a short circuit after repeated
failures, reuses equivalent fresh requests through a conservative semantic cache,
coalesces concurrent identical requests, and falls through to the next eligible
provider. `DexScreenerProvider` supplies public DEX market and token-discovery data.
`BirdeyeProvider` supplies chain-specific token overviews, `MobulaProvider` supplies
enriched token details and security metrics, and `BitqueryProvider` supplies recent
parameterized on-chain DEX trades. The latter three activate only when their API keys
are configured. Perplexity supplies web, URL, people and finance retrieval; OpenAI
hosted web search is the configured general fallback. Nansen and future MCP servers
continue through the dynamically discovered MCP registry, which applies its own
connection pooling, shortlisting, health ranking, caching and request coalescing.

CoinGecko and CoinMarketCap provide normalized contract identity and market metadata.
GoPlus and Honeypot.is provide EVM token-risk and sellability fallbacks. DeFiLlama
provides chain TVL, Helius provides Solana wallet activity, and GoldRush provides EVM
wallet transaction history. Listing/delisting requests use a constrained Perplexity
search that prefers official Binance, Bybit, and OKX announcement pages. Credentialed
adapters remain visible but unavailable in the admin catalog until their keys exist.

Set `ROOTDATA_API_KEY` to enable structured Web3 project, VC and people search through
RootData's `id_map` API. Project, VC and people maps are cached independently for 12
hours by default (`ROOTDATA_MAP_CACHE_TTL_SECONDS`) and searches are ranked locally,
which avoids paying RootData's 20-credit map cost for every chat query. The API key is
server-only and is never included in tool arguments, activity traces or public config.

Tool calling traces are enabled by default with `EXPOSE_TOOL_TRAJECTORY=true`. Each
applicable chat response displays an expanded **Agent activity** section containing
the selected tool/provider, safe input arguments, completion status, returned evidence
and source cards. General conversation without a tool call intentionally has no trace.

Relay remains the primary non-Solana execution router and Jupiter remains the Solana
router. `/execution/lifi/quote` and `/execution/lifi/status/{tx_hash}` expose LI.FI as
a quote/status-only backup; the server never signs or submits a LI.FI transaction.
Coinbase Wallet is available as a first-class self-custody EVM connection through the
official Coinbase Wallet SDK, supporting Smart Wallet/passkey, extension and mobile
connection experiences. Its EIP-1193 provider is passed directly to Relay for EVM
quote execution. Login uses a five-minute, single-use signed challenge followed by a
seven-day HttpOnly, SameSite session cookie; no wallet key or reusable signature is
stored by Orbit.
When `CLICKHOUSE_HOST` is configured, provider success/latency events are written by a
bounded background queue so analytics outages do not block chat requests.

`/capabilities` reports configuration, cost, quota remaining, health, reliability and
latency for API providers alongside discovered MCP tools. `/health` exposes provider
calls, failures, quota skips, cache hits, coalesced calls and estimated cost counters.
Provider settings are environment-driven so deployment quotas and contract prices
can be updated without code changes.

US and Indian stock-market, ticker, company-share, earnings and equity-research
requests resolve to the dedicated `equity_research` capability. Only Perplexity
finance and Perplexity web-search tools provide that capability; OpenAI and crypto
market providers are deliberately ineligible. If Perplexity is unconfigured,
unhealthy or fully quota-exhausted, the assistant reports that condition instead of
silently switching equity research to another vendor.

The provider control plane is available at `http://localhost:8000/ui/admin.html`.
Set `ADMIN_API_KEY`, enter it in the dashboard, and use the table to enable or disable
tools and override priority, requests-per-minute quota, or invocation cost. Changes
are written atomically to `PROVIDER_OVERRIDES_PATH`. The route-preview panel shows
the currently eligible provider order for a capability and sample request. API keys
are never returned by the admin APIs or rendered in the provider table.

The admin page also includes an intent-classification lab and a provider sandbox.
Intent tests are deterministic dry runs and consume no provider/model quota. Provider
tests default to ranking-only dry runs; the separately labelled live mode invokes only
read-only tools and may consume provider quota. Chat responses carry compact context
capsules, evidence freshness, a canonical intent-lock fingerprint, trade-readiness
checks, and destination-gas guidance. `/wallet-health/{wallet}` and
`/portfolio/{wallet}/scenario` expose deterministic Solana wallet diagnostics and
price-shock simulations, while `/executions/solana/{signature}` tracks submitted
Solana transactions without requiring another model call.

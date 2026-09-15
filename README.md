# Orbit — Web3 Copilot

Orbit is a multi-chain Web3 copilot: it researches tokens, wallets, protocols and
markets with live on-chain and market evidence, prepares Solana and cross-chain
swaps as reviewable quotes, and enforces the user's own risk rules before any
trade reaches a confirmation card. Every answer is grounded in named provider
data with freshness and provenance checks; nothing is ever signed server-side —
a swap executes only in the user's wallet, after an explicit confirm.

It runs as a FastAPI service with a LangGraph agent graph, DSPy programs on any
LiteLLM-compatible model, a multi-provider data router, and MCP integrations,
with Postgres and Redis for durable state. It ships as a container with CI-built
images and a one-command production deploy.

## What it does

**Research with evidence.** Token deep-dives across ten due-diligence dimensions,
a crypto market overview card, trending pools and new pairs per chain, top
holders, token security dossiers (Jupiter Shield, GoPlus, Honeypot.is), DeFi
TVL and yields, listing events, Web3 project/VC/people search, and live web
research. Symbols resolve chain-agnostically and Orbit asks when a ticker is
ambiguous across chains instead of guessing.

**Wallet intelligence.** Balances, holdings, activity and health diagnostics for
Solana and EVM wallets; portfolio scenarios ("what if SOL drops 20%") and
read-only trade simulations ("what would I get for half my SOL") against real
quotes. Sign in with Phantom, MetaMask, Coinbase Wallet, WalletConnect (Reown),
or Privy embedded wallets, or paste a public address for read-only use.

**Trading, gated.** Natural-language swaps are quoted through Jupiter (Solana)
and Relay (cross-chain and EVM), verified against token registries, simulated,
and rendered as a review card. A per-trade cap, slippage cap and price-impact
cap are enforced on every quote. Users add their own **risk charter** through a
card in the UI — max per trade, max % of portfolio per position, max slippage,
verified-only tokens, allowed chains — and those rules are checked exactly on
the real quote before a card is shown; a violation blocks the trade with the
precise reason.

**Pay per message with x402 (optional).** When `X402_ENABLED` is on, `POST /chat`
answers HTTP 402 with x402 v2 payment requirements (USDC on Base or Base
Sepolia, `X402_PRICE` per message, paid to `X402_PAY_TO`); the browser signs an
EIP-3009 authorization with the connected EVM wallet and retries, and the
facilitator verifies it before the turn runs and settles it after. The composer
shows the price and whether a paying wallet is connected. Off by default.

**Light and dark themes**, following the OS by default and switchable from the
top bar or Profile & settings. Conversations can be deleted one at a time from
the sidebar or the top bar (inline confirm, no browser dialogs), or all at once
from Profile & settings; deletion removes the server-side history.

**Team desk.** A switch in the composer routes requests through a multi-agent
trading desk: a Coordinator fans each request to Market Research, Execution and
Risk specialists and returns one synthesized answer and card.

**Self-improving analysis.** A role-memory loop stores deep-dive decisions,
reflects on realized price outcomes without outcome bias, and recalls
asset-scoped lessons into the next analysis. Tool rankings learn from what
answers could actually use (see *Outcome-scored tools*).

## Safety model

- The model never receives a private key, and no signing function is a tool.
- Quotes expire (120 s by default); confirmation must name the exact plan ID and
  is performed in the user's wallet. Server-side signing (`LIVE_TRADING`) is off
  by default and independent of the chat path.
- Tickers are never silently converted to addresses; both sides of a swap are
  verified against Jupiter's registry or Relay's chain data.
- USD notional, slippage and price-impact caps are enforced before confirmation;
  the unsigned transaction must simulate successfully.
- The user's risk charter can only make trades stricter, never looser; a
  charter violation supersedes the plan so its confirm token is inert.
- Financial-execution tools are excluded from research agents; a conditional or
  questioning phrasing ("if SOL drops…", "should I…") can never become a quote.
- Advice-shaped questions are answered as research with an explicit
  not-financial-advice framing.

## Architecture

```text
User message
  -> controls & session continuations        (deterministic)
  -> anchored rules                           (execution commands, addresses/URLs, "my" ownership)
  -> speech model for everything topical      (rules kept as capability hints)
  -> LangGraph nodes: general | research | portfolio | trade | cross-chain | team
       -> ProviderRouter (capability -> ranked provider tools, quotas, circuit breakers, cache)
       -> MCP registry (Nansen and any server in mcp.json)
  -> answer validator (provenance, freshness, grounding, cross-source consistency)
  -> outcome recording (which tools' results the answer could use)
```

- **Routing is model-first.** A small DSPy classifier decides the speech act
  (research, advice, explain, portfolio, policy, quote) for every topical
  request; hand-written rules decide only what a hard signal anchors. On the
  labelled routing set this moved intent accuracy from 90.5% to 100% at ~1.1 s
  p50. Details in `docs/routing-architecture.md`.
- **Providers sit behind one router.** Capabilities (`market_data`,
  `token_discovery`, `token_security`, `wallet_intelligence`, `defi_data`,
  `finance_data`, `web_research`, …) map to ranked tools from DexScreener,
  GeckoTerminal, Birdeye, Mobula, Bitquery, CoinGecko, CoinMarketCap, GoPlus,
  Honeypot.is, DeFiLlama, Dune, Helius, GoldRush, RootData, Perplexity, OpenAI
  web search, and Nansen (MCP). Ranking weighs priority, keyword fit, chain fit,
  reliability, latency, cost and learned outcomes; per-tool quotas, circuit
  breakers, a conservative semantic cache and request coalescing sit underneath.
- **Every answer is validated.** Step 7 of the pipeline checks provenance,
  freshness, grounding of figures in retrieved data, and consistency of the
  same metric across providers; results ride along on the response and never
  rewrite the answer.
- **Outcome-scored tools.** After each turn, every tool that ran is credited
  when its result was usable (clean call, grounded answer) and debited
  otherwise. A Beta-smoothed success rate per tool feeds the ranking, so tools
  that keep returning unusable data rank down on their own. Unobserved tools
  sit at the prior and earn an adjustment only with evidence.
- **Per-turn budgets and guards.** A call/cost budget bounds external calls per
  turn; a repeat-call guard stops a stuck tool loop; LLM calls have explicit
  timeouts, retries and a cross-provider fallback model.
- **Durable, serialized sessions.** Turns are serialized per session; message,
  response artifacts and a canonical context snapshot (revision, wallet, focus
  entity, active workflow, risk charter, team mode) commit together.

## Technical architecture

### Stack

| Layer | Technology |
|---|---|
| API | FastAPI + uvicorn; static UI served from `app/static` (vanilla HTML/JS, esbuild-bundled wallet SDKs from `web/*.ts`) |
| Agent graph | LangGraph state machine (`app/graph.py`, nodes in `app/nodes/`) |
| Model programs | DSPy signatures/ReAct on any LiteLLM model (`MODEL`, default `openai/gpt-4.1-mini`); optional separate `INTENT_MODEL` for the routing classifier |
| Data providers | `ProviderRouter` (`app/provider_router.py`) over 36 read-only tools in 14 capabilities; MCP registry (`app/mcp_tools.py`) for Nansen and any `mcp.json` server |
| Execution | Jupiter (Solana quotes/plans), Relay (cross-chain and EVM), LI.FI (quote/status backup); wallet-side signing only |
| State | Postgres (trade plans, Relay executions, tool outcomes) and Redis (sessions, per-session turn locks, caches); in-memory fallback for development |
| Analytics | ClickHouse (provider events, per-turn budgets) and Langfuse tracing, both optional |
| Delivery | 3-stage Docker image, compose stack, CI (tests → image build → `/health` smoke test → GHCR publish), Caddy TLS overlay |

### A user query, end to end

`POST /chat` runs one turn (`app/main.py`):

1. **Admission.** Per-client chat rate limit, global concurrency slot with a
   queue timeout, then a per-session turn lease so two tabs cannot mutate the
   same chat at once. The client's `context_revision` must match the stored
   snapshot or the request fails with 409 instead of being reinterpreted.
2. **Typed inputs.** A quick action is authorized only if it matches, byte for
   byte, an action the server persisted on the latest response. The team-desk
   switch and the risk-charter card are typed fields; a charter is validated
   against the built-in caps and rewritten into one canonical
   `set my risk charter: …` message so transcript, chip and Risk agent share
   one rendering.
3. **Contextual resolution.** A swap that was refused for a missing wallet is
   parked for one turn and re-run when the next message acknowledges the
   connection ("yes connected"). A pending token disambiguation ("which chain?")
   is consumed by a one-word reply. Otherwise the follow-up is bound to the
   session's focused entity only when its name matches the canonical focus —
   never inferred from unrelated history.
4. **Routing** (below) picks an intent and capabilities.
5. **The graph runs** under a per-turn call/cost budget (`MAX_EXTERNAL_CALLS_PER_TURN`,
   `MAX_PAID_DATA_COST_USD_PER_TURN`) and a repeat-call guard that stops a
   stuck tool loop from retrying an identical failing call.
6. **Answer validation.** Provenance (are sources cited), freshness (as-of
   timestamps on time-sensitive answers), grounding (do the figures in the
   answer trace to retrieved data), and consistency (the same metric across
   providers for the same contract). Advisory: attached to the response, never
   rewrites the answer.
7. **Outcome recording.** Every tool that ran is credited or debited for the
   router's learned ranking.
8. **Commit.** The next context snapshot (revision, wallet, focus entity, last
   intent/capabilities, active workflow, risk charter and fields, team mode,
   one-turn pending state) is advanced and committed with the message pair in
   one transaction. The response carries intent, capabilities, evidence
   summary, validation, risk assessment, trade readiness, gas advisory, the
   team mode and the charter, so the UI never has to guess state.

### Routing precedence

`app/routing/resolver.py` decides, in order:

1. **Controls and continuations** — confirm/cancel, risk-charter and team-mode
   commands, quick actions, parameter fragments for an active trade. Deterministic.
2. **Anchored rules** — a rule keeps deciding by itself only when a hard signal
   anchors it: an execution command with its fields (`trade`, `cross_chain_swap`),
   an address or URL in the message, "my" ownership (balances, holdings,
   activity, health, price-shock scenarios, simulations of the user's own
   position), a control verb, an equity ticker. These are precise and free.
3. **The speech model** decides everything else — every request a rule matched
   only by topic keywords, and everything no rule matched. A DSPy classifier
   (`SpeechResolution`) labels the act — `research`, `advice`, `explain`,
   `portfolio`, `policy`, `quote`, `abstain` — with a domain and confidence. The
   rule that fired survives only as a capability hint when it agrees on intent;
   otherwise the model's reading of the act wins. Results are cached per
   request text.
4. **Fallbacks** — an uncertain or abstaining model defers to a strong rule or
   asks a clarifying question (the embedding tier is never a second fuzzy
   opinion); an unavailable model falls back to nearest-labelled-example
   embeddings, then asks.

The model can never grant execution: a model-proposed `quote` reaches
`plan_execution_route` only with `explicit_action` on a crypto subject and only
when the message carries none of the competing-speech signals (`if`, `when`,
`?`, `should`, negation…). Intent → node: `general`, `research`, `portfolio`,
`trade` (Jupiter), `cross_chain_swap` (Relay), or `team` when the desk is on.

Measured on `scripts/routing_eval/cases.json` (49 labelled cases including 15
long-tail phrasings and policy questions): rules-first 90.5% intent accuracy,
model-first 100%, ~1.1 s p50 / 1.8 s p95 with the model on the hot path for
about 70% of turns. When a prompt misroutes, the fix is a new labelled case and
a sharper classifier example — not a regex.

### Graph nodes

- **general** — controls (confirm/cancel, charter set/show/clear, team on/off),
  the policy summary (built from settings and the session, never the model),
  execution explanations, and a lightweight LM reply for conversation.
- **research** — deterministic intercepts run first when they fit: the crypto
  market overview card, the ten-dimension token deep-dive, Hyperliquid
  positions, the market brief. Otherwise `ProviderRouter.try_route_across`
  ranks tools across every eligible capability and calls the best; if no
  provider answers, a DSPy ReAct agent runs with only the router-chosen tools
  plus web search. Ambiguous symbols trigger a chain question rather than a guess.
- **portfolio** — wallet snapshot (GoldRush/Bitquery balances, Hyperliquid
  positions, optional Nansen DeFi enrichment), holdings and activity, health
  diagnostics, price-shock scenarios, and read-only trade simulations against
  real Jupiter quotes.
- **trade** — parse → resolve both tokens against Jupiter's registry → quote →
  deterministic caps (notional, slippage, price impact) → simulate the unsigned
  transaction → persist an expiring plan → **charter risk** (structured rules
  checked exactly; free-text notes interpreted by the Risk agent) → review card.
- **cross_chain_swap** — extract a typed draft from the current message only,
  resolve tokens against Relay chain data, quote, review card; the browser
  wallet signs and the backend independently tracks Relay's status API.
- **team** — a Coordinator fans the request to Market Research, Execution and
  Risk specialists (reusing the nodes above) and synthesizes one answer; the
  Risk specialist fetches two or more providers so the consistency check fires.

### Tool selection policy

Two layers, then a learned term:

1. **Capabilities.** Routing yields the capabilities a request needs
   (`market_data`, `token_discovery`, `token_security`, `wallet_intelligence`,
   `defi_data`, `finance_data`, `web_research`, `url_fetch`,
   `people_intelligence`, `project_intelligence`, `vc_intelligence`,
   `equity_research`, `market_sentiment`, `listing_events`). A reachability
   backstop makes a tool eligible whenever its own matcher fires, even if the
   classifier did not name its capability — so vocabulary drift between the two
   layers can never hide a tool.
2. **Ranking across the union.** `try_route_across` ranks every candidate from
   every eligible capability by one capability-independent score:

   ```text
   score = priority + 8
         + 2.0 × keyword hits
         + chain fit (+2 match, −5 conflict)
         + reliability × PROVIDER_HEALTH_WEIGHT
         + PROVIDER_OUTCOME_WEIGHT × (learned success rate − prior)
         − 2.0 × consecutive failures
         − latency penalty (EWMA, capped)
         − cost × PROVIDER_COST_WEIGHT
         − breadth penalty for very long requests
   ```

   A tool's regex matcher is a hard gate; a description-bearing tool that fails
   it gets one semantic second chance (embedding similarity ≥ 0.45, calibrated
   against real requests), but a real regex match always outranks a semantic
   fallback. Each tool has a rolling per-minute quota and a circuit breaker
   (opens after repeated failures, cools down); identical concurrent requests
   coalesce; equivalent recent requests are served from a conservative semantic
   cache. Credentialed tools are visible but ineligible until their keys exist.
   Admins can disable tools and override priority, quota and cost at runtime.
3. **Outcome-scored tools.** After every turn, a tool is credited when its call
   succeeded and the answer's grounding check did not warn, debited otherwise.
   Counts are stored per tool per day (14-day window) and folded into the score
   as a Beta-smoothed rate (prior 0.8, weight 5): a new tool sits at the prior
   with zero adjustment (provisional) and earns one only with evidence
   (trusted). `GET /admin/tools/outcomes` shows the live numbers.

MCP tools follow the same idea in their own registry: capabilities, chain
coverage and risk are inferred from each discovered schema; only the best few
matching tools (`MAX_MCP_TOOLS_PER_REQUEST`) are exposed per request;
financial-execution tools are excluded from research agents.

### Cross-cutting guards

- **Call budget** — a per-turn ContextVar counts every paid provider, MCP and
  embedding call and stops at the call or cost ceiling; it propagates through
  thread pools explicitly so no path can escape it.
- **Repeat guard** — one scope per model attempt; an exact repeat of a failed
  `(tool, args)` raises immediately with a corrective observation.
- **LLM resilience** — explicit per-request timeout, litellm retries, and a
  one-shot cross-provider fallback model on transient failures, applied at the
  single `_call_lm` choke point.
- **Execution invariants** — Solana-only routes use Jupiter, anything else
  Relay; a symbol-only request stays in `collect` mode and asks for the chain;
  the current message is authoritative and a new trade verb never inherits an
  older quote; research or portfolio turns clear natural-language trade
  collection; an already-created card stays addressable only by its plan ID.

### Memory

- **Session context** — the canonical snapshot described above, stored apart
  from display history and used by routing before any prose.
- **Role memory** — deep-dive decisions are stored with their evidence; a
  bias-free reflection compares them to the realized price outcome after a
  window and produces one process correction, recalled into the next analysis
  of the same asset. Safety rules always override a learned lesson.

### Observability and evaluation

- `/health` — counters for provider calls, failures, quota skips, cache hits,
  coalesced calls, rate limits, queue timeouts, validation warnings, LLM
  fallbacks and estimated spend. `/capabilities` — catalog, configuration,
  quota, reliability and latency per tool.
- ClickHouse receives provider events and per-turn budget events through a
  bounded background queue; Langfuse receives traces.
- `scripts/routing_eval/harness.py` — `router` mode (deterministic layer, free),
  `resolve` mode (the decision layer with the real classifier: intent accuracy,
  which tier decided, latency), `chat` mode (end to end through a running
  server: tool reached, forbidden-tool rate, ask accuracy, intent accuracy).
- The test suite covers routing invariants, execution safety, the charter,
  validation, budgets and guards, the desk, Docker-facing API paths and the
  UI-facing contracts (476 tests).

### Milestones

Multi-chain token resolution that asks on ambiguity · Jupiter-verified token
security dossiers · ten-dimension token deep-dive with anti-hallucination rules
and role-memory reflection · crypto market overview card · GeckoTerminal
trending/new pools per chain · cross-capability tool ranking with a reachability
backstop · semantic tool fallback calibrated on real requests · per-turn call
budget, repeat guard and LLM-hop resilience · answer validator with cross-source
consistency · risk charter with a hard veto, now a structured card checked
exactly on the quote · multi-agent trading desk · team-desk and charter controls
in the UI · swap-after-connect and pronoun/focus resolution · model-first
routing measured on a labelled set · outcome-scored tool ranking · production
container, CI image pipeline and one-command TLS deploy.

### Known limitations

- The routing classifier adds ~1.1 s p50 to topical turns; a faster tier
  (`INTENT_MODEL`) was evaluated and rejected for accuracy, so the answer model
  is used.
- The risk charter gates Jupiter (Solana) plans; Relay drafts produce no plan
  object today, so they are not charter-checked.
- A portfolio snapshot for an extremely large wallet can exceed the turn
  timeout.
- Chain vocabulary is finite: tokens native to chains outside the resolver's
  list (for example HYPE on Hyperliquid) resolve to same-symbol tokens on
  supported chains instead.

## Run locally

```bash
cp .env.example .env            # at minimum OPENAI_API_KEY; provider keys as you have them
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
npm install && npm run build:web
uvicorn app.main:app --reload --port 8000
```

UI: `http://localhost:8000/ui/`. Admin control plane: `/ui/admin.html` (set
`ADMIN_API_KEY`). Without `DATABASE_URL`/`REDIS_URL` the service falls back to
in-memory state for development.

Tests: `pytest -q`. Routing evaluation: `python scripts/routing_eval/harness.py
--mode resolve` (decision layer, in-process) or `--mode chat` (end to end against
a running server); extend `scripts/routing_eval/cases.json` from real
misroutes rather than adding rules.

## Docker

The image is built in three stages (Node bundles `web/*.ts`, Python resolves the
pinned dependencies, a non-root `python:3.11-slim` runtime serves `app.main:app`
with a `/health` check). Runtime state lives in the `/app/data` volume; `.env`
never enters the image.

```bash
docker compose up --build -d    # api + postgres + redis, memory fallback disabled
curl -s localhost:8000/health
```

### Production

`docker-compose.prod.yml` runs the CI-built image (`ghcr.io/sanjayarun07/orbit:main`,
or `ORBIT_IMAGE_TAG=sha-<commit>` to roll back) behind Caddy with automatic TLS.
Set `POSTGRES_PASSWORD`, `ADMIN_API_KEY` and `ORBIT_DOMAIN` in `.env`, point the
domain at the host, then:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Only Caddy is exposed (80/443). Update with `pull` + `up -d api`; in-flight
requests drain. Keep `UVICORN_WORKERS=1` per container and scale with replicas.
CI builds and smoke-tests the image on every push and publishes it from `main`.
Release gates, credential rotation and reconciliation guidance are in
`docs/production-operations.md`.

## Use Orbit from Claude or ChatGPT (MCP)

Orbit is also an MCP server with the same surface as the web UI, so any agent
host can use it as a skill. The server is mounted at `/mcp` (Streamable HTTP)
and can also run over stdio. Four families of tools:

- **Conversation and session** — `orbit_chat` (the same turn as the web chat),
  `orbit_connect_wallet`, `orbit_prepare_swap`, `orbit_trade_simulation`,
  `orbit_set_team_mode`, `orbit_set_risk_charter` / `orbit_clear_risk_charter` /
  `orbit_risk_charter_limits` / `orbit_policy`, `orbit_history`,
  `orbit_delete_history`, `orbit_handoff_url`.
- **Analytics** — `orbit_portfolio`, `orbit_wallet_health`,
  `orbit_portfolio_scenario`, `orbit_market_overview`, `orbit_token_deep_dive`,
  `orbit_trade_plan`, `orbit_execution_status`, `orbit_relay_status`,
  `orbit_capabilities`, `orbit_route_preview`, `orbit_health`.
- **Data sources** — one `orbit_data_<tool>` per read-only provider tool
  (36 today), each running through the router's quotas, circuit breakers,
  cache, budget and outcome accounting with the request→tool matchers
  bypassed because the host chose the tool; plus `orbit_mcp_catalog` /
  `orbit_mcp_call` for tools Orbit discovers from its own MCP servers
  (execution-risk tools are refused).
- **Resources and prompts** — `orbit://skill`, `orbit://capabilities`,
  `orbit://health`, `orbit://history/{session_id}`, `orbit://policy/{session_id}`;
  prompts `orbit_skill`, `orbit_deep_dive`, `orbit_market_brief`.

The playbook in `skills/orbit/SKILL.md` is served as the server's instructions
and describes every tool and the wallet flow.

**Claude Code**

```bash
claude mcp add --transport http orbit https://orbit.yourdomain.com/mcp \
  --header "Authorization: Bearer $MCP_API_KEY"
```

**Claude Desktop** (`claude_desktop_config.json`, stdio, runs Orbit locally
from this checkout with its `.env`):

```json
{ "mcpServers": { "orbit": { "command": "/path/to/orbit/.venv/bin/python", "args": ["-m", "app.mcp_server"], "cwd": "/path/to/orbit" } } }
```

**ChatGPT**: Settings → Connectors → add a remote MCP server with the URL
`https://orbit.yourdomain.com/mcp`. ChatGPT's connectors authenticate with
OAuth or run without auth; a bearer key is not a supported option there, so
either leave `MCP_API_KEY` unset on a deployment reserved for that connector or
put an OAuth-issuing proxy in front of `/mcp`.

**How wallets work from an agent host.** The host has no wallet and must never
hold keys, so Orbit separates knowing a wallet from signing with it:
`orbit_connect_wallet` binds a *public* address to the session for read-only
analysis; any turn that produces a quote returns a `handoff_url`
(`PUBLIC_BASE_URL/ui/?session=<id>`) that opens the very same session in the
web UI, where the user connects Phantom, Coinbase Wallet, MetaMask, Privy or
WalletConnect, reviews the card and confirms — the wallet signs, Orbit does
not. Quotes expire, so the UI offers a fresh one if the hand-off was slow. The
conversation can never sign, submit, raise a cap or bypass the risk charter.

## Configuration

Everything is environment-driven; `.env.example` documents every variable.
The groups that matter most:

| Group | Variables |
|---|---|
| Model | `MODEL`, `OPENAI_API_KEY`, `LLM_FALLBACK_MODEL`, `INTENT_MODEL` |
| Trading caps | `MAX_TRADE_USD`, `MAX_SLIPPAGE_BPS`, `MAX_PRICE_IMPACT_PCT`, `PLAN_TTL_SECONDS`, `LIVE_TRADING` |
| Providers | `PERPLEXITY_API_KEY`, `BIRDEYE_API_KEY`, `MOBULA_API_KEY`, `BITQUERY_API_KEY`, `GOLDRUSH_API_KEY`, `HELIUS_API_KEY`, `NANSEN_API_KEY`, `ROOTDATA_API_KEY`, `DUNE_API_KEY`, … |
| Wallets | `PRIVY_APP_ID`, `PRIVY_CLIENT_ID`, `REOWN_PROJECT_ID`, `EVM_AUTH_RPC_URLS`, `SOLANA_RPC_URL` |
| Payments | `X402_ENABLED`, `X402_PAY_TO`, `X402_NETWORK`, `X402_PRICE`, `X402_FACILITATOR_URL` |
| Budgets | `MAX_EXTERNAL_CALLS_PER_TURN`, `MAX_PAID_DATA_COST_USD_PER_TURN`, `PROVIDER_*` quotas and weights, `PROVIDER_OUTCOME_WEIGHT` |
| State & ops | `DATABASE_URL`, `REDIS_URL`, `ALLOW_MEMORY_FALLBACK`, `ADMIN_API_KEY`, `TRUSTED_PROXY_HOSTS`, `FORWARDED_ALLOW_IPS`, `CLICKHOUSE_*`, `LANGFUSE_*` |

Credentialed providers stay visible but unavailable in the admin catalog until
their keys exist; keys are never returned by any API or rendered in the UI.

## Accounts, credits and API keys

Orbit is metered in **credits**. Every finished chat turn is charged by what
it did — a plain answer costs 1, a turn that ran data tools 2, a trade
preparation 3, a token deep-dive or trading-desk turn 5 (all configurable via
`CREDIT_COST_*`). The charge is reserved before the turn and settled after
it, so a failed turn costs nothing. Every change to a balance is a row in an
append-only ledger keyed by `(account, ref_type, ref_id)`, which is what makes
grants idempotent: a redelivered payment webhook or a monthly allowance
applied twice inserts nothing.

| Tier | Price | Credits | Includes |
|---|---|---|---|
| Trial (anonymous) | — | 10, once, per device/IP | Research and market data only |
| Free | $0 | 100 / month | History, wallet connection, trade review, risk charter |
| Pro | $19 / month | 2,000 / month | API keys, MCP access for Claude / ChatGPT, trading desk |
| Max | $49 / month | 7,500 / month | Higher rate limits, priority support |

**Sign-in** is an emailed one-time link (Resend; without `RESEND_API_KEY` in
development the link is returned to the UI and used automatically). Wallets
are linked to the signed-in account — history, portfolios, wallet connection
and trades all require sign-in; anonymous visitors can research on the trial.

**API keys** (`orb_live_…`, shown once, SHA-256 stored) authenticate `/chat`
and the MCP server as the owning user with scopes `chat`, `data`, `mcp`; they
never grant execution. Manage them under Profile → API keys.

**Settings** (Profile → Settings) has six tabs — Account (email, linked
wallets, signed-in devices, sign out everywhere, team invitations), Billing
(plan, credits, 30-day usage chart by feature, invoices, Portal, buy credits),
API keys (create / scope / revoke, usage per key), Preferences (display name,
risk preference, saved risk charter that seeds every new conversation, default
wallet, theme — stored on the account), Notifications (low-credit email with a
threshold, receipts) and Data & privacy (JSON export, delete all conversations,
delete account with typed confirmation). The Max plan adds **Members**: up to
five seats sharing the owner's plan and credit pool, invited by email.

**Payments** go through Stripe-hosted pages only — Orbit never sees a card or
wallet. `POST /billing/checkout` opens Checkout for a plan (subscription mode)
or a one-time **credit pack** (500 / 2,000 / 10,000 credits; payment mode),
`POST /billing/portal` opens the Customer Portal (upgrade, cancel, invoices),
and `POST /billing/webhook` applies verified events idempotently: pack
purchases credit the ledger, `invoice.paid` grants the plan's monthly
allowance per invoice, subscription updates/deletions move the plan, and
`charge.refunded` claws pack credits back pro rata — each keyed by the Stripe
event id. Cards are always accepted; set `STRIPE_CRYPTO_ENABLED=true` once
Stripe's USDC payment method is active for your entity. Everything works
against test-mode keys; without keys the billing endpoints answer 503 and the
UI shows the catalog read-only.

| Endpoint | Purpose |
|---|---|
| `POST /auth/email/start`, `POST /auth/email/verify` | Magic-link sign-in; sets the `orbit_user` cookie |
| `GET /me`, `POST /auth/signout` | Current account, plan, credits, linked wallets |
| `GET /me/credits` | Balance, per-turn costs and the recent ledger |
| `PUT /me/preferences` | Display name, risk preference, default wallet, theme |
| `GET/POST/DELETE /me/api-keys` | List, create (secret shown once), revoke |
| `GET /billing/plans` | The plan catalog with entitlements |
| `GET /me/usage`, `GET /me/invoices`, `GET /me/sessions`, `POST /me/sessions/revoke-all` | Credits by day/feature/key, Stripe invoices, signed-in devices, sign out everywhere |
| `GET /me/export`, `DELETE /me/conversations`, `DELETE /me` | Data export (JSON), delete all conversations, delete the account (ledger kept anonymously) |
| `GET /me/team`, `POST /me/team/invites`, `DELETE /me/team/members/{email}`, `POST /me/team/accept`, `POST /me/team/leave` | Team seats on Max: members share the owner's plan and credit pool |
| `POST /billing/checkout`, `POST /billing/portal`, `POST /billing/webhook` | Stripe Checkout (plan or credit pack), Customer Portal, verified webhook intake |

## API surface

| Endpoint | Purpose |
|---|---|
| `POST /chat` | One turn: message, optional wallet, session, team-mode switch, risk-charter fields |
| `GET /chat/history/{session_id}` | Display messages plus the canonical context snapshot |
| `GET /chat/risk-charter/limits` | Built-in caps and chains the charter card may use |
| `POST /trade-plans/{plan_id}/confirm` | Confirm a quoted plan (wallet-signed; server signing only with `LIVE_TRADING`) |
| `GET /wallet-health/{wallet}`, `GET /portfolio/{wallet}/scenario` | Deterministic wallet diagnostics and price-shock simulation |
| `GET /executions/solana/{signature}` | Track a submitted Solana transaction |
| `GET /capabilities`, `GET /health` | Provider catalog and health; aggregate counters incl. estimated spend |
| `GET /admin/providers`, `PUT /admin/providers/{tool}` | Enable/disable tools, override priority, quota, cost |
| `GET /admin/tools/outcomes` | Learned per-tool success rates and ranking adjustments |
| `POST /admin/intents/preview` | Routing lab: intent, capabilities, chain evidence for a request |

Chat responses carry the selected intent and capabilities, context capsules, an
evidence summary, validation results, risk assessment, trade readiness and gas
guidance, and — when enabled — an **Agent activity** trace of the tools called.

## Operating notes

- Scrape `/health` per replica; provider events and per-turn budgets stream to
  ClickHouse when `CLICKHOUSE_HOST` is set, and traces to Langfuse when its keys
  are set.
- The two reconciliation workers (Jupiter submissions, Relay executions) restart
  with the app, never sign and never rebroadcast; pin them to one replica at
  larger scale.
- Do not expose `/rpc/solana` without an upstream gateway; per-client request,
  body and batch limits still apply.
- Rotate any credential that was ever pasted, logged or committed;
  `scripts/check_secrets.py` runs as a pre-commit hook and in CI.

## Documentation

- `docs/routing-architecture.md` — precedence, the speech model, execution
  invariants, outcome-scored tools.
- `docs/production-operations.md` — release gates, credential rotation,
  repository protections.
- `scripts/routing_eval/` — the labelled routing set and harness.

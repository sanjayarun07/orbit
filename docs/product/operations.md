# Setup and operations

[Documentation home](README.md)

## Local setup

Python metadata supports 3.10–3.13; the container uses Python 3.11 and Node 22. PostgreSQL and Redis are recommended even for realistic local testing. Use UTF-8 database encoding. These instructions do not copy or reveal an existing `.env`.

For a new checkout only, copy `.env.example` to `.env` and edit it locally. Do not overwrite an existing configuration. Configure an answer-model credential, database URLs, and the provider credentials needed for the intended workflows.

Install the Python dependencies using the same manifest-based approach as the Docker build:

```bash
python3.11 -m venv .venv
.venv/bin/python -c 'import tomllib; from pathlib import Path; p=tomllib.loads(Path("pyproject.toml").read_text())["project"]; Path("/tmp/orbit-requirements.txt").write_text("\n".join(p["dependencies"] + p["optional-dependencies"]["dev"]))'
.venv/bin/python -m pip install -r /tmp/orbit-requirements.txt
npm ci
npm run build:web
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/ui/`. API documentation is at `/docs`, `/redoc`, and `/openapi.json`. Run commands from the repository root so the application and configuration files resolve consistently.

For the bundled local infrastructure and application:

```bash
docker compose up --build -d
curl --fail http://localhost:8000/health
```

Compose requires `POSTGRES_PASSWORD` in `.env`, overrides the application database/cache URLs to the Compose services, and disables memory fallback. It builds the wallet bundles inside the image. Schema creation/migration statements run through application database initialization; this repository does not use a separate Alembic migration tree.

## Configuration by purpose

All names below are environment variables. [Settings](../../app/settings.py) defines defaults; [.env.example](../../.env.example) supplies deployment examples. The private `.env` is not part of this documentation.

| Purpose | Main variables | Operating note |
|---|---|---|
| Answers and classification | `MODEL`, `INTENT_MODEL`, `OPENAI_API_KEY`, `LLM_FALLBACK_MODEL`, `LLM_REQUEST_TIMEOUT_SECONDS` | Configure credentials appropriate to the chosen model provider |
| Tool arbitration | `LLM_TOOL_SELECTION_ENABLED`, `TOOL_SELECTOR_MODEL`, `TOOL_SELECTOR_API_BASE`, `TOOL_SELECTOR_API_KEY` | Optional; disabled by default; independently configurable endpoint |
| Embeddings | `INTENT_EMBEDDING_*`, `TOOL_EMBEDDING_*`, `KNOWLEDGE_EMBEDDING_*` | Three distinct uses; changing one does not migrate the others |
| State | `DATABASE_URL`, `REDIS_URL`, `ALLOW_MEMORY_FALLBACK` | Use shared durable services and disable fallback in production |
| Trade policy | `LIVE_TRADING`, `MAX_TRADE_USD`, `MAX_SLIPPAGE_BPS`, `MAX_PRICE_IMPACT_PCT`, `PLAN_TTL_SECONDS` | Review provider-specific execution boundaries before enabling |
| Signing/RPC | `SOLANA_RPC_URL`, `SOLANA_PRIVATE_KEY`, `EVM_AUTH_RPC_URLS` | A private key enables a separate server-signing capability when other gates pass; not needed for browser signing |
| Browser wallets | `PRIVY_APP_ID`, `PRIVY_CLIENT_ID`, `REOWN_PROJECT_ID` | Optional wallet SDK configuration; public identifiers are exposed intentionally |
| Providers | Provider-specific API keys, URLs, quotas and cost settings | Catalogue registration does not imply valid credentials or paid access |
| Account/email | `PUBLIC_BASE_URL`, `RESEND_API_KEY`, `EMAIL_FROM`, `DEV_EXPOSE_MAGIC_LINKS` | Configure delivery and disable development link exposure for production |
| Billing | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_*` | Checkout/catalogue must match configured Stripe prices |
| Optional x402 | `X402_ENABLED`, `X402_PAY_TO`, `X402_NETWORK`, `X402_PRICE`, `X402_FACILITATOR_URL` | Separate HTTP-payment layer; default off, default network Base Sepolia |
| Access | `ADMIN_API_KEY`, `MCP_API_KEY`, `MCP_CONFIG_PATH`, `MCP_ENV_ALLOWLIST` | Treat service MCP access as privileged, not a normal metered user account |
| Capacity | `MAX_CONCURRENT_CHAT_REQUESTS`, `MAX_CONCURRENT_LLM_REQUESTS`, `CHAT_EXECUTION_TIMEOUT_SECONDS`, `REQUEST_QUEUE_TIMEOUT_SECONDS` | Per-process limits do not establish a cluster-wide capacity bound |
| Budgets | `MAX_EXTERNAL_CALLS_PER_TURN`, `MAX_PAID_DATA_COST_USD_PER_TURN`, provider cost settings | Estimates must reflect real provider contracts to be meaningful |
| Knowledge | `KNOWLEDGE_REGISTRY_LIMIT`, `KNOWLEDGE_DOCS_PAGE_BUDGET`, `KNOWLEDGE_INGEST_ENABLED`, `KNOWLEDGE_RERANKER` | Start with bounded ingestion and inspect coverage/errors |
| Telemetry | `CLICKHOUSE_*`, `LANGFUSE_*`, `EXPOSE_TOOL_TRAJECTORY` | Optional; keep raw client trajectories disabled unless intentionally debugging |
| Local persisted files | `ROLE_MEMORY_PATH`, `PROVIDER_OVERRIDES_PATH` | Ensure writable persistence and consistent configuration across replicas |

### Self-hosted tool selector

The current adapter supports a separate OpenAI-compatible endpoint through LiteLLM/DSPy. A configuration template is:

```dotenv
LLM_TOOL_SELECTION_ENABLED=false
TOOL_SELECTOR_MODEL=hosted_vllm/your-served-model-name
TOOL_SELECTOR_API_BASE=https://your-model-host.example/v1
TOOL_SELECTOR_TIMEOUT_SECONDS=6
```

Supply `TOOL_SELECTOR_API_KEY` through the local/deployment secret mechanism. These names describe the repository's adapter contract; compatibility with a particular serving stack must be tested. Configuring a model does not automatically enable arbitration. Validate the endpoint separately, evaluate routing quality, then deliberately enable the flag.

### Provider families

- Market/token discovery and pricing: CoinGecko, DexScreener, GeckoTerminal, Birdeye, Mobula, CoinMarketCap, and other registered handlers.
- On-chain/wallet analytics: Bitquery, GoldRush, Helius, Solana RPC, and configured outbound MCP tools such as Nansen.
- Security: Jupiter Shield, GoPlus, Honeypot.is, and contextual sources.
- DeFi/protocol data: DefiLlama and the Knowledge Base; Dune for supported analytical queries.
- Web/company/project research: Perplexity, OpenAI web search, RootData, and registered specialist tools.
- Sentiment/events: LunarCrush or X credentials where available, web-research fallback paths, calendar composition.
- Execution: Jupiter, Relay, and LI.FI quote/status backup.

Use `/capabilities` and the admin provider view for the actual registered/available surface. Consult [the generated tool catalogue](../tool-catalog.md) for per-tool requirements and exclusions, rather than assuming every provider supplies every capability.

## Knowledge Base preparation

1. Start the API and connect to the Knowledge Base administration page.
2. Bootstrap the registry with a bounded protocol count. An empty registry is not a populated corpus.
3. Ingest a selected protocol and inspect documents, chunks, source failures, and citations.
4. Run larger ingestion in a separate checkout/process sharing the database.
5. Review retrieval quality before claiming coverage for incidents, funding, or governance.

```bash
.venv/bin/python scripts/kb_ingest.py --slug aave-v3
.venv/bin/python scripts/kb_ingest.py --derive
.venv/bin/python scripts/kb_ingest.py --loop --parallel 3
```

These commands access external sources and can incur embedding/provider costs. Check the registered slug before using it. `--slug` forces that protocol's sources regardless of timers; loop mode runs due work. Default code also has an optional in-process ingestion worker, but leave it disabled when using a dedicated ingester.

The image ships `scripts/`, so the ingestion command runs inside the API container (`docker compose ... exec api python scripts/kb_ingest.py ...`). The supplied Compose files do not declare a separate ingestion service; run it by hand, once, and to refresh.

The API refreshes its in-memory entity resolver about every two minutes. pgvector is optional; the bundled PostgreSQL image uses the fallback unless the extension is separately supplied. Monitor search quality and latency as the corpus grows. Hashing embeddings are for offline/development use, not equivalent semantic retrieval quality.

## Deployment

The multi-stage Docker image bundles browser SDKs, installs Python dependencies, then runs as a non-root user. Application state files use `/app/data`. The base Compose stack runs API, PostgreSQL, and Redis with named volumes.

The production overlay uses the repository's configured GHCR image and adds Caddy TLS. Set `ORBIT_DOMAIN`, DNS, secrets, and `PUBLIC_BASE_URL`; use a Compose release supporting the overlay's `!reset` tags.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Keep `UVICORN_WORKERS=1` per container initially. Prefer an immutable `ORBIT_IMAGE_TAG=sha-<commit>` for a recorded release or rollback. Application rollback does not automatically undo schema/data changes.

The production overlay exposes Caddy rather than the API port. Its forwarded-header trust assumes the private network is appropriately restricted. Review the RPC proxy's allowed methods, body/batch limits, and upstream gateway configuration before public deployment. Do not expose an unconfigured service-mode MCP endpoint as if it had normal user metering and ownership semantics.

## Monitoring and recovery

| Signal | What to investigate |
|---|---|
| `/health` available but answers failing | Provider/model credentials, quotas, timeouts, budget exhaustion; an HTTP health response is not proof every dependency works |
| More 409 responses | Concurrent chat turns, stale revisions, expired/replaced approval context |
| More 402 responses | Credit balance versus optional x402 payment requirements; distinguish their response bodies |
| Provider circuit openings or repeated fallback | Provider errors, quota settings, wrong tool selection, stale cache assumptions |
| Old `submitting` / `submission_unknown` records | Check stored signature and chain/provider evidence; do not blindly rebroadcast |
| Missing scheduled results | Worker activity, next-run time, UTC offset, claim lease, balance, email configuration |
| Missing history | Redis TTL/eviction/restart and browser-local registry; ownership rows are not a transcript backup |
| Weak knowledge answers | Ingestion errors, missing sources, dimensions/model mismatch, unresolved entities, absent citations |

Back up PostgreSQL execution, account, ledger, task, and knowledge records; preserve Redis appropriately for its intended retention; retain configured local state files. Test restoration, not just backup creation. Never purge unresolved execution records to clear a dashboard.

Jupiter recovery requires persisted signatures and finalized chain evidence. Relay recovery requires the original request ID and provider status. Both workers are read-only with respect to chain submission. Old records lacking the required identifiers need manual investigation.

Billing events are signature-verified and credit effects use idempotent references. Admin replay exists for recovery. Inspect event processing status and the ledger before replaying, and do not manually grant credits as a substitute for understanding an incomplete event.

The default task worker claims occurrences before evaluation; a stale 15-minute lease can be recovered. Idempotent billing does not prove exactly-once email delivery. Observe delivery and worker errors separately.

## Troubleshooting quick reference

| Symptom | Next check |
|---|---|
| Admin connection returns 503 | `ADMIN_API_KEY` missing |
| Admin connection returns 401 | Bearer key absent or incorrect |
| Email link appears directly in local UI | No email provider plus development exposure enabled |
| Purchases unavailable | Stripe secret/price configuration and catalogue `purchasable` state |
| Wallet preparation rejects execution | `LIVE_TRADING`, plan state/expiry, confirmation text, simulation |
| A provider is listed but unavailable | Credential or eligibility requirement; catalogue visibility is not readiness |
| Self-hosted selector unavailable | Endpoint reachability, served model name, credential, timeout; deterministic ordering remains the fallback |
| Ingestion encoding error | PostgreSQL database should be UTF-8 |
| Root-level pytest aborts during collection | Use `pytest -q tests`; see the live smoke-script finding in [verification](development.md) |

Source: [settings](../../app/settings.py), [Dockerfile](../../Dockerfile), [Compose](../../docker-compose.yml), [production overlay](../../docker-compose.prod.yml), [ingester](../../scripts/kb_ingest.py), [production gates](../production-operations.md).

## Closed beta: free, unlisted, on a VPS you control

The decision of 2026-09-17: no payments, every signed-in account on the free
Beta plan, open (unlisted) sign-up, execution on with signing only in the
user's own wallet. Everything below is one env file on top of the existing
compose stack. (The template lives under `deploy/`, not as a `.env.*` file: the secrets scan treats any tracked `.env.*` as live configuration, by design.)

**What the flag does.** `CLOSED_BETA=true` makes `billing.configured()`
answer false whatever Stripe settings exist (every billing endpoint answers
503, account deletion has no subscription to cancel), puts every signed-in
account on the `beta` plan (`CLOSED_BETA_MONTHLY_CREDITS`, default 5,000,
API keys, MCP, the trading desk; the anonymous trial stays the trial), and
tells the browser through `/config/public`, which hides every price and
purchase control and turns the plans dialog into a note. The monthly grant is
keyed by plan id, so a tester who already drew Free's allowance this month
gets the beta one at once. Off by default; nothing changes for other setups.

**Bring-up, in order.**

1. Host: a VPS with Docker, DNS for `ORBIT_DOMAIN` pointing at it, ports 80
   and 443 open. Caddy obtains the certificate.
2. `cp deploy/beta.env.example .env` and fill every value marked with a comment. `SOLANA_PRIVATE_KEY`
   must be absent. The template passes the production audit once filled;
   the audit refuses to boot on any unsafe value and names them all.
3. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`.
   Postgres is the pgvector image (the knowledge base stores native vectors
   with it); Redis is `noeviction`; the API is read-only, non-root, behind
   Caddy only.
4. `curl -s https://$ORBIT_DOMAIN/readyz | jq` -- `status: ready`, no
   `failed`; `degraded` may list `knowledge_snapshot` until step 5.
   `knowledge_ingest` reads `disabled (KNOWLEDGE_INGEST_ENABLED=false)`, ok:
   the corpus is loaded by hand (step 5) and refreshed by re-running that
   load, not by the background worker.
5. Knowledge base: bulk-load once from the host, in its own process --
   `docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python scripts/kb_ingest.py --parallel 4 -v`
   (see "Knowledge Base preparation" above). On a fresh database the
   script bootstraps the registry itself (top 50 protocols by TVL from
   DefiLlama) before crawling; expect tens of minutes and a few dollars of
   embedding calls. Then `... --derive` for the competitor/integration
   edges. The snapshot warms within two minutes and `knowledge_snapshot`
   reports its entity count.
6. Sign in with your own email, confirm the magic link arrives, confirm the
   plan reads Beta with 5,000 credits, confirm the plans dialog shows the
   beta note and no prices. Then share the URL with the group.

**What to watch during the beta.** `/readyz` for degraded checks; the admin
page's accounts and credit usage (a runaway tester shows up as credit burn);
`research_gaps` for turns that fell through to web search -- that log is the
next round of routing-eval cases (`scripts/routing_eval/collect.py --gaps 7`).
Provider bills: the per-turn paid-data cap and the per-IP trial budgets stay
on precisely because sign-up is open.

**To turn swaps off** at any point: `DEPLOYMENT_MODE=research` and
`LIVE_TRADING=false`, restart. The browser hides the swap surfaces from
`/config/public`, and the server refuses the routes.


## Staging: the production stack, rehearsed

Staging is the same image, compose files and gates as production, on its
own host, hostname and stores, with `ENVIRONMENT=staging` (audited exactly
like production) and swaps off until they are rehearsed there. Nothing
below is staging-only except the values in `deploy/staging.env.example`.

**Before the first deploy (once).**

1. CI on `main` is green and the image job ran: `gh run list --limit 1`
   shows both jobs passed, and `ghcr.io/sanjayarun07/orbit:sha-<commit>`
   exists for the commit you are deploying (`gh api
   /users/sanjayarun07/packages/container/orbit/versions`). Nothing is
   deployed that CI did not build.
2. If the GHCR package is private, the host needs `docker login ghcr.io`
   with a token that has `read:packages`; or make the package public in the
   repository's package settings.
3. A host with Docker Engine and Compose v2 (the production overlay uses
   `!reset`, Compose 2.24+), 2 vCPU / 4 GB minimum (the API container is
   capped at 2 GB and runs two workers), ports 80 and 443 open, and a DNS
   record for the staging hostname pointing at it. Caddy obtains the
   certificate on first start.

**Deploy.**

```bash
git clone https://github.com/sanjayarun07/orbit && cd orbit      # the compose files, Caddyfile and scripts
cp deploy/staging.env.example .env && $EDITOR .env                # fill every <...>; generate secrets with: openssl rand -hex 32
export ORBIT_IMAGE_TAG=sha-<commit>                               # or set it in .env; the commit CI built
python3 -m venv .venv && .venv/bin/pip install -q -e . && .venv/bin/python scripts/preflight.py .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
curl -s https://$ORBIT_DOMAIN/readyz | jq '{status, failed, degraded, config_warnings}'
```

The preflight runs the startup audit on the file and exits 1 on anything
fatal (it never prints a value). `/readyz` must say `ready` with `failed`
empty; `degraded` may list `knowledge_snapshot` until the knowledge base
is loaded (below) and `polymarket` where the host's network cannot reach
it. If the API refuses to start, `docker compose logs api` names every
fatal setting at once.

**Then, in order.**

4. Load the knowledge base once, in its own process:
   `docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python scripts/kb_ingest.py --parallel 4 -v`
   then `... --derive`. Tens of minutes and a few dollars of embeddings;
   `knowledge_snapshot` reports its entity count within two minutes.
5. Sign in with your own email; confirm the magic link arrives from the
   staging sender, the plan reads Beta, and the plans dialog shows the beta
   note. Connect a wallet (Phantom on Solana, MetaMask on EVM) and ask for
   its portfolio.
6. Run the prompt sets against staging and read the turn log:
   `reports/meme-live-20260918/cases.json` (22 meme asks),
   `reports/uncertain-live-20-gated/` (20 ambiguous asks), then
   `.venv/bin/python scripts/turn_review.py` on the host, or the admin page's
   Turns panel. Every wrong answer found here is fixed before the same
   commit goes to production.
7. Only after 5 and 6 pass: rehearse swaps. Set `DEPLOYMENT_MODE=execution`
   and `LIVE_TRADING=true` (both, or the audit refuses), keep
   `MAX_TRADE_USD=5`, `docker compose ... up -d`, and quote, sign in your
   own wallet, and confirm one small Jupiter swap and one Relay swap. The
   server never holds a key: `SOLANA_PRIVATE_KEY` stays absent.

**Upgrade and roll back.** Every change is a new pinned tag:
`ORBIT_IMAGE_TAG=sha-<new commit>`, then `pull` and `up -d`; Compose
replaces the API container and Caddy keeps serving. Roll back by setting
the previous tag and repeating. Schema statements run at startup and are
additive (`CREATE ... IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`), so an
older image starts against a newer schema; data written by a newer
feature is simply unused.

**Promote to production.** The same tag that passed on staging, with
`deploy/beta.env.example` (execution on, $20 cap, the production hostname
and its own secrets) on the production host. Never a tag staging did not
run.

**What staging is not.** It shares no store with production: its own
Postgres, Redis and volumes, its own admin and MCP keys, its own provider
keys where the provider offers a second key (Mobula, TwitterAPI.io and
Perplexity bill per call: a staging prompt sweep spends real money).
Retention, the turn log and the holder ledger all run there as in
production; delete test accounts through the app (`DELETE /me`), which
exercises the scrub, rather than in the database.

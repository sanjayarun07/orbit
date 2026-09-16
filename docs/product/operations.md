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

**Container packaging limit:** the current Dockerfile copies application code and skills, but not `scripts/`. The standalone ingestion command therefore requires the source checkout or a worker image/package that includes the script. The supplied Compose files do not declare a separate ingestion service.

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

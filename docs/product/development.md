# Development and verification

[Documentation home](README.md)

## Repository map

| Path | Purpose |
|---|---|
| `app/main.py` | HTTP routes, mounts, middleware, worker lifecycle |
| `app/models.py`, `app/settings.py` | Typed contracts and configuration defaults |
| `app/execution_policy.py`, `app/session_access.py` | Shared conversation policy and ownership |
| `app/task_scheduling.py`, `app/tasks.py` | Shared task access/validation and durable scheduling |
| `app/graph.py`, `app/nodes/`, `app/routing/` | Graph orchestration, node behavior, resolution and selection |
| `app/provider_registry.py`, `app/tool_catalog.py` | Tools and their documented answer boundaries |
| `app/knowledge/` | Ingestion, entities, storage, retrieval, graph and citations |
| `app/static/` | Main UI, administration shell, CSS and generated browser bundles |
| `web/` | TypeScript wallet/payment/executor sources |
| `tests/` | Python regressions and browser-executor tests |
| `scripts/` | Live sweeps, routing evaluation, ingestion, provider verification and catalogue generation |
| `skills/orbit/` | MCP-facing product playbook |
| `.github/workflows/tests.yml` | Test, image build, health smoke test and conditional image publishing |

## Adding a read-only provider tool

1. Implement a bounded handler that returns the expected tool-result shape. Distinguish no data, unsupported input, partial data, and provider failure.
2. Register its `ProviderTool` with capability, chain coverage, matcher, availability check, cost/quota defaults, and description.
3. Add a `ToolSpec` in `tool_catalog.py`: API surface, inputs, returned fields, supported dimensions, exclusions, and realistic right/wrong examples.
4. Add tests for the matcher, output shape, wrong-chain/instrument handling, failure behavior, and competing-tool selection. Extend the labelled routing cases when the new tool overlaps existing vocabulary.
5. Exercise the deterministic route first. Evaluate embedding admission and optional model arbitration separately.
6. Regenerate `docs/tool-catalog.md` with `python scripts/tool_catalog_doc.py` from an environment where the application imports correctly.
7. Check `/capabilities`, admin preview, and MCP tool discovery. Do not assume automatic registration means the tool has valid credentials.

Catalogue claims should match the handler's actual API, not a provider's wider marketing capabilities. A paid-promotions list must not masquerade as an organic volume ranking. A token price endpoint must not be ranked as wallet transaction history.

## Adding knowledge sources

Implement the connector interface under `app/knowledge/connectors/`, including applicability, discovery/fetch or document generation, and refresh policy. Produce normalized documents with source URLs and meaningful metadata. Use structured facts for relationships supported directly by source fields; preserve provenance and confidence.

Verify content-hash idempotency, changed-document versioning, non-ASCII text, entity resolution, retrieval/citation output, and bounded source failures. Add global sources once per pass rather than redundantly per protocol. If using a new embedding model or dimensionality, plan re-embedding and schema compatibility rather than only editing a setting.

## Adding a workflow or external action

Keep HTTP and MCP as adapters. Shared admission, billing, ownership, and persistence belong in services, not duplicate transport implementations. Add typed workflow events/state and clear error contracts before adding UI shortcuts.

For an external action, establish a reviewable plan, expiry, explicit approval, durable claim/idempotency key, provider identifier, and reconciliation strategy. Distinguish preparation, submission, acceptance, and completion. An uncertain network result is not permission to repeat a side effect.

Browser execution adapters implement prepare/execute/status, but each provider still needs its own validation and recovery contract. Reusing the interface does not make two providers equally safe or interchangeable.

## UI maintenance

Edit handwritten UI in `app/static/index.html`, `admin.html`, and the corresponding styles. Edit SDK integrations in `web/*.ts`, then rebuild the relevant bundle or run `npm run build:web`. Do not manually patch minified generated JavaScript as the source of a feature.

Keep labels consistent across the main and admin shells. Preserve keyboard interactions, mobile navigation, focus behavior, empty states, unavailable-provider states, and explicit action confirmation. Test conversation switches while a quote is loading: stale responses must not reactivate an old approval.

Sidebar metadata is currently browser-local. New server-side project/sharing features require backend data models and access controls; adding another local-storage field does not create account synchronization.

## Verification commands

Routine isolated suite:

```bash
.venv/bin/pytest -q tests
npm run check:relay
node --test tests/browser-executors.test.cjs
```

The PostgreSQL integration is opt-in. Configure `TEST_DATABASE_URL` to a disposable PostgreSQL database, then run:

```bash
.venv/bin/pytest -q tests/test_postgres_execution.py
```

The test uses an isolated schema and exercises concurrent claims, restart recovery, finalization, and Relay uniqueness. Do not use a production database for QA.

Live prompt sweep:

```bash
.venv/bin/python scripts/e2e_sweep.py --base http://127.0.0.1:8000 --out /tmp/orbit-sweep.json
```

Run against an isolated instance with test accounts, an intentionally configured admin key, email delivery disabled as appropriate, and no funded transaction activity. The script creates/modifies account fixtures and calls real configured providers. Some scenarios require a populated Knowledge Base; a fresh database is not a sufficient fixture. Cached responses can also fail assertions that require a particular fresh tool name.

Routing evaluation:

```bash
.venv/bin/python scripts/routing_eval/harness.py --mode router
.venv/bin/python scripts/routing_eval/harness.py --mode resolve
.venv/bin/python scripts/routing_eval/harness.py --mode router --semantic --llm-select
```

The semantic/model modes can call paid external services. Record model, flags, corpus/provider availability, case-set revision, and output path. Existing saved scores are historical results, not guarantees for every deployed model or the latest code.

The standalone `scripts/tool_selector_smoke_test.py` is a **live endpoint diagnostic**, not an isolated unit test. Configure the endpoint deliberately and invoke it separately. See the collection finding below.

## Verification performed for this documentation

Baseline: `0dc35f6d`, 16 September 2026. No application code changed in this documentation pass.

| Check | Observed result |
|---|---|
| `.venv/bin/pytest -q tests` | **694 passed, 1 skipped**, one Starlette/AnyIO deprecation warning, 23.18 seconds |
| `npm run check:relay` | Passed |
| `node --test tests/browser-executors.test.cjs` | **6 passed**, including inline chat JavaScript parsing |
| Bare `.venv/bin/pytest -q` | Aborted during collection after importing the live tool-selector smoke script |
| PostgreSQL opt-in integration | Not rerun in this documentation pass; skipped without its test database |
| Live providers, payments, email, funded trades | Not certified by this documentation pass |

The earlier [UI/functionality QA report](../../reports/qa-2026-09-16/report.md) records live browser and API testing at older commits (`881133f`/`761f89b`). It is useful historical evidence, not validation of every later routing/catalogue change. Two UI issues were reproduced then: a clipped token suggestion menu and the generic response from the home Swap assets shortcut. Their full browser regression was not rerun here.

## Code findings that affect product claims or operation

| Finding | Evidence / consequence | Recommended follow-up |
|---|---|---|
| Root test discovery imports a live script | `scripts/tool_selector_smoke_test.py` matches pytest discovery and executes model calls / `sys.exit` at module import. Local bare pytest aborted; CI currently also invokes bare pytest | Add a main guard and/or explicit test discovery configuration; keep live diagnostics separate |
| Transcript retention is short | `sessions.py` sets two-hour TTL and 200 display messages; account ownership is a separate PostgreSQL record | Decide and implement the product's retention promise before advertising permanent history |
| Sidebar organization is local | Titles, pins, archives and projects use local storage | Add server persistence if cross-device/team organization is required |
| Optional server signing exists | `execution.py:execute_confirmed_plan` can sign with a configured key when gates pass | Keep deployment policy and public custody claims accurate; review whether this path is needed |
| Execution authorization differs by route/provider | Plan endpoints rely on plan/confirmation/signature checks rather than the same account dependency as account routes; Relay has browser-specific checks | Perform a focused authorization review before a public execution launch |
| In-depth entitlement is inconsistent with plan copy | Signed-in Free inherits `team_mode=True`; shared execution accepts mode updates | Resolve intended packaging and enforce it consistently across transports |
| Knowledge is deployment-wide | Public search/status/graph routes; no per-document tenant ACL layer | Do not ingest private multi-tenant business documents without new access controls |
| Task charging and delivery have different guarantees | Occurrence refs prevent duplicate brief charges; external notification delivery is not one atomic transaction | Add delivery idempotency/outbox semantics if exactly-once delivery is required |
| Bulk-ingester deployment is incomplete | Script exists in checkout but is not copied by Dockerfile; Compose has no dedicated worker | Package and supervise a worker for production ingestion |
| Health is shallow | `/health` returns status and counters, not an end-to-end dependency probe | Add separate readiness/dependency monitoring and alerting |
| Versions differ | API `0.3.0`, package `0.1.0`, finance-specific project package name | Establish one release/versioning convention |

These are implementation observations, not a full security audit or proof of exploitability. They are intentionally separated from features that work and from product proposals.

## Reusing the architecture for another business

Reusable components include transport adapters, shared execution policy, identity and credits, tool catalogue/routing, evidence retrieval, task claims, review/approval patterns, and reconciliation. Finance-specific parts include token/instrument resolution, market cards, risk-charter schema, Jupiter/Relay adapters, wallet identity, graph relationships, and many prompt examples.

A niche product such as a webhook investigator would need domain records, log/event connectors, an incident workspace, replay policy, and safe action semantics. An HVAC warranty product would additionally need document extraction, equipment/claim models, manufacturer connectors, and case deadlines. Neither is enabled by replacing labels alone. Tenant isolation, retention, and approval roles should be designed before bringing private business data into the existing shared Knowledge Base.

## Keeping these documents current

For a release, update the baseline commit and verification date; reconcile UI labels, routes, model settings, plan entitlements, and retention; regenerate the tool catalogue; re-extract the route inventory; and record the exact suites actually run. Never turn an older live-test result into an assertion about an untested release.

Review source comments critically. This pass found comments/prose that overstate permanent persistence, exclusive wallet signing, and lack of task claims. Executable code and current tests are the stronger evidence.

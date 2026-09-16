# API and integration guide

[Documentation home](README.md)

The browser and HTTP clients use FastAPI. An MCP server is mounted at `/mcp`; it reuses shared services rather than calling back through HTTP. The running API exposes `/docs`, `/redoc`, and `/openapi.json` for exact transport schemas. Models are defined in [models.py](../../app/models.py).

## Identity and access

| Mechanism | Purpose | Boundary |
|---|---|---|
| `orbit_user` cookie | Signed-in email account | Issued by email-token verification; used for account features |
| `Authorization: Bearer orb_live_…` | User API key | Has owner, plan checks, revocation, scopes; hash stored, secret shown once |
| `Authorization: Bearer <admin-key>` | Administration | Compared against `ADMIN_API_KEY`; separate from a user API key |
| `Authorization: Bearer <mcp-service-key>` | Trusted MCP deployment access | Shared service access when configured; not a per-user billing identity |
| `orbit_auth_session` cookie | Wallet authentication | Separate from email-account identity; challenge/signature proof |
| `x-orbit-device` header | Anonymous trial bucket | Client/device metering input, not an authentication credential |

API key scopes are `chat`, `data`, and `mcp`; checks are applied by the relevant handlers/services, not a universal OAuth permission system. A key never supplies a user's wallet signature. A member sharing an owner's paid plan should not assume every personal API-key check inherits that plan: `identity.resolve_identity` also checks the key owner's own stored plan.

Conversation ownership is enforced by `session_access`. Other users are given 404 rather than confirmation that an inaccessible conversation exists. The unowned-session path is different and is claimable on signed-in use. Account teams share billing, not automatic conversation access.

**Execution boundary:** plan read/confirm/wallet-transaction endpoints do not declare the same account dependency as `/me` or portfolio routes. They rely on plan state, exact confirmation material, wallet signatures, and execution settings as applicable. Do not describe all HTTP endpoints as authenticated or treat a plan ID as harmless public metadata. Review gateway/access controls for the intended deployment.

## Chat contract

`POST /chat` accepts:

| Field | Type | Meaning |
|---|---|---|
| `message` | string, 1–2,000 characters | User request |
| `session_id` | optional string | Continue a session; omit to create one |
| `context_revision` | optional nonnegative integer | Reject a stale client view with 409 |
| `wallet_address` | optional string | Wallet context; signed-in account required for normal HTTP callers |
| `team_mode` | optional boolean | Update In-depth/team-analysis mode |
| `tz_offset_min` | optional integer, −840…840 | Minutes east of UTC for scheduling |
| `risk_charter_fields` | optional object | Typed trade restrictions |
| `quick_action` | optional typed object | Must exactly match the server-persisted action/revision; currently newly generated lists are empty |

Minimal request:

```bash
curl --fail-with-body http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Explain what TVL means"}'
```

Authenticated API request, with a credential already supplied securely to the shell environment:

```bash
curl --fail-with-body http://localhost:8000/chat \
  -H "Authorization: Bearer $ORBIT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"How does Aave liquidation work?"}'
```

This uses credits and can call external services. On follow-up, send the returned session ID and revision:

```json
{
  "message": "What are its main risks?",
  "session_id": "<session-id-from-response>",
  "context_revision": 1
}
```

Always use the actual returned revision rather than assuming it is 1. Important response fields:

| Field | Consumer behavior |
|---|---|
| `answer`, `intent`, `capabilities` | Render the answer and declared workflow |
| `session_id`, `session_revision` | Store together; use on subsequent operations |
| `credits` | Display charged amount, kind, and balance when present |
| `trade_plan`, `cross_chain_swap` | Render provider-specific review flow; do not auto-execute |
| `context_capsules`, `intent_lock` | Explain subject/workflow and bind review context |
| `evidence`, `validation` | Display evidence/freshness/grounding caveats; validation is advisory |
| `risk_assessment`, `trade_readiness`, `gas_advisory` | Surface policy checks and execution considerations |
| `team_report`, `team_mode` | Render and synchronize In-depth state |
| `risk_charter`, `risk_charter_fields` | Synchronize canonical trade preferences |
| `trajectory` | Public activity or raw debug payload depending on configuration; do not rely on private tool internals |

## Common integration sequences

### Email account

1. `POST /auth/email/start` with `{ "email": "person@example.com" }`.
2. Follow the delivered one-time link; the UI verifies it through `POST /auth/email/verify` with `{ "token": "…" }`.
3. Retain the issued account cookie; use `GET /me` for plan, account and credit state.
4. `POST /auth/signout` ends the current account session. The wallet-auth logout endpoint is separate.

Production integrations must not depend on `dev_link`. It exists only for the configured development behavior.

### Reminder

With an authenticated account, `POST /me/tasks` accepts:

```json
{
  "kind": "reminder",
  "spec": { "message": "Review the weekly portfolio" },
  "schedule": { "daily": "09:00" },
  "channel": "inapp",
  "tz_offset_min": 330,
  "title": "Morning review"
}
```

Task creation returns 201. Other schedule forms are `at`, `every_minutes`, and `weekly` with day/time. Price alerts use a spec containing `symbol`, `<` or `>` as `op`, `price`, and optional chain/repeat. Briefs use kind `brief`. Model validation is followed by service-level schedule and plan-limit validation.

### Jupiter browser execution

Obtain a plan from chat → request `/trade-plans/{id}/wallet-transaction` with its exact `confirmation_text` → ask the matching wallet to sign → submit `confirmation_text` and `signed_transaction` to `/trade-plans/{id}/submit-wallet-transaction` → poll `/executions/solana/{signature}`.

This is a stateful, explicit-approval flow. Expired or superseded plans cannot simply be retried. If submission is uncertain, inspect status instead of issuing a replacement transaction. `/confirm` is the separate configured server-signer path, not the browser flow's generic approval endpoint.

### Knowledge retrieval

`GET /knowledge/search?q=…&limit=6` returns query, resolved entities, graph expansion, retrieval hits, and citations. Search/status/graph are public read routes in the current handlers. Bootstrap/ingest/derive require the admin key. Treat the current corpus as deployment-wide; per-organization private-document permissions are not implemented here.

## Errors and retries

Shared service errors become HTTP `{ "detail": ... }`; `detail` can be a string or object. Some middleware responses use an `error` field instead. Parse status and body rather than depending on one text string.

| Status | Typical meaning | Client response |
|---|---|---|
| 400 | Invalid confirmation, unsupported control, invalid provider/execution input | Correct input; do not automatically repeat execution |
| 401 | Sign-in/key/token required or invalid | Reauthenticate through the intended flow |
| 402 | Insufficient credits or optional x402 payment challenge | Distinguish credit and payment requirements |
| 403 | Plan/scope/role not allowed | Explain the missing entitlement |
| 404 | Missing/inaccessible conversation, task, or record | Avoid disclosing another user's resource existence |
| 409 | Stale revision, concurrent turn, superseded workflow | Refresh state; require a fresh review where relevant |
| 422 | Request schema validation failure | Correct payload against OpenAPI |
| 429 | Request or authentication rate limit | Respect `Retry-After` when supplied |
| 503 | Busy service, missing admin/billing configuration, unavailable dependency path | Check configuration/health; bounded retries for safe reads |

There is no documented universal idempotency header for chat. Repeating a submitted message may create another turn and charge. Billing, task occurrences, and execution claims have their own domain-specific idempotency mechanisms.

## MCP

Inbound MCP supports Streamable HTTP at `/mcp` and local stdio via:

```bash
.venv/bin/python -m app.mcp_server
```

User API keys must have MCP scope and an eligible plan; conversational tools also use the shared chat policy. A configured service key gates trusted service access. Leaving it unset does not create user authentication for service calls. Avoid presenting that configuration as a normal public paid-user interface.

Tool families include conversation/session controls; portfolio, market and execution-status analytics; dynamically registered `orbit_data_<tool>` read tools; and an outbound-MCP catalogue/proxy. Resources include `orbit://skill`, `orbit://capabilities`, `orbit://health`, `orbit://history/{session_id}`, and `orbit://policy/{session_id}`. Prompts include `orbit_skill`, `orbit_deep_dive`, and `orbit_market_brief`.

`orbit_handoff_url` gives the owning user a browser path to the same conversation for review and wallet interaction. It is not a public-share or automatic-authentication token. Hosts should use MCP discovery rather than a hard-coded tool count, because registered data tools evolve.

Outbound MCP is separate: `mcp_tools.py` discovers configured servers from `MCP_CONFIG_PATH` or `mcp.json`, filters risky execution tools, and applies its connection, call, timeout, and cache limits.

## HTTP route inventory

The inventory below is extracted statically from `app/main.py` at the documented commit. It covers declared application routes, not FastAPI's generated documentation routes, individual static assets, or the mounted MCP protocol. “Body” identifies the typed request model; raw-request handlers may parse JSON themselves. See each linked handler and the running OpenAPI document for query/path parameters and delegated access checks.

**94 declared HTTP routes.**

### Discovery, market and UI

| Method | Path | Body | Handler |
|---|---|---|---|
| `GET` | `/ui/knowledge.html` | — | [`knowledge_admin_page`](../../app/main.py#L172) |
| `GET` | `/health` | — | [`health`](../../app/main.py#L208) |
| `GET` | `/config/public` | — | [`public_config`](../../app/main.py#L355) |
| `GET` | `/home/highlights` | — | [`home_highlights_cards`](../../app/main.py#L403) |
| `GET` | `/calendar` | — | [`market_calendar`](../../app/main.py#L410) |
| `GET` | `/home/suggestions` | — | [`home_suggestions`](../../app/main.py#L417) |
| `GET` | `/tokens/search` | — | [`token_search`](../../app/main.py#L439) |
| `GET` | `/capabilities` | — | [`capabilities`](../../app/main.py#L465) |
| `GET` | `/portfolio/{wallet_address}` | — | [`portfolio`](../../app/main.py#L688) |
| `GET` | `/wallet-health/{wallet_address}` | — | [`wallet_health_report`](../../app/main.py#L700) |
| `POST` | `/portfolio/{wallet_address}/scenario` | `PortfolioScenarioRequest` | [`portfolio_scenario_report`](../../app/main.py#L710) |

### Authentication

| Method | Path | Body | Handler |
|---|---|---|---|
| `POST` | `/auth/coinbase/challenge` | `WalletAuthChallengeRequest` | [`coinbase_auth_challenge`](../../app/main.py#L213) |
| `POST` | `/auth/coinbase/verify` | `WalletAuthVerifyRequest` | [`coinbase_auth_verify`](../../app/main.py#L224) |
| `GET` | `/auth/session` | — | [`auth_session`](../../app/main.py#L245) |
| `POST` | `/auth/logout` | — | [`auth_logout`](../../app/main.py#L251) |
| `POST` | `/auth/email/start` | `EmailSigninStart` | [`email_signin_start`](../../app/main.py#L785) |
| `POST` | `/auth/email/verify` | `EmailSigninVerify` | [`email_signin_verify`](../../app/main.py#L797) |
| `POST` | `/auth/signout` | — | [`signout`](../../app/main.py#L815) |

### Execution and RPC

| Method | Path | Body | Handler |
|---|---|---|---|
| `POST` | `/execution/lifi/quote` | `LifiQuoteRequest` | [`lifi_quote`](../../app/main.py#L258) |
| `GET` | `/execution/lifi/status/{tx_hash}` | — | [`lifi_status`](../../app/main.py#L276) |
| `POST` | `/rpc/solana` | — | [`solana_browser_rpc`](../../app/main.py#L314) |
| `GET` | `/trade-plans/{plan_id}` | — | [`trade_plan`](../../app/main.py#L1459) |
| `POST` | `/trade-plans/{plan_id}/confirm` | `ConfirmRequest` | [`confirm`](../../app/main.py#L1467) |
| `POST` | `/trade-plans/{plan_id}/wallet-transaction` | `ConfirmRequest` | [`wallet_transaction`](../../app/main.py#L1475) |
| `POST` | `/trade-plans/{plan_id}/submit-wallet-transaction` | `SignedTransactionRequest` | [`submit_signed_wallet_transaction`](../../app/main.py#L1483) |
| `GET` | `/executions/solana/{signature}` | — | [`solana_execution_status`](../../app/main.py#L1493) |
| `POST` | `/executions/relay/{request_id}` | — | [`track_relay_execution`](../../app/main.py#L1498) |
| `GET` | `/executions/relay/session/{session_id}/{revision}` | — | [`relay_execution_for_turn`](../../app/main.py#L1526) |
| `GET` | `/executions/relay/{request_id}` | — | [`relay_execution_status`](../../app/main.py#L1532) |

### Administration

| Method | Path | Body | Handler |
|---|---|---|---|
| `GET` | `/admin/providers` | — | [`admin_providers`](../../app/main.py#L484) |
| `GET` | `/admin/tools/outcomes` | — | [`admin_tool_outcomes`](../../app/main.py#L490) |
| `PUT` | `/admin/providers/{tool_name}` | `ProviderPolicyUpdate` | [`update_admin_provider`](../../app/main.py#L503) |
| `DELETE` | `/admin/providers/{tool_name}` | — | [`reset_admin_provider`](../../app/main.py#L514) |
| `PUT` | `/admin/provider-groups/{provider}` | `ProviderPolicyUpdate` | [`update_admin_provider_group`](../../app/main.py#L522) |
| `POST` | `/admin/routes/preview` | `RoutePreviewRequest` | [`preview_admin_route`](../../app/main.py#L535) |
| `POST` | `/admin/intents/preview` | `IntentPreviewRequest` | [`preview_admin_intent`](../../app/main.py#L545) |
| `POST` | `/admin/providers/test` | `ProviderTestRequest` | [`test_admin_provider`](../../app/main.py#L578) |
| `POST` | `/admin/wash-trading/runs` | `WashTradingDetectionRequest` | [`create_wash_trading_run`](../../app/main.py#L607) |
| `GET` | `/admin/wash-trading/runs/{run_id}` | — | [`get_wash_trading_run`](../../app/main.py#L637) |
| `GET` | `/admin/wash-trading/runs/{run_id}/wallets/{wallet}` | — | [`get_wash_trading_wallet`](../../app/main.py#L666) |
| `PUT` | `/admin/users/{email}/plan` | `AdminPlanUpdate` | [`admin_set_plan`](../../app/main.py#L883) |
| `POST` | `/admin/users/{email}/credits` | `AdminCreditGrant` | [`admin_grant_credits`](../../app/main.py#L898) |
| `GET` | `/admin/users` | — | [`admin_list_users`](../../app/main.py#L1066) |
| `GET` | `/admin/users/{email}` | — | [`admin_user_detail`](../../app/main.py#L1080) |
| `POST` | `/admin/users/{email}/api-keys/{key_id}/revoke` | — | [`admin_revoke_key`](../../app/main.py#L1101) |
| `POST` | `/admin/users/{email}/sessions/revoke` | — | [`admin_revoke_sessions`](../../app/main.py#L1110) |
| `GET` | `/admin/billing/events` | — | [`admin_billing_events`](../../app/main.py#L1118) |
| `POST` | `/admin/billing/events/{event_id}/replay` | — | [`admin_replay_event`](../../app/main.py#L1124) |
| `GET` | `/admin/metrics/business` | — | [`admin_business_metrics`](../../app/main.py#L1137) |
| `GET` | `/admin/research/gaps` | — | [`admin_research_gaps`](../../app/main.py#L1239) |

### Conversation

| Method | Path | Body | Handler |
|---|---|---|---|
| `POST` | `/chat` | `ChatRequest` | [`chat`](../../app/main.py#L721) |
| `POST` | `/chat/feedback` | `FeedbackRequest` | [`chat_feedback`](../../app/main.py#L1389) |
| `GET` | `/chat/risk-charter/limits` | — | [`risk_charter_limits`](../../app/main.py#L1406) |
| `GET` | `/chat/history/{session_id}` | — | [`chat_history`](../../app/main.py#L1417) |
| `DELETE` | `/chat/wallet/{session_id}` | — | [`forget_chat_wallet`](../../app/main.py#L1429) |
| `DELETE` | `/chat/history/{session_id}` | — | [`clear_chat_history`](../../app/main.py#L1443) |

### Account, team and tasks

| Method | Path | Body | Handler |
|---|---|---|---|
| `GET` | `/me` | — | [`me`](../../app/main.py#L824) |
| `GET` | `/me/credits` | — | [`my_credits`](../../app/main.py#L829) |
| `PUT` | `/me/preferences` | `PreferencesUpdate` | [`update_preferences`](../../app/main.py#L843) |
| `GET` | `/me/api-keys` | — | [`list_api_keys`](../../app/main.py#L856) |
| `POST` | `/me/api-keys` | `ApiKeyCreate` | [`create_api_key`](../../app/main.py#L861) |
| `DELETE` | `/me/api-keys/{key_id}` | — | [`revoke_api_key`](../../app/main.py#L874) |
| `GET` | `/me/usage` | — | [`my_usage`](../../app/main.py#L912) |
| `GET` | `/me/invoices` | — | [`my_invoices`](../../app/main.py#L922) |
| `GET` | `/me/sessions` | — | [`my_sessions`](../../app/main.py#L930) |
| `POST` | `/me/sessions/revoke-all` | — | [`revoke_my_sessions`](../../app/main.py#L935) |
| `GET` | `/me/export` | — | [`export_my_data`](../../app/main.py#L942) |
| `DELETE` | `/me/conversations` | — | [`delete_my_conversations`](../../app/main.py#L963) |
| `DELETE` | `/me` | `DeleteAccountRequest` | [`delete_my_account`](../../app/main.py#L972) |
| `GET` | `/me/team` | — | [`my_team`](../../app/main.py#L1002) |
| `POST` | `/me/team/invites` | `TeamInvite` | [`invite_team_member`](../../app/main.py#L1007) |
| `DELETE` | `/me/team/members/{email}` | — | [`remove_team_member`](../../app/main.py#L1031) |
| `POST` | `/me/team/accept` | `TeamAccept` | [`accept_team_invite`](../../app/main.py#L1039) |
| `POST` | `/me/team/leave` | — | [`leave_team`](../../app/main.py#L1049) |
| `GET` | `/me/tasks` | — | [`my_tasks`](../../app/main.py#L1165) |
| `POST` | `/me/tasks` | `TaskCreate` | [`create_my_task`](../../app/main.py#L1170) |
| `PATCH` | `/me/tasks/{task_id}` | `TaskUpdate` | [`update_my_task`](../../app/main.py#L1176) |
| `DELETE` | `/me/tasks/{task_id}` | — | [`delete_my_task`](../../app/main.py#L1182) |
| `POST` | `/me/tasks/{task_id}/run` | — | [`run_my_task_now`](../../app/main.py#L1188) |
| `GET` | `/me/inbox` | — | [`my_inbox`](../../app/main.py#L1193) |
| `POST` | `/me/inbox/read` | `InboxRead` | [`read_my_inbox`](../../app/main.py#L1198) |

### Knowledge Base

| Method | Path | Body | Handler |
|---|---|---|---|
| `GET` | `/knowledge/search` | — | [`knowledge_search`](../../app/main.py#L1205) |
| `GET` | `/knowledge/status` | — | [`knowledge_status`](../../app/main.py#L1221) |
| `GET` | `/admin/knowledge/protocols` | — | [`admin_knowledge_protocols`](../../app/main.py#L1227) |
| `GET` | `/admin/knowledge/overview` | — | [`admin_knowledge_overview`](../../app/main.py#L1248) |
| `POST` | `/admin/knowledge/derive` | — | [`admin_knowledge_derive`](../../app/main.py#L1267) |
| `GET` | `/knowledge/graph/{entity_id:path}` | — | [`knowledge_graph`](../../app/main.py#L1279) |
| `POST` | `/admin/knowledge/bootstrap` | — | [`admin_knowledge_bootstrap`](../../app/main.py#L1304) |
| `POST` | `/admin/knowledge/ingest/{protocol_slug}` | — | [`admin_knowledge_ingest`](../../app/main.py#L1313) |
| `POST` | `/admin/knowledge/tick` | — | [`admin_knowledge_tick`](../../app/main.py#L1325) |

### Billing

| Method | Path | Body | Handler |
|---|---|---|---|
| `GET` | `/billing/plans` | — | [`billing_catalog`](../../app/main.py#L1334) |
| `POST` | `/billing/checkout` | `CheckoutRequest` | [`billing_checkout`](../../app/main.py#L1343) |
| `POST` | `/billing/portal` | — | [`billing_portal`](../../app/main.py#L1356) |
| `POST` | `/billing/webhook` | — | [`billing_webhook`](../../app/main.py#L1368) |

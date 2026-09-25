# Anvaya stabilization backlog

Prepared 16 September 2026 against HEAD `0c07de7d` and the visible staged changes. This is an action plan, not a claim that its checks have passed. Changes are being made concurrently; record a fixed commit before running release acceptance.

## Update at documentation commit

HEAD advanced to `8297bd0c` after this backlog was written. STAB-01's discovery fix is implemented: pytest is restricted to `tests/` and the live script was renamed. STAB-05's account-listing and retention changes are committed in `29ef9563`. STAB-11 now has a `/readyz` dependency/worker probe. These items still require their applicable release acceptance evidence; the original entries below record the planning baseline, not a claim that these implementation changes remain absent.

## Release target

The first target is a controlled, invite-only research beta: account sign-in, reliable research, retained conversations, Knowledge Base retrieval, and tasks. Public live trading has additional gates below. Keep product expansion and new framework adoption outside this stabilization cycle unless they resolve a demonstrated blocker.

Previously completed work stays completed: shared session access, execution policy, and task-scheduling services are extracted. Execution claims, reconciliation, and occurrence-keyed brief charges exist. The work now is to verify boundaries, close gaps, and make operation predictable.

Status: **Open** = work needed; **Partial** = implementation exists but acceptance is incomplete; **Verification needed** = newly staged behavior must be tested before closure.

## A. Establish a reliable release baseline

### STAB-01 — Fix test discovery and CI · Open

- Put the live selector smoke script behind a `main()` guard and/or explicitly restrict pytest discovery to `tests/`.
- Keep live model/provider diagnostics separate from ordinary regression tests.
- Verify the actual CI command in a clean environment with no private `.env`, including the opt-in PostgreSQL test.
- **Done when:** bare `pytest -q` collects safely without live model calls; CI passes Python, real PostgreSQL integration, TypeScript, browser executor checks, and image smoke checks.
- Sources: `scripts/tool_selector_smoke_test.py`, `pyproject.toml`, `.github/workflows/tests.yml`.

### STAB-02 — Define safe deployment modes · Partial

- Define research-beta versus execution-enabled settings and document which routes are available in each.
- Validate production configuration at startup: required shared storage, account delivery/auth setup, intended admin/MCP authentication, and prohibited development conveniences.
- Ensure the research-beta mode actually blocks money-moving entry points across providers; `LIVE_TRADING` behavior is not automatically uniform across Jupiter and Relay.
- Record a fixed candidate commit, configuration profile, schema assumptions, and image tag.
- **Done when:** unsafe combinations fail visibly and a deployment-mode test matrix proves the intended endpoint behavior.

## B. Identity, access, and retained state

### STAB-03 — Audit authorization across transports · Partial

- Make a matrix for anonymous users, email users, wallet-only users, team owners/members, scoped API keys, MCP service callers, and administrators.
- Map each identity against chat, history, wallet linking, task operations, exports, plan reads, confirmation, submission, and execution tracking.
- Confirm the intended custody model. Disable/remove the server-signing route for a wallet-only deployment, or separately restrict and test it if intentionally retained.
- Check resource ownership even when an identifier or confirmation material is known. Do not assume UI visibility is authorization.
- **Done when:** prohibited combinations fail in HTTP and MCP tests, allowed combinations work, and the remaining service-account exceptions are deliberate and documented.
- Sources: `identity.py`, `session_access.py`, `main.py`, `mcp_server.py`, `execution.py`.

### STAB-04 — Verify wallet-only account lifecycle · Verification needed

- Test fresh wallet sign-in, existing-wallet sign-in, linking from an email account, account switching, disconnect versus sign-out, revoked sessions, and account deletion.
- Cover EVM EOAs, supported smart-wallet verification, and Solana separately; test expired/reused challenges, invalid signatures, and chain identity handling.
- Verify email-less accounts in billing, notifications, team invitations, exports, and deletion screens.
- **Done when:** supported real wallet/browser combinations complete the lifecycle; replay, wrong-wallet, and account-collision cases cannot transfer another account's access.
- Dependency: STAB-03 authorization matrix.

### STAB-05 — Complete conversation retention and account isolation · Verification needed

- Staged changes introduce 30-day signed-in retention, two-hour anonymous retention, and server-backed account conversation listing. Review and test these changes rather than implementing them again.
- Define whether retention is measured from creation, last read, or last write, and explain the 200-message display limit.
- Verify retention after API restart, across workers, after anonymous-to-signed-in transition, and when Redis keys expire independently.
- Test two accounts in one browser, two browsers for one account, stale session IDs, deletion, and export.
- Decide whether pins, rename, archive, and projects remain local or become account-synchronized. State the shipped behavior explicitly.
- **Done when:** the documented retention promise survives the supported restart/deployment conditions, no account sees another account's chats, and expiry/deletion have clear UI states.

### STAB-06 — Separate durable state from disposable cache · Open

- Review Redis `allkeys-lru` against its use for histories, sessions, prepared transactions, and turn locks.
- Choose explicit persistence and eviction boundaries. Prefer durable transcript storage in PostgreSQL if retained history is a product promise; otherwise substantiate the Redis retention/recovery design.
- Test database/cache outages and recovery with memory fallback disabled.
- **Done when:** cache pressure cannot silently invalidate the promised data retention or allow concurrent work after losing a lock; dependency failures are controlled and observable.
- Dependency: retention decision in STAB-05.

## C. Money, scheduled work, and external services

### STAB-07 — Validate billing and credit accounting · Partial

- Run Stripe test-mode subscription, renewal, pack purchase, cancellation, failed payment, refund, and webhook replay flows.
- Include duplicate/out-of-order events, concurrent spending, team credit pools, low balances, and interruption between reservation and settlement.
- Reconcile displayed prices/entitlements with the actual Stripe catalogue. Resolve the Free/In-depth entitlement discrepancy.
- **Done when:** ledger balances reconcile to expected fixtures, retries do not duplicate credit effects, interrupted holds have a recovery path, and all account types have usable billing states.

### STAB-08 — Make task delivery recoverable · Partial

- Preserve existing atomic occurrence claims and idempotent charges; add notification delivery identifiers/outbox semantics where needed.
- Kill a worker before charge, after charge, before send, and after send. Verify lease recovery and rescheduling.
- Define timezone/DST behavior and test pause/resume, run-now concurrency, and depleted team balances.
- **Done when:** paid occurrences are not lost or charged twice; in-app duplicates are prevented; external-email delivery guarantees and provider limitations are explicit.

### STAB-09 — Certify execution separately · Partial; live-trading release gate

- Test Jupiter and Relay with actual supported wallets in an isolated, appropriate test environment; use deliberately bounded transaction values when a real-network test is necessary.
- Cover rejection, expiry, changed context, duplicate clicks, two tabs, failed simulation, timeout after broadcast, restart before finalization, refund, and unknown status.
- Verify authority and policy checks at the layer that executes, not only on the card.
- Reconcile every test transaction to its chain/provider outcome; confirm uncertain outcomes do not automatically rebroadcast.
- **Done when:** the reviewed transaction is the executed transaction, recovery is demonstrated, and a focused security review has no unresolved release blockers. Do not open public trading based only on unit tests.
- Dependencies: STAB-02, STAB-03, STAB-04, STAB-06, STAB-11.

## D. Answer quality, interface, and operation

### STAB-10 — Establish a representative research acceptance set · Partial

- Extend sweep prompts with real user phrasing, ambiguous symbols, follow-ups, missing data, conflicting sources, and provider failures.
- Score intent, correct tool/source, factual support, citation correctness, freshness, and sensible abstention separately.
- Compare deterministic selection with optional model arbitration on the same cases before enabling the latter for users.
- Use fixed Knowledge Base fixtures, including incidents/funding/governance, and distinguish cached-answer assertions from fresh-tool assertions.
- **Done when:** the team defines explicit quality/latency/cost thresholds and the release candidate meets them without concealing failed cases. Test count alone is not the threshold.

### STAB-11 — Add readiness, monitoring, and restore drills · Open

- Separate process liveness from dependency readiness; add signals for database/Redis availability, worker heartbeat, stuck executions, task backlog, provider failures, and credit anomalies.
- Measure request success rate, latency percentiles, provider fallback frequency, and actual versus estimated costs without logging secrets.
- Back up and restore PostgreSQL, relevant Redis state, and configured local files. Test restart and application rollback with current schema/data.
- Package/supervise the standalone ingester; the API image currently omits `scripts/` and Compose does not supply an ingestion worker.
- **Done when:** a failed dependency or stopped worker produces an actionable signal, and a documented restore/rollback drill meets agreed recovery objectives.

### STAB-12 — Finish the browser acceptance pass · Partial

- Reproduce and resolve the previously observed clipped token picker and ineffective home Swap assets shortcut; do not assume they persist or are fixed without a fresh run.
- Cover account/wallet transitions, history, all settings, tasks, billing unavailable/error states, administration, and populated Knowledge Base results.
- Include keyboard operation, focus restoration, mobile overflow, slow networks, stale responses, and supported browser/wallet combinations.
- **Done when:** the fixed release candidate passes the agreed critical journeys, with no silent failures or stale action cards and evidence for each supported environment.

## Recommended execution order

1. **First batch:** STAB-01, STAB-02, STAB-03; review the staged work through STAB-04 and STAB-05.
2. **Second batch:** STAB-06, STAB-07, STAB-08, STAB-11. These establish reliable storage, money, background work, and recovery.
3. **Research-beta acceptance:** STAB-10 and STAB-12 on a fixed build, with all applicable earlier gates met.
4. **Separate execution release:** STAB-09 plus its dependencies. Research-beta approval is not trading approval.

Do not assign calendar estimates until the access/retention decisions and the live integration environment are settled. Close each item with a commit, configuration, test evidence, and any accepted limitation. Keep historical QA results distinct from the current release evidence.

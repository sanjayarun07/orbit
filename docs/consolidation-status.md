# Consolidation status — 2026-09-10

## Implemented and checked locally

- One resolver in `app/routing/resolver.py`; removed legacy IntentResolution,
  `_resolve_intent_candidate`, and Relay raw-history execution recovery.
- Typed workflow events at the session persistence boundary; pending Jupiter
  plans are superseded before inference on a replacement/non-confirmation turn.
  Atomic submission claims reject superseded and expired plans.
- Relay chat quotes are bound to conversation, revision and UI turn epoch.
  Late quote responses cannot reactivate an earlier conversation's approval.
- Data-backed equity alias seed replaces the inline ticker list. This is not a
  complete equity instrument database.
- Calibration harness and saved results in `embedding-calibration.jsonl`.
  The 12-case set is a smoke set, not sufficient calibration for production.
  At similarity 0.65 one false quote route occurred; defaults were not lowered.
- Raw tool payloads disabled by default; allowlisted tool names and completion
  status remain visible. Evidence summaries use internal results, not redacted
  UI payloads. Raw portfolio tool cards are hidden under this setting.
- Auth endpoint rate limiting, uncertain broadcast status (non-retryable), and
  restartable best-effort ClickHouse worker. Example credential removed.
- Regression tests for wallet leverage routing, explicit token context, unrelated
  DEX pair filtering, HYPE discovery routing, pool URLs, equity suggestions,
  stale trade history, workflow invalidation and public activity redaction.
- Local verification: 196 Python tests passed, TypeScript check passed, inline
  UI JavaScript parsed. No wallet transaction was signed or broadcast.

## Follow-up implementation

- Extracted graph handlers into app/nodes/{routing,general,research,portfolio,trading}.
  Shared model runtime and typed state are separate; graph.py is composition and
  the public runner. Removed the former inline handler implementations.
- Added the browser Executor interface with Jupiter and Relay adapters and
  prepare/execute/status operations. No automatic signing-provider fallback.
  Jupiter uses sign-only wallet approval and server-validated submission; Relay
  retains SDK signing under the same browser execution boundary. LI.FI remains
  a read-only backup quote service, not an enabled signing adapter.
- Persisted the signed Jupiter transaction's deterministic signature in its
  atomic submission claim before broadcast. RPC acceptance is submitted, not
  executed. Read-only reconciliation requires finalized chain evidence.
- Persisted Relay request IDs and unique chat-turn claims before wallet approval.
  Reloaded chats recover tracking rather than automatically quote another swap.
  Relay provider status is checked by a separate restartable backend worker.
- Uncertain outcomes stay non-retryable. Removed automatic Relay refresh/retry
  after execution errors and removed unconditional backend terminal-status writers.
- Added real PostgreSQL isolated-schema coverage: concurrent claims, connection
  pool restart recovery, terminal-state CAS, supersession and Relay turn uniqueness.
- Added mocked HTTP chat-to-wallet-submit-to-finalization coverage and browser
  adapter tests. No live transaction was signed or broadcast.
- Added staged credential checking, CI PostgreSQL/browser checks, PR checklist,
  release/rotation/protection runbook and a local consolidation branch.
- Final verification: 209 Python tests passed; the opt-in PostgreSQL test passed
  separately against localhost in an isolated temporary schema; six browser
  executor tests and TypeScript validation passed. No live trade was submitted.

## External release gates and known limits

- Complete the event-driven lifecycle: workflow reconstruction still exists in
  session metadata. Execution settlement now has durable reconciliation, but
  legacy records without signatures require operator investigation.
- Broaden instrument and labeled dev coverage. Run actual wallet-extension and
  live provider integration checks in the deployment environment before release.
- Disable memory fallback in production; it is still enabled for this development
  checkout. Durable restart recovery depends on PostgreSQL being available.
- Rotate exposed credentials at each provider; removing an example value cannot
  revoke it. Existing local .env credentials remain operational and untracked.
- Baseline is staged on codex/production-consolidation. A commit requires the
  user's author name/email; remote protections require a repository URL and
  administrative access. Neither was configured during this pass.

This is a safety-focused consolidation pass, not a production certification or
completion of the entire multi-week roadmap.

# Durable jobs: work that keeps going

Spec, 2026-09-22. Scope: a job engine for work that outlives one HTTP
request, resumes after a restart or a lost worker, never repeats a paid
call, can pause for the user, and settles with a receipt. The shape is
taken from OpenMuse's task worker (CopilotKit, MIT); the code is ours, in
Python, on the Postgres and Redis the stack already has.

## Why

Orbit already runs work that is long and costs money per step: a deep
dive at 60 to 70 seconds and a dozen provider calls, the desk, the tape.
The roadmap adds more of it: the equities paper desk, periodic live
re-runs, signal and alert cycles. Today each of these is one request. A
restart, a lost worker or a closed tab loses the run and re-spends every
call. The existing task engine (`app/tasks.py`: reminders, price alerts,
the brief) has claim leases and stale-claim recovery but no checkpoints,
no resumption, no pause, no per-run log: each occurrence is a single shot.

## Non-goals

Not Temporal, not a queue, no new infrastructure. No browser or terminal
worker. The job engine never signs a transaction: an approval is a trade
plan the user confirms in their wallet, as now.

## Model

One table, `jobs`, one owner per row (cascades with the user), and one
append-only `job_events` table.

| Column | Meaning |
|---|---|
| `id`, `user_id`, `account_id`, `session_id` | ownership; `session_id` is the conversation the result lands in |
| `kind` | `deep_dive`, `desk`, `tape`, later `paper_cycle`, `rerun`, `scheduled` |
| `status` | see states |
| `spec` | the request as given: subject, chain, question, options |
| `plan` | `[{id, title, status, started_at, finished_at, note}]`, at most 12 steps |
| `state` | free-form working state the handler keeps between steps |
| `evidence` | `[{id, kind, title, source, excerpt, at}]`, what was seen |
| `operations` | `{sha256(tool, args): {result, at, cost_usd, volatile}}`, the idempotent cache |
| `signals` | typed `Signal` dumps (app/signals.py) as they are produced |
| `attempts`, `lease_id`, `lease_until` | the lease; `lease_until` is extended by a heartbeat |
| `next_run_at` | for `scheduled` jobs |
| `question`, `action_id` | why the job is paused, and on what |
| `result`, `receipt_id`, `error` | the outcome; `receipt_id` links the decision receipt |
| `created_at`, `updated_at`, `settled_at` | timing |

`job_events`: `(id, job_id, at, kind, title, detail)` with kind in `step`,
`checkpoint`, `paused`, `resumed`, `error`, `settled`. This is the run log
the UI streams and the turn log links to.

Every write to a running job is a compare-and-swap on
`(id, lease_id, status = 'running')`. A write that finds another lease
raises `LostLease` and the handler stops where it is. This is what makes
two replicas safe without any Redis coordination: the row is the lock.

## States

```
queued ──claim──► running ──settle──► succeeded | failed | cancelled
                    │  ▲
   ask the user     ▼  │ answer / approval / expiry
        waiting_input, waiting_approval
scheduled ──next_run_at ≤ now──► running (then back to scheduled)
running with lease_until < now ──► claimable again (attempts + 1)
```

- `queued`: created, nothing done. Claimed by any worker on any replica.
- `running`: one lease holds it. Heartbeat every `lease/3` extends the
  lease; a heartbeat that fails aborts the handler.
- `waiting_input`: the handler asked a question (the existing rule:
  ambiguous means ask, never guess). The question is posted to the
  conversation; the user's next message in that conversation answers it
  and the job is re-queued with the answer in `state`.
- `waiting_approval`: the handler produced a trade plan. `action_id` is
  the plan id; the plan's own TTL (120 s) is the approval expiry. Confirm
  in the wallet resumes the job; expiry fails the step, not the job.
- `succeeded`: `result` and `receipt_id` set, the answer appended to the
  conversation, credits charged once.
- `failed`: after `attempts` reaches the kind's limit (3), or on a
  non-retryable error. `error` says why; the partial `result` stays.
- `cancelled`: by the user; the lease is abandoned at the next guard.

A lost lease is not a failure: the job goes back to `queued` with its
plan, state, evidence and operations intact, and the next claim resumes
from the last checkpoint.

## The handler contract

A handler is `async def run(job, ctx) -> Outcome` where `ctx` offers:

- `await ctx.guard()`: raises `LostLease` if the lease is gone. Called
  before every provider call and every checkpoint.
- `await ctx.checkpoint(**patch)`: CAS-writes plan, state, evidence,
  signals. Returns the fresh row.
- `await ctx.step(title)`: marks the plan step started, writes an event.
- `await ctx.call(tool, **args)`: the operation cache. A cached result is
  returned without a call, costs nothing and writes no event; a fresh
  call is made through the provider router, its result and cost are
  cached at once, and the paid-data cap counts it. Tools tagged volatile
  (price, funding, order book) are cached with a max age of 60 s.
- `await ctx.ask(question)`: pauses with `waiting_input`.
- `await ctx.approve(plan_id)`: pauses with `waiting_approval`.
- `ctx.signal`: an `asyncio.Event` set when the lease is lost or the job
  cancelled, for handlers that await long things.

Handlers must be restartable from any checkpoint: they read the plan and
skip finished steps. That is the one discipline this asks of them.

## The worker

One asyncio task per replica, started in the lifespan next to the task
worker, ticking every second: claim up to three eligible jobs by CAS,
run each with its heartbeat, requeue on `LostLease`, record the error and
count the attempt otherwise. Shutdown drains through
`execution_policy.drain_background`; whatever does not finish in time is
recovered by lease expiry on another replica. A `maintenance` pass every
minute re-appends outcomes that settled without their conversation
message (a crash between the two writes) and expires stale approvals.

## Attaching a request to a job

A chat request that starts a job stays attached for up to
`JOB_ATTACH_SECONDS` (default 90): events stream into the turn as status
lines and the answer arrives in the same turn when the job settles in
time. Past that, the turn ends with "still working; the answer will
appear here", and the job's settle appends the answer to the conversation
(the path the brief uses for the inbox, extended to append an assistant
message to a session). `GET /jobs`, `GET /jobs/{id}`,
`GET /jobs/{id}/events` (SSE) and `POST /jobs/{id}/cancel` are the API;
the sidebar shows a running job on its conversation.

## Money and records

- Credits: `charge_once` at settle with the job id as the reference, so a
  resumed job is charged once. The per-turn paid-data cap becomes a
  per-job cap counted from the operation cache, so a replay costs zero.
- The decision receipt (`decision_records`) is written at settle from the
  job's signals, evidence and result: one receipt per job, linked both ways.
- The turn log records the attaching turn and links the job id; the
  admin Turns panel shows the job's events under it.
- Deletion: jobs are scrubbed like the turn log (per-user advisory lock,
  deletion marker consulted by the worker before any checkpoint), so a
  job running during a deletion stops and keeps no words. Export includes
  the user's jobs and events.

## What moves, in order

1. **Deep dive.** One handler: resolve, plan from the ten dimensions, one
   step per evidence source through `ctx.call`, judge, receipt. The
   request stays attached for 90 s, which covers today's run; the win is
   resumption and no re-spend on a restart. Removes the special-casing in
   the research node.
2. **Desk and tape.** The desk's fan-out (market research, execution,
   risk) as three plan steps; the tape's clauses as parallel calls under
   one step.
3. **Reminders, price alerts, the brief** become `scheduled` jobs and
   `app/tasks.py` is retired after a migration of `user_tasks`. Same
   inbox, same email, same plan limits.
4. **Paper-desk cycles and periodic re-runs** (the hedge-fund roadmap) are
   `scheduled` jobs from day one; the holder ledger stays a system worker
   because it belongs to no user.

## Acceptance

- Kill the worker mid-plan; restart; the job resumes at the next step and
  the operation cache shows zero repeated provider calls.
- Two replicas, one job: exactly one runs it; the other's claim fails.
- Lose the lease during a call: the handler stops at its next guard and
  the job is requeued with state intact.
- An approval expires: the step fails, the job asks again, the user's
  wallet was never asked twice.
- Delete the account while a job runs: it stops, is scrubbed, and the
  receipt is not written.
- Export lists the job and its events.

## Size

The engine (table, worker, context, API) is on the order of the OpenMuse
worker plus the API, about 600 lines with tests. Wave 1 is the engine
plus the deep-dive handler, about two days. Wave 2 a day. Wave 3 a day
plus the migration. Wave 4 lands with the paper desk.

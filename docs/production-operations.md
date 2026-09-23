# Production release gates

## Deployment modes

Two independent switches, because they answer different questions.

`DEPLOYMENT_MODE` decides whether this deployment may move money **at all**.
`research` (the default) refuses every money-moving entry point across every
provider; `execution` allows them, still subject to the per-trade policy below.
Leaving it unset infers the mode from the older `LIVE_TRADING` flag, so an
existing deployment keeps behaving as it did.

`DEPLOYMENT_MODE` is the *only* execution policy. `LIVE_TRADING` feeds into it
when the mode is unset and is otherwise checked for agreement at startup: both
`research` with live trading on and `execution` with live trading off are fatal
configuration errors. The second was the dangerous one -- it passed the audit
and reported execution enabled in `GET /config/public` while the Jupiter routes
still refused every call, so the deployment advertised what it would not do. The
trade routes no longer consult the legacy flag at all.

`ALLOW_CUSTODIAL_SIGNING` decides whether the **server** may hold a key and sign
on a user's behalf. Off by default, so the server-signing route refuses even on
an execution deployment. A wallet-only product should leave it off and should
not configure `SOLANA_PRIVATE_KEY` at all; doing both with live trading on is a
startup failure until the operator states the intent explicitly.

| Entry point | research | execution | execution + custodial |
|---|---|---|---|
| `POST /trade-plans/{id}/confirm` (server signs) | 403 | 403 | allowed |
| `POST /trade-plans/{id}/wallet-transaction` | 403 | allowed | allowed |
| `POST /trade-plans/{id}/submit-wallet-transaction` | 403 | allowed | allowed |
| `POST /execution/lifi/quote` | 403 | allowed | allowed |
| Relay swap (browser SDK) | hidden in the UI | offered | offered |

The refusal is a FastAPI dependency, so it runs before the ownership lookup: a
research deployment never reads the plan store to say no, and a route added
later is covered by declaring `Depends(_require_execution_mode)`.

Relay's execution *claim* (`POST /executions/relay/{request_id}`) carries the
same gate. It is the step immediately before wallet approval, so it is a
money-moving entry point. The `GET` status routes are deliberately not gated: an
operator still has to reconcile swaps that happened before a deployment was
switched to research mode.

The browser fails closed. `executionEnabled` starts disabled and is turned on
only by a configuration load that both succeeded and said execution is enabled;
a failed fetch leaves swapping off and says so distinctly ("could not confirm
this deployment's execution settings") rather than claiming research mode, with
a retry, so a transient blip costs one retry rather than the feature.

**Accepted limitation, stated plainly.** Relay quotes and signs in the browser
against Relay's own API, so the server is not consulted for the quote or the
signature. Research mode refuses the claim endpoint, reports every provider
unavailable in `GET /config/public`, and the shipped UI hides the swap dialog
and both swap cards. Someone driving the Relay SDK directly, outside this UI,
is still not stopped from signing a swap with their own wallet. The Jupiter and
LI.FI routes are genuinely server-enforced.

## Startup configuration audit

`ENVIRONMENT=production` turns development conveniences into refusals to start.
Every problem is collected and reported at once so the whole list is fixed in
one deploy. Fatal in production:

| Setting | Why it is fatal |
|---|---|
| `DEV_EXPOSE_MAGIC_LINKS=true` | The sign-in link is returned in the HTTP response whenever email delivery fails, so anyone who knows an address could sign in as them. |
| `ALLOW_MEMORY_FALLBACK=true` | A missing datastore falls back to per-process memory, so retained history, trade plans and turn locks stop being shared between workers instead of failing visibly. |
| `DATABASE_URL` unset | No durable store for accounts, plans or history. |
| `REDIS_URL` unset | No shared store for sessions, turn locks or retention. |
| `MCP_API_KEY` unset | `/mcp` accepts unauthenticated tool calls. |
| `PUBLIC_BASE_URL` not a public HTTPS origin | Sign-in and MCP hand-off links are built from it. |
| `OPENAI_API_KEY` unset | Every chat turn fails. |

Warnings are logged and returned from `GET /readyz` as `config_warnings`, but do
not block startup: a missing `ADMIN_API_KEY` (admin routes fail closed with 503),
a missing `RESEND_API_KEY` (wallet sign-in still works), and x402 enabled against
a testnet. Anything fatal stops the process, so a running instance can never
report one.

## Configuration and durable state

Use PostgreSQL and Redis with `ALLOW_MEMORY_FALLBACK=false`. Memory mode is
development-only: restart recovery cannot be guaranteed without PostgreSQL.
Stay on `DEPLOYMENT_MODE=research` (with `LIVE_TRADING=false`, which must agree
with it) until wallet, RPC, persistence and provider integration tests have
passed in the target deployment. Keep `EXPOSE_TOOL_TRAJECTORY=false`.
Back up trade_plans and relay_executions; do not purge unsettled records.

Jupiter signatures are now stored atomically with the submission claim before
broadcast. RPC acceptance means submitted, not executed. The worker polls chain
history and requires finalized evidence for executed/failed. Missing signatures
remain submission_unknown; never automatically rebroadcast or create a retry.
Old submitting records without signatures require manual explorer/provider
investigation. They cannot be safely reconstructed by guessing.

Relay execution claims persist the provider request ID before wallet approval.
The SDK signs with the wallet; the backend independently checks Relay's status
API. Deposits and pending fills are not success; refunds are not successful swaps.
Provider status documentation: https://docs.relay.link/references/api/get-intents-status-v3
An abandoned wallet approval remains unknown; get a fresh reviewed quote only
after checking the old request and wallet activity.
Context replacement is checked before entering the wallet SDK, but Orbit cannot
revoke a transaction already approved/broadcast by an external wallet. Cancelling
a chat request is not an on-chain transaction cancellation.

Both reconcilers restart with the app, never sign and never rebroadcast. Monitor
the age/count of unresolved records, database availability and provider errors.
Multiple workers are safe for terminal updates, but duplicate read polling costs
more; assign reconciliation to one worker deployment at larger scale.

## Credential rotation (provider administrator required)

1. Inventory configured secret variable names without displaying their values.
2. Create replacement credentials in each provider dashboard, with minimum scope
   and production restrictions. Prioritize every key ever pasted, logged or committed.
3. Update the deployment secret manager and local .env; do not paste keys into chat.
4. Restart services and verify a low-cost read-only request per provider.
5. Revoke the old keys and verify they no longer authorize requests.
6. Record provider, key identifier, operator and rotation time—not the secret.

`scripts/check_secrets.py` checks the staged baseline and recognizes common key
formats. Locally it also checks for literal .env secret values. It cannot revoke
keys or prove that a key was not leaked elsewhere.

## Repository protections (repository administrator required)

After setting a remote and commit author, commit the audited baseline on the
codex/production-consolidation branch. Open a PR; never force-push a baseline over
an existing repository. Protect the release branch with:

- Pull requests and at least one independent approving review.
- Required `test` CI check, up-to-date branches, resolved review conversations.
- No force pushes or branch deletion; restrict bypass permissions.
- Secret scanning and push protection where supported by the hosting plan.

No remote or author was configured in this checkout when this pass began.
Local CI files do not activate host-level protections automatically.

## Liveness, readiness, and release evidence

`GET /health` is liveness only: the process is up and serving. It is cheap,
touches no dependency, and must not be used to decide whether to send
traffic.

`GET /readyz` is the readiness gate. It reports, with per-check latency and
a 3s timeout each (`readiness_check_timeout_seconds`):

| Check | Required when | Fails the probe |
|---|---|---|
| `postgres` | `DATABASE_URL` is set | yes |
| `redis` | `REDIS_URL` is set | yes |
| `model_credentials` | always | yes |
| `reconciliation`, `relay_reconciliation`, `tasks` | always | yes -- these must never die silently |
| `tool_outcomes`, `knowledge_ingest` | never | no, reported as `degraded` |

It returns **503** when any required check fails, so a load balancer or
deploy gate can act on it, and **200** with a non-empty `degraded` list
when only optional workers are down (the knowledge ingester is off by
default, which is a degraded state, not an outage). The response also
carries `version` (the running commit, or `ORBIT_RELEASE`) and
`live_trading`, so a deployed instance can be identified from its own
probe rather than from deployment notes.

A required worker that was never started reports `not started` and fails
the probe -- a process whose startup did not complete must not receive
traffic.

### Release candidate

CI runs a bare `pytest -q`, which `pyproject.toml`'s `testpaths` pins to
`tests/`; `scripts/` is excluded from collection and every script must be
import-safe (`tests/test_release_hygiene.py` fails the build otherwise --
an operational script that ran work or exited at import time used to break
collection). Verify a candidate with:

    pytest -q                      # the suite CI runs, nothing else
    curl -fsS $HOST/readyz | jq    # 503 = do not promote

## Authorization model

`tests/test_authorization_matrix.py` is the authority: it enumerates every
route, requires each to be declared in one class, and fails the build when
a route's enforcement does not match its declaration or when a new route is
added without a class. The classes are:

| Class | Means | Enforced by |
|---|---|---|
| `public` | no caller identity needed | nothing |
| `auth_entry` | unauthenticated by design -- these establish identity | signature/token inside the handler |
| `webhook` | authenticated by provider signature | Stripe signature check |
| `user` | a signed-in account | `Depends(require_user)` |
| `admin` | the admin API key | `Depends(_require_admin)` |
| `owner_scoped` | bound to the resource's own owner | `_require_session_access` / `_require_plan_access` |

**Who pays is not who owns.** `identity.account_id` is the *billing* account,
and a team member is billed to their owner's account, so every member of a team
shares one `account_id`. Using it as an ownership key made one teammate's quoted
trade viewable and executable by another. Ownership keys off
`identity.principal_id`, which is the individual user (or the anonymous device),
and billing keys stay where they are. Credits, plans and rate limits still use
`account_id`, which is correct for them.

**One authorization function, every transport.** The rule lives in
`app/plan_access.py` and both the HTTP routes and the MCP server call it. The
MCP tool previously returned whole plans -- `confirmation_text` included, which
is the only secret the execution endpoints check -- to any caller who knew the
id, so the HTTP gate could simply be walked around. `plan_access` reads the plan
through `plans.get_plan` as a module attribute on purpose: the gate and the
handler must read through the same function, or the gate can answer "no such
plan" while the handler goes on to find one.

Three earlier hardening changes, from building the matrix itself:

1. **Trade plans are bound to the account that requested them.**
   `confirmation_text` is `CONFIRM {plan_id}`, so plan id alone previously
   satisfied every check the execution endpoints made -- including
   `/confirm`, which signs server-side when `LIVE_TRADING` and a signer key
   are configured. Plans now carry `owner_account_id` (set per turn through
   a contextvar, the same pattern as the call budget) and all four
   trade-plan routes return 404 to anyone else. Plans quoted before this
   have no owner and stay reachable by id until they expire.
2. **Admin authorization runs before body validation.** All 27 admin
   routes called `_require_admin(request)` inside the handler, so an
   unauthenticated caller got a 422 schema error rather than 401. They now
   use `Depends(_require_admin)`.
3. **Relay tracking attached to a conversation passes the session gate.** A
   matching revision alone used to be enough.

Still open, and deliberately not claimed as done: the wallet sign-in paths
other than Phantom have not been exercised with real extensions, and this
matrix is a structural guarantee, not a penetration test.

## Conversation retention

**Retention is measured from the last write.** Every turn appended to a
conversation pushes its expiry out again; reading one does not. "Thirty days"
means thirty days after the last message, not after the conversation started.

| State | Kept for | Listed by account | Recoverable later |
|---|---|---|---|
| Signed out | 2 hours (`CHAT_HISTORY_TTL_SECONDS`) | no, never mapped to an account | no |
| Signed in | 30 days (`CHAT_HISTORY_SIGNED_IN_TTL_SECONDS`) | yes, `GET /me/conversations` | yes, from any browser |

A conversation started signed out is claimed by the first account to use it
while signed in, and is retained as that account's from then on. Ownership never
transfers after that.

**The 200-message limit is a display cap, not retention.** A conversation keeps
its most recent 200 messages; older ones are trimmed as new ones arrive. It is
the length of the transcript the UI can show and the model can be given, and it
is independent of how long the conversation lives.

### Expiry writes never shorten what is stored

This was a real defect. The retention a conversation had been granted lived only
in a per-process dictionary, so any process that had not itself granted it --
a restarted API, or simply a second worker -- wrote the two-hour scratch expiry
back over a signed-in account's thirty-day history on the next turn.

Every expiry write now goes through an extend-only pair, so retention is a
property of the stored data rather than of whichever process handled the turn.
Both halves of that pair are necessary: `GT` refuses to act on a key that has no
expiry, because Redis treats that as an infinite one, so `GT` alone would leave
a conversation's first message stored forever; `NX` sets that first expiry and
`GT` handles every refresh after it. The routing-context key is written by value
on each turn and so uses `KEEPTTL` before the same pair.

Verified live across a real API restart: a wallet-only account's conversation
held 30 days before the restart and still held 30 days after a further turn on
a fresh process.

### What is account-synchronized and what is not

Shipped behavior, stated explicitly because the two halves differ:

- **Synchronized:** the conversation list, its titles and their ordering. These
  come from the server and appear on any browser the account signs in from.
- **Per-browser only:** pins, renames, archive and the Recents/Pinned/Archived
  view. These live in that browser's local storage and do not follow the account
  to another device.

Entries are stamped with the account that owns them, so signing in as a second
account on a shared browser does not show the first account's conversations. To
be precise about what that is and is not: it is a view filter, so the stored
titles remain in that browser's local storage. Anything stronger requires moving
this state to the server, which is the decision to revisit if pins and renames
should follow the account.

## Wallet account identity

**One EVM address is one account on every EVM network -- if it is an EOA.** An
externally owned address is the same secp256k1 key everywhere, so identity is
resolved by `accounts.find_wallet_owner`, which matches EOA rows across every
non-Solana chain label. Solana is a different curve and a different address
space, so it is matched exactly.

**The network comes from the verified challenge, never from the request.**
Both `/auth/wallet/verify` and `/auth/coinbase/verify` derive the chain from the
challenge that was actually verified. Taking it from the request body let a
signature validated against Base be presented as Ethereum and resume an
Ethereum-linked account; a mismatch between the two is now rejected outright.
The Coinbase endpoint used to record every network as `evm`, which is the same
merge in a different shape, since Coinbase Smart Wallet is a contract wallet.

**A contract wallet is scoped to the network it was verified on.** A contract
address is derived from its deployer and nonce, not from a key, so the same
address on Base and Ethereum can be two different contracts with two different
controllers. Treating those as one identity meant authenticating against a
contract you control on one network could resume an account linked to somebody
else's contract on another. `wallet_auth.verify_challenge` now reports whether
an EOA recovery or the ERC-1271/6492 validator proved the signature, that type
is stored on the link, and EOA lookups exclude contract rows so the
cross-network match cannot be reached from the other side either. Rows linked
before this column existed default to `eoa`, which is how they were already
being treated; a pre-existing smart-wallet link keeps its old, broader scope.

**An API key cannot become a browser session.** Wallet sign-in is a browser
flow, and `/auth/wallet/verify` and `/auth/coinbase/verify` refuse any request
carrying an API key. Without that, a read-only key could link its holder's own
wallet to the key owner's account, receive a full session cookie, and mint an
unrestricted key -- and the wallet link would outlive the key it came from.

This was a real defect, not a hypothetical. Identity used to be the exact
`(chain, address)` pair, so the same wallet produced a different account
depending on how it arrived:

- MetaMask on Ethereum stored `ethereum`, the same wallet switched to Base
  stored `base`, and the Coinbase SDK entry point stores `evm`.
- Signing in from a second network with no existing session therefore created a
  brand-new empty account, silently splitting one person's conversations,
  credits and plan in two, with no error anywhere.

The stored chain label is now provenance only -- the network a wallet first
signed in from, which is what Settings displays. Linking an address that already
belongs to the same account under another EVM label adds no second row.

`tests/test_wallet_lifecycle.py` holds the rest of the lifecycle evidence:
challenge expiry under real elapsed time, nonce substitution, cross-family
replay (a Solana nonce offered to the EVM verifier), address binding, sign-out
revoking the token server-side rather than only clearing the cookie, deletion
releasing the wallet so it can start over, and every account surface answering
without a 500 for an account whose email is `null`.

## Billing lifecycle

**Deleting an account cancels its subscription first.** Deleting the user row
strips the Stripe customer and subscription ids while the subscription keeps
renewing, leaving a recurring charge nobody can map back to a person. If the
cancellation fails the deletion stops with HTTP 409 and the account is left
intact: that is recoverable, and deleting anyway is not.

**Stripe events apply only to the subscription the account is actually on.**
Events arrive late and out of order, so a customer who cancelled and
resubscribed has two subscription ids. One rule covers every subscription-state
handler: an event for the current subscription always applies, a different one
applies only when it is demonstrably newer (`created`, recorded as
`users.subscription_created`), and without timestamps to compare a different id
is treated as stale. Guarding only the deletion handler was not enough -- an
out-of-order `customer.subscription.updated` made the old subscription current
again, after which the old deletion matched and downgraded the account.

**Every subscription-state writer applies the ordering rule, and payment is
separate from entitlement.** The rule above first covered only the update and
delete handlers; `checkout.session.completed` and `invoice.paid` still wrote
unconditionally, and either could put an older subscription back as current.
Checkout now applies the same rule. The invoice handler separates two facts: the
payment happened, so its credits are always granted (keyed by event id, so a
replay cannot double-grant); which plan the account is *on* is a different
question that an invoice never answers, because invoices renew subscriptions
rather than establish which one is current. Entitlement is written only when
the invoice belongs to the subscription the account is already on.

**Entitlements come from the price being charged, not from checkout metadata.**
Metadata is written once at checkout and never updated, so after an upgrade it
still names the old plan -- a Max-priced subscription kept Pro entitlements
indefinitely. The subscription's price is read first and metadata is only a
fallback when no configured price matches.

## Plan limits

A limit on how many tasks *run* has to bind wherever a task becomes active, not
only where one is created. Creation was the only check, so pausing tasks and
resuming them walked straight past it. `tasks.assert_can_activate` now runs on
both paths and excludes the task being changed, so a task at the limit can
still be edited.

Counting and activating are two operations, so checking the count and then
activating still let two concurrent resumptions take the same last slot. Both
paths hold `tasks.account_task_gate`, a per-account mutex: a transaction-scoped
Postgres advisory lock where a pool exists, so it serialises across every
worker, and an asyncio lock keyed by `(loop, account)` otherwise.

The lock, the count and the write share **one connection and one transaction**.
The first version held a pool connection for the lock and then asked the pool
for a second connection to count and write. With as many concurrent activations
as the pool has connections, every one of them held a lock-connection and none
could obtain a work-connection: a deadlock that surfaced as request timeouts.
Every store call inside the locked window now takes the gate's connection
(`db=`) instead of going back to the pool, and the transaction-scoped lock is
released with the commit, so there is no separate unlock to miss.

**How this is tested, and the gap it exposed.** The ordinary suite cannot reach
this code: `tests/conftest.py` points the pool getter of the accounts, credits,
api_keys, billing and tasks modules at nothing, so every in-suite test of those
layers runs their in-memory branch. The advisory-lock branch, the one
production runs, had no coverage at all until `tests/test_postgres_task_gate.py`,
which is opt-in and needs an isolated database:

    TEST_DATABASE_URL=postgresql://localhost/orbit_test pytest tests/test_postgres_task_gate.py tests/test_postgres_execution.py

Those are the skips a bare run reports. A skip there means the Postgres path is
unverified in that run, and the skip message says so. Anything asserting about
pools, transactions, advisory locks or SQL belongs in an opt-in test, because
an in-suite version passes against broken code without noticing.

## Risk charter

The user's charter is a hard limit and binds on every swap surface. The inline
chat swap card never called the veto, so a trade the dialog and the chat card
both refused went through it. All three now check at quote time and again
immediately before signing, since the charter can be tightened while a quote
sits on screen.

## Consolidated review of 2026-09-17: the five P1 findings

The report and its evidence live in `reports/review-2026-09-17/`. Its own
reproductions assert the defects; `tests/test_review_20260917_p1.py` asserts the
rules, using the same inputs, with a control beside every refusal. Each fix
was checked both ways: the regression test passes and the review's check fails.

**R01, untrusted Markdown could inject attributes.** The shared HTML helper
serialised a text node, which escapes `&`, `<` and `>` and leaves both quote
characters alone, so a Markdown link whose URL carried a `"` closed the `href`
and added an `onmouseover`. Both helpers (`escapeHtml` in the app, `esc` in the
admin shell) now escape quotes, so the same value is safe in text and in any of
the attribute templates that use them. Link targets are additionally validated
as plain `http(s)` URLs with no quotes, brackets, whitespace or control
characters before insertion; anything else is left as literal text.

**R02, API keys could manage the account.** The signed-in dependency accepted a
key as signed in without regard to scope, so a key holding only `data` could
change preferences, delete every conversation and revoke every browser session.
There are now three dependencies and the authorization matrix declares which
each route uses: `browser` (a cookie session; every `/me` and `/billing`
route), `key:chat` (chat and history) and `key:data` (portfolio, wallet health,
credit balance). A key is refused everywhere else with 403. Unknown scope names
used to be filtered out and then, with nothing left, expanded to *every* scope;
they are now a 400. That expansion is why one of this project's own earlier
"restricted key" tests had been exercising a full-power key.

**R03, the server wallet had no entitlement.** A plan is bound to the account
that asked for it and execution checks that the plan's wallet is the server's;
neither proves the account may spend from that wallet, so any signed-in account
could address a quote to the signer's public address and confirm it.
`CUSTODIAL_SIGNING_PRINCIPALS` lists the user ids entitled to the server-held
key. It is checked when a plan is quoted against the signer and again when the
server is asked to sign, and the caller must also own the plan. Custodial
signing enabled with an empty list is a startup error, since that is custody
for nobody.

**R04, late billing events restored paid access.** Cancellation used to erase
the subscription id, so a late update, invoice or checkout for that very
subscription found "no current subscription" and re-entitled the account.
Cancellation is now a tombstone: the id and timestamps stay, the status becomes
terminal, and only an event demonstrably newer than the cancellation may move
it (Stripe reactivates before period end). Every subscription-state writer
receives the event envelope's `created`, recorded as `subscription_event_at`,
which orders events for the *same* subscription; a subscription's own creation
time orders *different* subscriptions. The invoice line loop resolves the plan
from the price first and metadata second, as the subscription resolver already
did; credits for a paid invoice are always granted, entitlement only when the
invoice belongs to the account's current, non-terminal subscription.

**R05, plan changes started a second subscription.** Checkout always creates a
new subscription, so a subscribed account choosing another plan paid for two
while the app remembered one. A subscription checkout is refused with 409 for
an account whose recorded subscription is not known to be terminal, and the UI
sends that case to the billing portal, which edits the subscription Stripe
already has. Account deletion now lists every live subscription for the
customer and cancels each, not only the one id on record.

## Consolidated review of 2026-09-17: the eight P2 findings

Verified the same way as the P1s: `tests/test_review_20260917_p2*.py` assert
the rules with the review's inputs, and the review's own checks fail against
the tree. One exception is recorded honestly below.

**R06, public cost admission could be reset from the client.** The trial
account is keyed on a browser-supplied device id, so rotating it minted a fresh
trial and a fresh chat rate bucket. The device key stays (two people behind one
NAT should not share credits), with server-controlled budgets on top: a per-IP
cap on new trial grants per day, and a per-IP chat rate bucket the client
cannot rotate away from. The public knowledge route now has a query size bound,
the caller's and the network's rate buckets, a concurrency cap, and a daily
spending ceiling for the whole deployment. The review's rotation check still
passes because it rotates twice against a default budget of three; the
regression tests set the budget to two and prove the third device gets nothing.

**R07, two brief occurrences could spend the same last credit.** The brief
path read the balance and then appended a debit, unlocked. Every spender now
goes through `credits.charge_once`: one atomic check-and-debit under the same
per-account lock chat reservations use, idempotent on the occurrence. The
charge is taken before the brief is composed and refunded if composition
fails, and the payer is resolved through the same team validation identity
uses, without its monthly-grant side effect. The review's check for this one
hangs rather than fails: it synchronises two callers at the balance read and
waits for both, and the first now holds the lock while it waits, so the
second can never arrive. A mutex makes that gate unreachable by construction.

**R08, conversation ownership was not part of the turn.** A server-minted
conversation id was mapped to its owner only after the answer, with failure
swallowed, so under a database fault a private answer came back on an unowned
id. The claim now happens before any work for minted ids as well as named
ones, and if it cannot be recorded there is no turn. Bulk deletion takes each
conversation's turn lease like single deletion does; conversations mid-turn
are kept, still owned, and reported.

**R09, task recovery could lose or repeat work.** An evaluation that failed
was returned as "did not fire", so a one-shot brief whose provider was offline
was marked done. Failures now raise, and the recovery handler keeps the
occurrence identity across the retry, which the claim prefers over the new
scheduled time. The inbox row is the durable delivery record, keyed by
occurrence, so a retry of an occurrence that delivered is a no-op. Email
failure behaviour, stated: the inbox row stands as the delivery of record and a
failed send is recorded and not retried, because a retry cannot tell a lost
email from a late one and a duplicate brief is the worse outcome.

**R10, team membership was not exclusive and seats were not serialised.**
Joining team B now leaves team A in the same step, an owner removing a member
clears the user's team pointer only if it still points at that owner, and
invitations count and insert under a per-team lock.

**R11, advertised entitlements differed from enforced ones.** A member's task
limit, API-key eligibility and chat rate now come from the effective plan the
UI shows, resolved once and shared, with the global chat rate kept as the
ceiling.

**R12, ingestion could neither recover nor reindex.** A document is now "done"
only when its content, a pipeline fingerprint (embedder, dimension, extraction
version) and a completion flag all match. A failed derived write leaves the
document incomplete and the next pass resumes from the derived step without
re-embedding; a changed embedder forces a new version with fresh vectors even
when the content did not change.

**R13, the shipped Redis could evict a held lock.** The compose file now runs
`noeviction`: this instance holds turn locks, sessions and retained history,
none of which may silently disappear. A write refused for memory surfaces as a
typed error the chat path answers with 503, which is the safe failure. The
regression test starts a real one-megabyte `redis-server` under each policy so
the difference is demonstrated rather than asserted from documentation; it
skips honestly when the binary is absent.

## Verification of 8cbbd1bc: the eight partial closures

The verification report and its evidence live in `reports/review-2026-09-17/8cbbd1bc/`.
`tests/test_review_20260917_p3.py` and `tests/test_postgres_team_operations.py`
assert the rules; the verification's twelve reproductions fail against the tree.

**R02.** Preparing, submitting or having the server sign a transaction is a
browser act. All three execution routes require a browser session; an API key
may quote through chat and may not move funds, whatever its scope. Without
this a data-only key belonging to an allowlisted account could have the server
sign that account's plan, because scope was never consulted on the way to the
custodial check.

**R04.** Every entitlement writer now decides and writes under the account's
row lock (`SELECT ... FOR UPDATE`, with the write on the same connection), so a
slower older webhook cannot overwrite a newer one. Two events for the same
subscription in the same second cannot be ordered by time, and Stripe does emit
them: checkout completion and the first subscription update usually share a
second. Neither last-arrival-wins (which reordered state) nor first-wins (which
would drop legitimate events) is right, so a tie is reconciled: with Stripe
configured the subscription is retrieved and its state applied, independent of
delivery order; without Stripe, an event that agrees with the recorded state
passes through and a conflicting one is ignored with a warning. A tying
deletion is applied, since it is the terminal state and any later event says
otherwise.

**R05.** A subscription checkout is recorded on the account when it is created
and cleared by its completion webhook; a second one is refused while the first
is in flight. Before creating one, Stripe is asked whether the customer already
has a live subscription the app has not heard about, and if so it is recorded
and the checkout refused. Cancellation on deletion is now as cautious as
checkout: a recorded subscription whose state is not known to be over is
cancelled, and if Stripe is not configured the deletion does not proceed.

**R08.** Transcript and ownership are removed together, inside the turn lease,
so there is no instant at which a turn can commit private history to a
conversation whose ownership is about to vanish. A turn that starts during
deletion waits and finds an empty, unowned conversation. Account deletion
uses the same transition and refuses with 409 while a conversation is mid-turn.

**R09.** A retry keeps its payment: the occurrence's charge is not refunded on
failure, and the retry, running as the same occurrence, delivers against it.
Retries are bounded by `TASK_RETRY_LIMIT`; when exhausted, the occurrence is
refunded and dropped, and the next run is a new, separately paid occurrence.

**R10 and R11.** Every database operation inside a gate rides the gate's
connection: invitations insert through it, and the effective plan a task
limit needs is resolved before the gate is entered, not inside it. Team joins
run under the user's row lock and the database itself refuses a second active
membership through a partial unique index.

**R12.** The pipeline fingerprint names the provider and the model, not only
the provider, so a change between models of one provider forces fresh vectors.

## Verification of a1b477ba: the five remaining partial closures

The verification of `a1b477ba` closed eight of the thirteen consolidated
findings and reproduced five that were still partial. Each reproduction was
confirmed at that commit before anything changed, and each is now a
regression test with its assertion inverted plus a control
(`tests/test_review_20260917_p4.py`; the Postgres-only parts in
`tests/test_postgres_review_p4.py`).

**R04, one writer path.** Every subscription-state writer -- update,
deletion, checkout completion, invoice -- now goes through
`billing._apply_subscription_event`, so ordering, tie reconciliation and the
mapping from a subscription's state to the account's fields cannot drift
apart. They had: a checkout took a same-second tie as agreement "by
construction" and a checkout tying with a recorded cancellation restored paid
access; an invoice took the reconciliation as a yes/no and wrote its own plan
and `active` over a cancelled subscription. On a tie the authoritative
subscription's own status and plan are what get written, for every writer. And
when Stripe cannot be asked, nothing is decided: `ReconciliationUnavailable`
propagates, the webhook answers 500, the event stays unprocessed, and
Stripe's redelivery tries again. Marking it processed turned a moment's
outage into a silently dropped entitlement change. Invoice credit grants stay
separate from the entitlement decision: the payment happened, the credits are
granted, and whether the account is on that plan is answered by the writer
path.

**R05, a durable checkout intent.** Starting a subscription checkout now
records an intent (`checkout_session_id = intent:<key>`) under the account's
lock, commits it, and only then calls Stripe with that key as the request's
idempotency key. If the outcome is never recorded -- the process dies, the
write fails -- the next attempt replays the identical request and Stripe
answers with the session it already created. A recorded session is settled
with Stripe before anything replaces it: open and within the window, it is
handed back rather than duplicated; open past the window, it is expired at
Stripe first; complete, the subscription is being activated and no
replacement is allowed; anything Stripe cannot confirm fails closed. Sessions
are created with `expires_at` at the end of the pending window (never under
Stripe's 30-minute minimum), so the local timer never outlives what Stripe
would still let the customer pay.

**R08, admission re-checked under the lease.** Ownership was checked and
claimed before a chat turn's session lease, and the wait for the lease is
where that goes stale: a deletion under the lease removes the mapping, another
account claims the id, and the queued turn committed a private question and
answer into that account's conversation. `execution_policy._require_still_admitted`
now confirms, under the lease and before anything is read, that the
conversation is still what the caller was admitted to (theirs, or nobody's
for a visitor); anything else is "not found", the same answer the first check
gives. The post-turn ownership refresh moved under the lease too, so it can
never re-map a conversation a deletion has just forgotten. Account deletion is
covered by the same check: its conversations are forgotten under their
leases before the account row goes.

**R09, retry state and delivered work.** `retry_count` was written to
Postgres but never read back from a row, so every attempt at an occurrence
was attempt one and the bounded retry never bounded. It is hydrated now, and
the opt-in test drives it through real reads and claims. The terminal refund
also consulted only the ledger, which cannot tell "never delivered" from
"delivered, then failed to record it". The inbox row keyed by occurrence is
the delivery record, and the recovery consults it first: a delivered
occurrence is completed (its records written, its schedule advanced) and never
refunded or re-composed; only an occurrence that never delivered is refunded
when its retries run out.

**R10, joins that write nothing when rejected; the migration.** The Postgres
join deleted the user's current membership before establishing that the
destination had a valid invite, and a missing invite returned `None` from
inside the transaction, committing the deletion. The invite is now selected
and locked first; a rejected join has written nothing. Nonexistent, revoked
and already-accepted invites are tested. The one-active-membership index is
added only after the data is reconciled the first time the schema runs
against a database that predates it: of a user's active memberships the one
their team pointer names survives (with no pointer to any of them, the most
recently accepted), the others are moved to `team_members_reconciled` with a
reason, and the pointer is set to the survivor. The test starts from the
older data state, not an empty schema.

**Not established by these checks.** The Stripe interactions
(`Subscription.retrieve`, `checkout.Session.retrieve/expire`, idempotency-key
replay) are exercised against stubs that model Stripe's documented behaviour;
no live Stripe call was made. The R05 reproduction's own stub returned a
distinct session per create call regardless of key, which Stripe does not do;
the regression test's stub honours the key, and the invariant proved is that
the retry is the identical request under the same key.

## UI QA of 2026-09-17: the six confirmed findings

Browser QA (109 recorded checks) confirmed six interface defects. Three were
the same shape: a handler that ignored the server's answer and showed
success. Each is fixed and has a browser-code regression in
`tests/test_ui_qa_20260917.py`, which runs the real inline script from
`index.html` in `tests/js/ui_harness.mjs` with the server answering 503, plus
the control that the confirmed path still completes.

- **UI-01, delete all conversations.** Every response is checked. Only what
  the server confirmed deleted leaves the list; conversations whose delete
  failed stay, the account's list is re-read from the server, the dialog
  stays open and a status line says what could not be deleted (including
  conversations the server kept because a request was still running). The
  single-conversation delete reports a failure on its button instead of
  dropping the row.
- **UI-02, preference saves.** Appearance settings (theme, auto-scroll) are
  this browser's and are kept locally whatever happens. Account preferences
  are written locally and applied only once the server has confirmed the PUT;
  a failure keeps the dialog open with the typed values and a "Not saved"
  status, and the button is re-enabled for a retry.
- **UI-03, team departure and member removal.** The panel changes only after
  the server confirms; a failure keeps Members open with a retryable error.
  The remove-member handler, which had the same shape, reports its failure
  too.
- **UI-04, token suggestions behind the header.** `placeMentions` measured
  the room above the composer against the viewport edge; the header is not
  usable space. It now measures against the header's bottom edge, opens
  below when there is not enough room above, and caps the list to the space
  on the side it opens. Measured live at 1440×1000 with the composer at
  y=314 (the QA geometry): the list opens below at y=455 and
  `elementFromPoint` on the first row hits the row.
- **UI-05, arbitrary text as a wallet address.** A public address must be a
  Solana public key or an Ethereum address (`0x` + 40 hex); anything else
  shows an inline error and the dialog stays open. The Solana check decodes
  the base58 and requires exactly 32 bytes: the alphabet-and-length regex it
  started as accepted strings that cannot be a key (43 ones decode to 43
  bytes, 44 z's to 33). The browser decoder was cross-checked against
  `solders.Pubkey.from_string` on the same strings. An adopted public address is labelled "view only" and the wallet
  control carries a `readonly` class and title, so it cannot be mistaken for
  a signing wallet.
- **UI-06, admin pages at 320 px.** The outer layout could not shrink: nav
  links were `nowrap`, the topbar and its children had no `min-width: 0`,
  the footer could not wrap. Grid and flex children now shrink, long words
  wrap, the body never scrolls sideways, and the provider table keeps its own
  horizontal scroll. Measured in Chrome in a 320×844 viewport: with the
  pre-fix stylesheet the QA's numbers reproduce (document 330 px; hero, auth
  form and panel at 330); with the current one the document is 320 px and no
  outer element exceeds it, on both the admin and the knowledge pages.

Rerun: Playwright is now a dev dependency (`npm install`; it drives the
installed Chrome), and the QA's own proof scripts were rerun unmodified against
an in-memory instance. Every finding's check inverts -- the delete-all dialog
stays open with its status line and the conversation kept; the preferences
dialog stays open with the error visible and nothing persisted; the team
departure stays on Members with the error visible and the membership intact;
the first suggestion row is hit-testable; `not-a-wallet` is not adopted; the
admin document measures 320 px with nothing overflowing. The two followup
checks still failing are the reviewer's own noted harness problem in the
team-leave step (superseded by their team-retest, which passes) and the swap
shortcut waiting for a relay card absent from the live news set.

## Streaming: the turn rendered as it happens (2026-09-17)

`POST /chat/stream` is the same turn as `POST /chat` -- same admission, lease,
credits, persistence and gates, because it runs `execute_chat_turn` itself --
delivered as server-sent events instead of one JSON body at the end:

| event | when | payload |
|---|---|---|
| `status` | routing decided; each tool starting; the synthesis starting | `text` |
| `card` | the moment a tool's output is usable | `markdown`, `tool` |
| `delta` | each token of the synthesis, as it is generated | `text` |
| `done` | the turn is persisted and charged | `data`: the complete AgentResponse |
| `error` | the turn was refused or failed | `status`, `detail` -- what the JSON route would have answered |

The channel is a per-turn ContextVar (`app/streaming.py`); `emit` is a
no-op when nobody is streaming, so the JSON route and the MCP server are
untouched.

Every turn streams, not only a composed one (second pass, same day: the
first pass streamed only multi-tool turns, so a general reply, a
single-tool answer, a ReAct research turn, a deep dive and a knowledge
answer still arrived whole and looked unchanged):

- `runtime.answer(program, field="answer", tier=...)` is the one call a
  node makes for the text the user reads. With a client listening it runs
  the program through DSPy's `streamify`, hands the field over token by
  token, and reports each tool the program runs as a status line;
  otherwise it is the ordinary bounded call on the named tier. The general
  node, the ReAct research agent, the portfolio and trade-simulation
  agents, the trade planner, the knowledge synthesizer, the deep dive, the
  equity brief, the composite summary and the team coordinator all go
  through it.
- `ProviderRouter._invoke_with_retry` emits `Running <tool>` -- every real
  provider call passes there, so a status line precedes every card whatever
  path chose the tool. Deterministic intercepts (market overview, events
  calendar, wallet portfolio, direct MCP lookups, the Jupiter quote, the
  charter check) announce themselves the same way.

A single-tool turn still delivers its card with `done` (there is nothing to
show before the one tool returns); what it gains is the status line and,
when a model writes the answer, the tokens.

The browser tries the stream first and falls back to the JSON route when
the response is not an event stream (a proxy that buffers or strips it) or
when x402 payment wrapping is on. It renders each card as it arrives, grows
the streamed text token by token (rendered as markdown as it grows, so lists
and emphasis appear in place), and on `done` renders the final answer exactly
as before. A streamed `error` is shaped like a failed fetch, so the existing
404 (conversation gone -> retry fresh) and 409 (stale revision -> retry)
handling still applies.

Measured on the dev instance for "is BONK safe": routed at 0.2 s, first
card at 4.0 s, second at 12.3 s, first synthesis token at 13.6 s, done at
15.5 s -- against ~15 s of blank typing indicator before.

Behind Caddy nothing needs configuring (it streams chunked responses); the
`X-Accel-Buffering: no` header covers nginx if one is ever put in front.

A client that goes away mid-turn (an iPhone locking its screen, a tunnel
hiccup -- seen as "Could not reach the assistant: Load failed" on the
installed app, 2026-09-18) does not cancel the turn: it was admitted and
charged, it finishes and persists like a JSON turn, and the browser recovers
the answer by polling the conversation's history for up to two minutes,
rendering it exactly as `done` would have. The stream also writes a
`: keepalive` comment whenever nothing has been sent for
`STREAM_KEEPALIVE_SECONDS` (15), so a slow tool never looks like a dead
connection to a proxy.

## Installable app and phone layout (2026-09-17)

The chat UI is a progressive web app: `/ui/manifest.webmanifest` (standalone
display, `/ui/` scope, 192/512 and maskable icons under `/ui/icons/`), the
`apple-mobile-web-app-*` tags and `apple-touch-icon` for iOS Safari's Add to
Home Screen, and a service worker at `/ui/sw.js`.

What the worker does and does not do:

- Precaches the shell (index, the four stylesheets, manifest, icons) on
  install; navigations under `/ui/` are network-first, each page cached
  under its own path (query strings ignored) so Admin never replaces the
  offline chat shell and offline Admin comes back as Admin; an error
  response never replaces a cached page; other files under `/ui/` are
  served from cache and refreshed in the background.
- Activation deletes only Orbit's own older caches (`orbit-shell-*`); another
  app's caches on the same origin are left alone.
- Never intercepts anything outside `/ui/`: `/chat`, `/chat/stream`, `/auth`,
  `/me`, `/billing` and every other API route reach the server exactly as
  before. tests/js/sw_harness.mjs proves this by running the worker in a vm.
- The `/ui` no-cache middleware also covers `sw.js`, so a deploy is picked up
  on the next open. Bump `VERSION` in `sw.js` when the shell's file list
  changes; the old cache is dropped on activate.

The drawer shows an install card: on iOS Safari (not standalone) the
Share -> Add to Home Screen steps; on browsers that fire
`beforeinstallprompt`, a real Install button. "Not now" hides it for 30 days.

`mobile.css` is loaded last on both pages and holds the touch and phone
rules: 16px fields (iOS zooms on focus below that), 42px targets, safe-area
insets for the topbar, composer, drawer and dialogs in standalone mode (the
topbar grows by the status-bar inset instead of losing that height),
full-width assistant messages with a small badge, bottom-sheet dialogs with
side-by-side footer buttons, a fading settings tab strip, and the admin
page's forms and tables at phone width.

Phone audit: `node scripts/ui/mobile_audit.mjs <outDir> http://localhost:8000`
drives every screen with Playwright's iPhone 13 emulation (home, drawer,
history, tasks, each settings tab, plans, wallet, a streamed answer, a swap
turn, sign-in, admin), saves a screenshot per screen and prints horizontal
overflow, sub-32px targets and sub-16px fields as JSON; rerun it after any
layout change. Icons are regenerated with
`node scripts/ui/make_icons.mjs app/static/icons`.

## Review of 2026-09-18: two P1 findings

**A new API-key secret outlived the account that made it.** The one-time
secret box in Settings was never cleared, so after signing out, or after
another account signed in on the same tab, the previous account's secret was
still on screen. The secret now belongs to the account id that created it:
`renderAccount` clears the box whenever the tab is signed out or a different
account is rendered, and sign-out clears it before the request is sent.
Browser-harness regression: `api_key_secret_is_cleared_on_sign_out_and_account_switch`.

**Deleting an account left its open Checkout payable.** Cancelling the
subscription was not enough: a Checkout session stays payable for up to 24
hours, and paying it after the deletion created a subscription for a Stripe
customer no account maps to. Closed on both sides:

- `billing.expire_open_checkouts_for` runs during `DELETE /me`, after the
  subscription cancel: Stripe is asked for every open session of the customer
  (not only the id the account recorded) and each is expired; a session
  Stripe will not confirm closed raises `CheckoutCloseFailed`, which stops
  the deletion with a 409 the user can retry.
- The `checkout.session.completed` handler, when no account maps to the
  session, cancels the subscription the session created (`orphan_cancelled`)
  rather than returning `no_user` and leaving it running.

Tests: tests/test_review_20260918_p1.py (all four fail on the code before
the fix and pass after).

## TradingView: charts under answers, and data on the user's own account (2026-09-18)

Two separate things, one provider.

**Charts.** A research answer that resolved a token, or researched an
equity, carries `chart` in the response (`ChartCard`: exchange-qualified
symbol, label, interval, kind) and the browser draws it with TradingView's
embeddable Advanced Chart widget under the answer -- no account, no key,
attributed per TradingView's widget terms. `app/charts.py` names the symbol
through TradingView's public symbol search (most liquid USD-quoted spot pair
for a coin, primary listing for a stock; cached six hours) and falls back to
TradingView's aggregate index (`CRYPTO:SOLUSD`) or the bare ticker, both of
which the widget resolves itself. The card is stored with the turn, so a
reopened conversation draws it again.

**Data.** TradingView's MCP server (https://www.tradingview.com/mcp/docs)
answers quotes, technicals, fundamentals, forecasts, news, filings and
calendars for crypto and equities -- for a signed-in TradingView user over
OAuth 2.1, with no service credential. Orbit therefore never holds one
TradingView login for everyone (that would be one paid seat serving many
users, and against the platform's terms). Each user links their own account:

- Settings › Account › Connections › TradingView › Connect sends the user
  to `/integrations/tradingview/connect`, which registers Orbit as an OAuth
  client at TradingView on first use (dynamic client registration; the record
  is in `oauth_clients` and re-registered when `PUBLIC_BASE_URL` changes),
  then to TradingView's authorization page (authorization code + PKCE +
  the MCP resource indicator). The callback stores the tokens in
  `user_integrations` for that user only; refresh happens a minute before
  expiry; Disconnect revokes and forgets. Deleting the account cascades.
- Every chat turn binds the user's access token to a ContextVar
  (`app/integrations/tradingview.py`); the four router tools
  (`tradingview_snapshot`, `tradingview_financials`, `tradingview_news`,
  `tradingview_earnings`) match only while a token is bound, so a user
  without a connection never sees them chosen. The equity research brief
  adds TradingView fundamentals, forecasts and the quote to its evidence,
  and the token deep dive gains a technical-indicator dimension, when the
  user is connected.
- Rate limit: about 100 requests per minute per user at TradingView. Beta:
  market data may be delayed and the tool list may change.

`TRADINGVIEW_ENABLED=false` hides the whole feature. `PUBLIC_BASE_URL` must
be the public HTTPS origin: it is the OAuth redirect. Tests:
tests/test_tradingview.py; the real registration and authorize URL were
verified against TradingView from the dev instance.

## Signing in from a phone (2026-09-18)

Two faults seen on an installed iPhone app. The emailed link opens Safari,
whose cookies the home-screen app does not share, so the app stayed signed
out even when the email arrived. The same email now carries a six-digit
code; the sign-in sheet shows a code field once the email is sent, and
`POST /auth/email/code` (email + code) signs the app in on its own origin.
The code resolves to the link's own one-time token, so using either spends
both; five wrong guesses burn it; the /auth rate limit applies. And the
Privy email wallet connected but never signed the account in: Privy's
embedded Solana provider takes a base64 message and answers with a base64
signature, and the page passed raw bytes, failing silently. A wallet
sign-in that fails now says so on the wallet panel.

Email delivery itself: with Resend in testing mode only the Resend account
owner's address receives mail, and the sender must be `onboarding@resend.dev`
or a verified domain. For the beta, verify a domain in Resend and set
`EMAIL_FROM` to an address on it. Accepted sends log Resend's message id.

## Social-trending asks (2026-09-18)

"Trending meme on twitter socials" was answered with DEX Screener's narrative
categories by trading volume: a market table for a social question. The
router now has `x_social_trending` (capabilities market_sentiment,
token_discovery; dimensions social, narratives): what crypto Twitter is
posting about, market-wide, measured by 24h interactions when
`LUNARCRUSH_API_KEY` is set, else reported from a web search over X posts
with the accounts and links, labelled as such; a "meme" ask asks for
memecoins rather than the majors that always lead a mention count. The
narratives tool is `not_for` social, so it no longer claims these asks;
`x_kol_sentiment` still takes the one-token form ("what is CT saying about
BONK"). Eval cases social-trending-memes and social-trending-ct.

## Routing: one vocabulary, a gate, and a look-up before a guess (2026-09-18)

Why (user: "I don't want this to happen again and again"): the same class of
miss kept recurring. "Audit report on ANSEM" went to web search and came back
with French public-sector audit reports; "Audit report on ANSEM TOKEN" asked
"Which token should I check?" although ANSEM had just been resolved; "is it
audited?" reached the router and lost the security tool. Three causes, three
mechanisms:

- **One vocabulary.** Six hand-kept copies of the security words (the
  Layer-1 rule, the research intercept, two tool matchers, the catalog's
  dimension pattern) had drifted; "audited" was known to some and not to
  others. Every layer now reads `lexicon.SECURITY_WORDS`. Never add a word to
  one layer: add it there and add a phrasing to the gate.
- **A gate in the suite.** `tests/test_routing_gate.py` runs the whole
  router-mode eval (`scripts/routing_eval/cases.json`) on every commit with
  zero misses allowed, and checks sixteen security phrasings against every
  layer, the Solana security tool's own matcher and the multi-tool plan.
  A misroute is now a failing test, not a transcript.
- **A look-up before a guess.** When the classifier is unsure and no rule
  anchors the turn, `app/routing/subject_probe.py` asks the web search
  what the message's subject is (plain web search beat the finance-tuned
  one on memecoins at the same price) (one strict-JSON call, cached an hour)
  and routes on the answer: a token becomes token research on its chain
  (the request is rewritten "…(GIGA token on solana)"), a stock equity
  research, a protocol a knowledge question, a person or company web
  research. Only when nothing settles it does the turn ask the user back.
  Live: "what about GIGA" and "PUMP" route as Solana tokens, "Reliance
  results" as an NSE equity.

Also: a security follow-up with no token named ("is it audited?") takes the
token in the conversation's focus instead of asking.

## Search first, then the tools; unlock schedules; related questions (2026-09-18)

Three changes from one transcript ("OPEN token unlock schedule" answered
with a DEX pair search, compared side by side with Perplexity's answer).

**Search first when the router is unsure.** User rule: "if we are not
confident enough, route to Perplexity search or finance search, synthesize,
and then decide the tools later." The uncertain branch of the resolver now
runs two look-ups at once: the strict-JSON subject probe (which decides the
route, as before) and `subject_probe.context_search`, the question itself
put to Perplexity (finance search for a market-shaped message, web search
otherwise, cached fifteen minutes). The web's answer travels with the route
as `web_context`; the research node shows it as the first card the moment
the turn starts, runs the tools the probe chose, and synthesizes the web
card and the tools' cards together (`perplexity_context_search` is the first
trajectory step). When nothing settles the subject but the web answered,
that answer stands on its own with `web_research` capabilities instead of a
clarifying question; only with neither does the turn ask the user back. An
anchored rule or a confident classifier never triggers either look-up, so
the ordinary turn costs nothing extra.

**Unlock schedules.** `defillama_token_unlocks` (app/token_unlocks.py) owns
unlock/vesting/emissions/cliff questions that name a token. It finds the
slug by ticker in DefiLlama's emissions index (`emissionsIndex`: slug, name,
ticker, token reference), so ARB is `arbitrum` and never the
`arbitrum-exchange` namesake the old prefix match picked; a request that
carries a contract is verified against the entry's token reference and a
namesake is never shown. When DefiLlama lists nothing for the ticker
(OpenLedger's OPEN), the tool answers from Perplexity finance search with
the project's published vesting terms, marked as web-sourced, with the
DefiLlama gap stated in one line. The catalog's `unlocks` dimension keeps
the pair-search tool out of these asks.

**Related questions.** The quick-action chips removed on 2026-09-14 for
being generic are not back. Instead, app/followups.py writes 3-5 candidate
follow-ups from the question and the answer on the primary model and keeps
only the ones grounded in the answer (a name, ticker, domain or figure the
answer actually contains; never a pronoun subject; never the user's own
question); fewer than two survivors means no section. Only research and
general answers with substance get them: never a trade turn, a clarifying
question, or an error. They run alongside the chart card, bounded to eight
seconds, and reach the browser as `suggestions` on the response and in
history; the browser renders a "Related" list under the answer and a tap
sends the question. `FOLLOWUPS_ENABLED=false` turns the whole thing off. The
suite runs with it off and opts in per test.

### Twenty uncertain prompts, live (2026-09-18): what the run found and what changed

`reports/uncertain-live-20/` scored 7 pass, 6 partial, 7 fail on a
deliberately hard set. Fixes, each with the run's evidence as a test in
`tests/test_uncertain_live_20.py`:

- **One subject per turn.** The probe and the context search must agree
  before the web answer rides along: a token, equity or protocol needs
  market text (`app/clarify.py`), so TRUMP keeps the memecoin route and
  drops the politician (`routing_decision.web_context =
  "dropped:off_subject"`). A probe that says "concept" (Mercury retrograde)
  no longer routes; with an off-market web answer the turn asks, naming
  what the web read the word as.
- **A clarification is terminal.** The web's own "which unlock do you
  mean?" is recognised (`clarify.is_clarification`) and ends the turn as a
  question; the research node never merges a web card onto a clarifying
  answer; related questions are never generated for one.
- **An unlock ask with no token** takes the conversation's focus token or
  asks; it never falls to the market calendar. The calendar intercept also
  ignores any question that names a ticker ("what's happening with
  FARTCOIN?").
- **No audit claim without an audit report.** `composition.audit_guard`
  puts a correction in front of a summary that calls a token audited when
  no card names an audit report or auditing firm; the synthesis signature
  says the same.
- **Web questions about ticker-like names are scoped to markets** ("What is
  OPEN?", "Is M safe?", "Research ARC") so the search reads the word as a
  token, protocol or company.
- **Related questions** drop predictions, effects, advice and questions
  aimed at the user; the model bound is six seconds.
- **Unlock web fallback** asks the search to report conflicting figures
  side by side, never to reconcile or derive a monthly amount. The
  emissions index (31s cold) is fetched at startup
  (`WARM_CACHES_ON_START`).

Still open from the run: confident-route identity misses (VIRTUAL, KITE,
Apple token) are classifier and knowledge-base gaps, not the uncertain
branch; latency (median 16s) is dominated by tool fan-out and Perplexity.

### A knowledge miss is never the answer (2026-09-18)

"Who are the investors backing EigenLayer?" was answered with "the provided
passages do not include information about the specific investors" and four
forum links: the knowledge base had passages about the project, not about
the question. The knowledge synthesis is now asked to reply `NOT COVERED:
...` when the passages do not answer, `research.knowledge_missed` recognises
that marker and the prose variants, and `_synthesize_knowledge` then answers
from a crypto-scoped Perplexity web search with the gap stated in one line.
In a multi-tool plan a missed knowledge card is dropped rather than
synthesized with the others. Live, the question now returns the seed,
Series A, Series B and token-purchase rounds with the investors named.

### The trading desk on its own; no switch in the page (2026-09-18)

User decision: keep the desk, do not make it the default for everything,
"make it auto in the backend whenever required", and "remove it from UI".
`resolver._desk_wanted` sends a turn to the desk when the classifier calls
it advice about a named asset ("should I buy BONK here?", "thoughts on
$WIF") and it routed as research or a trade simulation. A factual lookup,
a security check (the dossier answers), an equity question, a plain swap
and a quick action never go on their own. `routing_decision.team_auto`
marks such turns; the desk turn is still billed at `credit_cost_team_turn`.
`TEAM_DESK_AUTO=false` turns it off. The session's `team_mode` (the chat
phrase "enable team mode", the MCP tool) still forces the desk for every
content turn. The composer's "In-depth" switch is gone; the page keeps a
no-op `syncTeamMode` because answers and history still carry the field.

### TradingView connect: discovery timeouts (2026-09-18)

"TradingView could not be reached to start the connection" was a 10-second
read timeout fetching the protected-resource document, which answered
normally a minute later. Discovery now tries twice with a 20-second read
timeout and names the document in its error, and the metadata is fetched
at startup (`WARM_CACHES_ON_START`) so a click does not pay for it.

### A ticker shared by several tokens is a question, not a pair dump (2026-09-18)

"open token details and current price details" returned a DEX pair table
mixing OpenLedger, an index token and "Open tokens" across four chains as
if they were one token. Two causes: the named-token pattern did not read a
lower-case name before "token details", so the resolver never engaged; and
when the resolver finds namesakes but no verified or clearly dominant one,
it left the raw ticker to the pair-search tool. Now a lower-case name
before "token" plus a data noun is a ticker, and unsettled namesakes become
a question listing each candidate with its chain, name and liquidity
(mirror-chain listings excluded), remembered for the one-word follow-up.
Also found: the equity rule anchored on any cashtag, so "thoughts on $WIF"
was a stock question; a cashtag now anchors equity only for a known
instrument or an exchange-prefixed symbol.

### A pasted token-page link is the token (2026-09-18)

A CoinMarketCap link to OpenLedger was answered with "I couldn't access the
CoinMarketCap page" (the site blocks fetches). `app/token_pages.py` reads
CoinMarketCap and CoinGecko slugs (CoinGecko's coin API, then its search
when the slugs differ), DEX Screener, Birdeye and explorer links into a
symbol, name, chain and contract; picks the chain where that contract has
the most DEX liquidity; and the research node rewrites the turn into a
token question about that contract, answered from our own tools, with one
line saying how the link was read. News and docs links are still fetched.

### The answer gate: correct over fast (2026-09-18)

User rule after a day of per-phrasing fixes: "1000 ways of asking the same
question ... even if we have to degrade performance the result should be
correct for the query. It is always better than a wrong answer." Routing
fixes guard the way in; `app/answer_gate.py` guards the way out. Every
research answer (not a clarification, trade card or error) is judged by a
small model on two tests: is it about the subject the user named, and does
it give what was asked. A failing answer never ships: the question as
asked goes to the web, the web's answer is judged once more, and if that
fails too the user gets a question naming what could not be found and
what the data was about instead. A wrong-subject answer is dropped; a
right-subject but incomplete one is kept under the web's answer. Cost: one
small-model call per research turn (about a second) and a web search on a
miss. `ANSWER_GATE_ENABLED=false` turns it off; the suite runs with it off
and `tests/test_answer_gate.py` opts in. `answer_gate` on the result
records the verdict and how it was resolved.

Rule for future transcripts: do not add a phrasing regex. Add the
transcript as a test; if the gate let a wrong answer through, sharpen the
AnswerCheck signature.

### Linked pages are read three ways and summarized by our model (2026-09-18)

A pasted link used to go straight to Perplexity's fetch tool, and a page it
could not read came back as "I couldn't access the page". `app/url_reader.py`
(router tool `url_reader`, ahead of `perplexity_fetch_url` for `url_fetch`)
fetches the page directly with a browser-like request and extracts the
article text with the standard-library parser, then falls back to
Perplexity's reader, then to a Perplexity web search about the link (a
post the index has seen even when the site blocks readers). The text is
summarized by the primary model against what the user asked, with the
source named. Only when all three fail does the answer say what was tried
and ask for the text or the token. Token pages never reach it: the
research node turns CoinMarketCap, CoinGecko, DEX Screener and explorer
links into token questions first.

### The gated re-run of the twenty prompts (2026-09-18)

`reports/uncertain-live-20-gated/` re-runs the reviewer's twenty prompts
with the answer gate on: 19 pass, 1 partial, 0 fail (was 7/6/7), median
turn 12.9s (was 16.1s) because fewer turns fan out to tools that cannot
answer them. The gate blocked three answers (HYPE wrong subject, KITE
incomplete, Mercury the planet) and passed fifteen untouched.

Two fixes came out of that run and are in the suite:

- **A ticker means the crypto asset the market lists.** `app/symbol_registry.py`
  reads CoinGecko's ranked listings for a symbol: one clear leader is the
  token (OpenLedger at rank 673 over the OPEN at 1,963 and the index at
  4,991), close ranks become the question naming each coin and its rank.
  DEX Screener's pool dust is never offered as a candidate.
- **The web is asked in market terms.** `subject_probe.market_scoped` rewrites
  a ticker-like question for a web search; only web tools receive it, data
  tools keyed on a symbol or address get the request verbatim, and the
  answer gate's own web fallback is scoped the same way. A one-letter name
  is asked about rather than searched. `subject_probe.agrees` now holds
  every probe kind to the market-text test, so a company-kind probe no
  longer lets an off-subject web card (Mercury the planet) into the answer.

Test hygiene: `tests/conftest.py` clears the learned tool-outcome counts and
the singleton router's health and overrides per test. Both are module state
that had been reordering ranking assertions between tests.

### The desk is sequential on purpose (2026-09-18)

A widely shared Minara walkthrough runs Market Research, Execution and Risk
in parallel and has the Coordinator block until all three land. Ours keeps
the chain: Market Research writes the thesis, Execution drafts the order
from it, Risk vetoes that draft. The reason is that Risk must judge the
real order, with its size, slippage and route, and a Risk agent running
beside Execution has no order to judge yet. Parallelism is used where the
work is genuinely independent: the multi-tool plan, the four-source market
bundle, wallet balances against positions against DeFi holdings, and the
chart alongside the follow-up questions.

Two changes did come out of the comparison:

- **A trade the charter already refuses costs nothing.** `trading.charter_precheck`
  reads what the request itself says (a dollar amount over `max_trade_usd`,
  a chain outside `allowed_chains`, slippage over `max_slippage_bps`) and
  the desk refuses before writing a thesis or fetching a quote. It only
  ever refuses, never approves: `charter_risk_node` still checks the real
  quote against every rule, and a request that says nothing about amounts
  or chains passes straight through.
- **One voice needs no Coordinator.** An analysis turn with no risk charter
  returns the Market Research thesis directly, saving a model call. With a
  charter set there is a rule to relate the thesis to, so the Coordinator
  still folds.

### A wallet turn must not offer a question its own data cannot answer (2026-09-18)

A Hyperliquid portfolio answer offered "When did the wallet open the ONDO
long position at 10x leverage?" as a related question. No tool can answer
it: `clearinghouseState` and every balances endpoint report current state,
never history. The gate correctly refused, but the refusal read "I couldn't
find The answer lacks the date or time ... for what you asked" and then
told a user who had pasted an address to name a token. Three fixes:

- Related questions drop position and holding history ("when did ... open",
  "how long has ... held"), while keeping current-state questions such as a
  liquidation or entry price.
- The judge's `missing` field is specified as a short noun phrase, and
  `answer_gate._missing_phrase` normalises a sentence into one anyway,
  keeping a name's capital ("Mercury the token").
- A refusal for a question that already names an address says what those
  sources do and do not cover and offers the next step that fits, instead
  of asking for a ticker. The "what I could pull is about ..." clause is
  dropped when it only restates the question.

### Mobula: wallet tracking, portfolio analysis and meme rug risk (2026-09-18)

User decision: use Mobula "explicitly for wallet tracking and portfolio
analysis related queries at high priority order", and "for meme token
complete in depth analysis". `MOBULA_API_KEY` gates every tool below;
without it they are unregistered and nothing changes.

**Wallet (app/mobula_wallet.py), priority 14, ahead of the per-chain tools.**
`mobula_wallet_portfolio` is everything an address holds across 40+ chains
in one call, priced, with each holding's share. `mobula_wallet_history` is
its dated transfers and trades plus how its total value moved: this is what
answers "when did this wallet buy X", which no source of ours could.
`mobula_wallet_analysis` is realized PnL, win-rate and market-cap
distribution, first funding and labels. Airdrop-spam tokens are filtered
from both the holdings table and the activity table and counted in a
footnote, because an unfiltered history for a real wallet is a wall of
claim-page tokens. A token-shaped question ("recent trades of 0x… on base")
is left to the token tools.

**Meme rug risk (app/mobula_security.py).** `mobula_token_security` reports,
per pool, how much LP is burned, locked in a named locker, held by an
unidentified contract, or sitting in a wallet that can pull it, plus holder
concentration at top 10/50/100, buy/sell/transfer fees and the
mint/freeze/pause/renounce switches. It works on Solana and every EVM chain.
It is ranked below the chain-native security tools so "is this token safe"
still reaches Jupiter Shield or GoPlus first, and the multi-tool plan runs
both. The token deep dive calls it by name as a `liquidity_locks` dimension,
so a meme analysis always includes it.

**Coverage checked live, not assumed.** Mobula indexes HyperEVM (chain 999)
but not the Hyperliquid perps clearinghouse, so `goldrush_hyperliquid_positions`
remains the source for perps. Its own perps endpoints cover Lighter and
Gains Network only, on a demo gateway.

Tests run with a network guard: `tests/conftest.py` points the Mobula
callers at a stub, so a configured key never turns the suite live.

### Memecoin forensics from Mobula: holders, trades, deployer (2026-09-18)

User direction: memecoins on Solana, Base and BNB Chain, plus Hyperliquid
perps, are the focus, and Orbit should answer what GMGN answers. Three more
tools in `app/mobula_meme.py`, each verified live before it was written:

- `mobula_token_holders` (`/api/2/token/holder-positions`): each wallet's
  share of supply, USD value, buy and sell counts, unrealized PnL, first
  trade date and Mobula's behaviour labels; a top-10 concentration line;
  and a flagged-wallet table when labels such as sniper, bundler or insider
  appear, stated as evidence rather than proof. First for holder questions.
- `mobula_token_trades` (`/api/2/token/trades`): the latest indexed swaps
  with side, size, price, wallet and venue, and the buy/sell split. The deep
  dive calls it by name as a `latest_trades` dimension.
- `mobula_wallet_deployer` (`/api/2/wallet/deployer`): the other tokens a
  wallet deployed, for a developer's track record, with an honest line when
  Mobula indexed none.

Not built, because the endpoint path could not be confirmed live: the
documented first-buyers endpoint. Holder positions carry `firstTradeAt`,
which covers most of that question when the field is populated.

The routing eval now expects Mobula first for wallet, holder and rug-check
questions; the previous tools remain in each case's accepted set.

### GMGN-class Phase 1: first buyers, launch feed, funding edge, copycat logos (2026-09-18)

The remaining Mobula endpoints the feasibility review named, each found by
reading its reference page (the guessed paths had been wrong) and verified
live before it was written:

- `mobula_token_first_buyers` (`/api/1/token/first-buyers?asset=`): the
  first hundred wallets in, when they first held, whether they still hold,
  whether they added, trimmed or exited, and which are tagged as snipers;
  a retention share capped at what each first bought. A `first_buyers`
  dimension of the token deep dive.
- `mobula_new_launches` (`/api/2/pulse?chainId=`): what is launching on
  Solana, Base, BNB Chain, Ethereum or HyperEVM, in three tables (just
  launched, bonding with the curve percentage, graduated), each row with
  the GMGN columns Pulse carries: dev, sniper, bundler and top-10 holdings.
  A brand-new token with no symbol yet is named from its pair; a row
  nothing identifies is dropped; on a pair minutes old the market cap and
  volume are pool artifacts and are blanked rather than printed.
- Funding edge: the deployer card names where the wallet's first funds came
  from and the known entity behind that address (`/api/2/wallet/funding`).
  This is the first edge of a wallet relationship graph, not the graph.
- Copycat logos: the security card counts other tokens reusing the exact
  logo and names the busiest lookalikes (`/api/2/token/logo-reuses`);
  byte-identical images only.

Not built: `/api/2/token/dev-history` is alpha and returned nothing for the
sample token; the deployer card stands in for it. Everything above is
research and monitoring data; no execution.

### Bundle reconstruction (2026-09-18)

The first piece of the proprietary layer the feasibility review called
for. `mobula_token_bundle` reconstructs a bundle from two independent
signals and reports their overlap:

1. **Same second.** First buyers grouped by the second they first held;
   groups of three or more are listed with how many still hold, the share
   they retain of what they first bought, and how many are tagged. On
   Solana a shared second approximates a shared block.
2. **Shared funder.** The first twenty-five buyers' funding sources
   (`/api/2/wallet/funding`, six concurrent lookups); funders feeding two or
   more early buyers are listed with any known entity tag.

Verdict is deterministic: **Strong** when at least three wallets share both
a funder and a second; **Some** when either a same-second group of five or
a three-wallet funding cluster exists alone; **None found** otherwise. A
funder tagged as an exchange is called out as one that funds strangers.
Every card states that this is evidence from indexed data, not proof of
intent. Live on FARTCOIN's first hundred buyers: groups of ten and eight
entered in the same second. Cost: one first-buyers call plus twenty-five
funding calls, cached five minutes.

### The meme live set (2026-09-18) and the Memes chip

`reports/meme-live-20260918/` runs 22 live cases over the eight most-
followed trader wallets in the user's sheet, FARTCOIN/BONK/WIF, three
pump.fun tokens taken from Pulse at run time, the top Robinhood-Chain
memecoin and a Hyperliquid wallet: 16/4/2 on the first run, 20/2/0 after
five fixes it found (the wallet-portfolio intercept now asks Mobula first
with a twenty-second bound; an address is never an equity question and the
deep-dive lens runs on it; "deploy" reaches the deployer tool; card titles
no longer carry the provider after a dash; Robinhood Chain memecoins are no
longer filtered as stock mirrors, a decoy-pool rule replaces that filter).

The home screen has a **Memes** chip second after Trending. Its rows are
today's two memecoin headlines from the news look-up (the JSON now asks for
a `memes` array; the four market tiles are unchanged and meme cards ride
separately as `meme_cards`) followed by prompts every one of which a live
tool answers: what's bonding on pump.fun, new launches on Solana and on
Robinhood chain, a FARTCOIN deep dive, BONK's LP locks, WIF's holders.

### A bare ticker before a data word is the token (2026-09-18, 22:22 transcript)

"ANSEM top holders on solana" went to the web and came back as a smart-
money aggregate with no wallet in it; the follow-up "largest smart money
wallets holding ANSEM" was asked which token. Both because the resolver and
the whale intercept only read tickers in known phrasings ("holders of X",
"X token") and "ANSEM" stood bare. Now a bare all-caps symbol that is not
market jargon counts as the token whenever the question carries a data
word (holders, price, liquidity, security, unlocks, trades, buyers, bundle,
snipers, deployer ...): `research._bare_symbols` behind `_DATA_ASK`. The
whale intercept asks only when nothing names an asset. The answer judge is
told that "top holders" means a ranked list of wallets and an aggregate is
missing. And "wallets holding <mint>" is a token question, never a
portfolio lookup on the mint, whatever wallet words it contains.

### Cross-conversation memory (2026-09-18)

Built in-house rather than adopting mem0 (`app/user_memory.py`). After a
signed-in research, general or portfolio turn, the primary model is asked
for the few durable facts the turn revealed about the user: holdings,
chains and venues used, preferences, aversions, goals, experience. Each
fact is one third-person sentence; secrets and full addresses are refused
by the parser whatever the model returns; a fact within cosine 0.88 of an
existing one refreshes it instead of duplicating; a user holds at most 200.
Before a turn, the five facts nearest the message are recalled (embedding
similarity, recent facts filling in) into the conversation history as a
"what Orbit knows about this user" block, which explicitly says it is
context and never a trade permission. Trade turns are neither mined nor
gated by it; the risk charter remains the only enforced source of truth.
Extraction runs after the answer is written, off the critical path,
bounded to ten seconds. Storage is the `user_memories` table (JSON
embeddings compared in Python, so pgvector stays optional), cascading on
account deletion, with the in-memory store for tests and no-Postgres runs.
Settings > Data & privacy lists every fact with its kind and date, deletes
one or clears all; `/me/export` includes them; `USER_MEMORY_ENABLED=false`
turns the feature off.

Also today: a ticker on a named chain with several real namesakes resolves
to the clear liquidity winner and the answer opens by saying which one was
read; with no clear winner it asks, listing them.

### Review of 2026-09-20: nine findings, eight closed

- **One Mobula door.** `app/mobula_client.py` is the only place a Mobula
  request is made: the host comes from `MOBULA_BASE_URL`, a process-wide
  token bucket enforces `MOBULA_REQUESTS_PER_MINUTE` across the wallet,
  security, meme and deep-dive callers, at most six requests are in flight,
  every request is counted (`mobula_requests`, `mobula_budget_exceeded`),
  and a caller that would exceed the budget waits up to eight seconds then
  fails with a clear error rather than bursting. A bundle check is still up
  to twenty-six requests, but now inside the same budget as everything else.
- **An EVM contract never defaults to Ethereum.** A bare 0x address with no
  chain named is placed by one free DEX Screener look-up (the chains where
  that exact contract has a real pool); one chain places it, several or
  none ask the user. This applies to every token-data question, not only
  security. `mobula_security._subject` returns no subject for a chainless
  EVM address.
- **Timestamps.** Mobula trade and transfer dates are epoch milliseconds;
  one reader handles milliseconds, seconds and ISO. The fixture now uses
  the real shape.
- **Bundle wording.** Verdicts say "in the sample", state the sample (first
  hundred buyers; funding for the first twenty-five) and what was not
  checked, and no longer call a shared second a shared block.
- **Memory.** Recall returns only facts near the message; the recency fill
  is gone. Users can switch memory off for their account (Settings > Data &
  privacy, `PUT /me/memory`), which stops both collection and recall.
- **Background work is tracked.** `execution_policy.background()` keeps the
  task and shutdown drains outstanding work for up to fifteen seconds.
- **Binance listing status** comes from Binance itself (`binance_spot_listing`,
  public exchangeInfo, no key, cached ten minutes): pairs and status, or
  "not listed on spot", with futures and Alpha named as unchecked.
- **Noise.** Every `setex` is now `set(..., ex=)`; the per-chain wallet
  fallback is built lazily so no coroutine is created un-awaited.

Finding 3, settled by the user (2026-09-20): "Robinhood" in the product
scope means **Robinhood Chain** (Mobula name "Robinhood Chain", chain id
4663), which is what is built. Tokens tradable in the Robinhood app are
not in scope and no adapter is planned for them. "Binance" means the
exchange, answered by `binance_spot_listing`; BNB Chain memecoins are
covered by the on-chain tools like any other chain.

## Typed analyst views, no-data honesty, and decision receipts (2026-09-20)

Phase 0 of the design taken from the ai-hedge-fund v2 discussion. The user
set the order: contracts first, asset-class agnostic, before any equities
data layer, personas, or paper desk. Nothing here trades or sizes yet; it
makes today's answers checkable and gives the later phases one shape to
speak.

**`app/signals.py`** (pure, no I/O)

- `Subject` -- a token is (chain, address), an equity a ticker, a wallet
  (chain, address); `key` is the stable identity across records.
- `Signal` -- one analyst's view: `value` in [-1, +1], `reasoning`,
  `components` (a quant model's decomposition), `metadata`. `Signal.abstain`
  marks a NON-view (no data, model failed, answer unparseable); an abstain is
  excluded from every blend, a real neutral vote dilutes. `Signal.from_stance`
  folds a one-word stance and a confidence word (or 0-100) into a conviction
  and abstains on anything it cannot read, rather than guessing a direction.
- `AlphaModel` -- `predict(subject, as_of, data) -> Signal`, the one interface
  every future analyst implements (quant model or persona).
- `blend_signals`, `apply_limits`, `limits_from_charter` -- the ai-hedge-fund
  arithmetic: weighted mean over voting models, optional market-neutral
  demeaning, per-position cap then proportional gross scale with a
  `ClampEvent` per firing, freed exposure never redistributed. The charter's
  `max_position_pct` is in PERCENT; the converter turns it into a fraction
  and returns None when the user set no cap (advisory, no invented limit).

**No data is not a failure.** `app/provider_router.NoData` (a RuntimeError
subclass, so old `except RuntimeError` sites still work). A handler raises it
when the provider answered and has nothing for this subject. The router
records the call as healthy (no circuit-breaker step, no reliability
penalty), lists `<tool>: no data (<reason>)` in `failures`, does not cache,
and still tries the next candidate. The Mobula holders, trades, first-buyers,
bundle, security and wallet-analysis tools raise it on an empty result. Before
this, ten unindexed tokens in a row opened the circuit on a working provider.

**Decision receipts.** `app/decision_records.py`, table `decision_records`
(per user, cascading on account deletion; anonymous decisions carry no
owner; bounded in-memory fallback without Postgres). One record per decision:
the `Subject`, every `Signal` (votes and abstentions with reasons), the
coverage envelope, every skipped dimension with its reason (`SubjectSkip`),
the price at the time, and the verdict as given. `why(row)` renders it as the
answer to "why did Orbit say that". `GET /me/decisions` lists the signed-in
user's receipts newest first, each with its rendered receipt. The turn's user
is bound through `decision_records.bind_turn` in execution_policy, the same
way the TradingView token is.

**Retrofits.**

- The deep-dive signature (`TokenDeepDive`) now returns `stance` and
  `confidence` as one word each beside the prose; the lens's vote is
  `Signal.from_stance("token_deep_dive", ...)`, abstaining when the words do
  not parse. Synthesis failure abstains with "synthesis failed".
- The bundle check is split into `_bundle_analysis` (numbers), `_render_bundle`
  (the card) and `bundle_signal` (the vote: strong -0.8, some -0.4, none 0.0
  as a real neutral, no first buyers -> abstain), so the card and the vote
  can never disagree. The deep-dive bundle gained a `bundle` dimension (12
  dimensions now) whose vote lands on the receipt.

**Verified live** on :8001 (2026-09-20): "deep dive on BONK" as a signed-in
user produced a receipt with a typed neutral/medium lens vote, a live bundle
check over 100 first buyers (25 funders traced, none clustered) voting
neutral, and four dimensions listed as not seen.

**Not done yet, by decision:** the equities point-in-time data client and
snapshot, persona analysts, the meme holder snapshot ledger, and any blend
or clamp CALLER -- the arithmetic exists and is tested, nothing feeds it a
book. Those are the next phases in the agreed order (contracts, equities PIT
data, personas, meme snapshot ledger, equities paper desk).

## The holder snapshot ledger (2026-09-21)

`app/holder_snapshots.py`. Mobula answers what a meme token's holder
structure is NOW and has no yesterday, so a state-based meme signal ("top-10
under 20% and LP locked") could only be backtested with today's state on
past prices, which is lookahead. The ledger records the structure on a
schedule so history exists from the day recording starts. The user chose to
start it early, before the equities data layer, for exactly that reason.

**A row** (`holder_snapshots`): top-10 / top-50 concentration, the largest
20 positions with Mobula's labels, the share held by dev / sniper / bundler
/ insider labelled wallets, LP burned / locked / unlocked for the main pool,
the contract switches, price, liquidity and market cap, and `flags.missing`
naming every part Mobula returned nothing for. Zeros are never invented: a
concentration figure of 0 on a token minutes old is "not computed" and is
stored as null; when security says nothing, top-10 is summed from the
largest positions with pool and curve wallets left out.

**What is tracked** (`tracked_tokens`): every token a user deep-dived (14
days, extended on each ask) and every new or bonding launch Pulse lists on
`holder_snapshot_pulse_chains` with at least `holder_snapshot_min_holders`
holders (3 days). The Pulse row itself is stored once as the token's first
ledger row at no extra cost (cohort shares, holder count, price, market cap;
no liquidity, since Pulse's pair figure is one-sided and not comparable to
the market endpoint's). A token whose snapshots come back dark twice in a
row (no holders, no market) stops being tracked.

**Cadence**: every `holder_snapshot_fresh_minutes` (10) while under a day
old, then every `holder_snapshot_interval_minutes` (60); at most
`holder_snapshot_max_per_tick` (10) snapshots per 60 s tick, fresh launches
first. Three Mobula calls per row through the budgeted door
(app/mobula_client.py), so the worker takes at most half the bucket and a
user's turn always gets through. The worker starts in the lifespan; off via
`holder_snapshots_enabled`. Tests run against the in-memory store (conftest
patches the pool for this module and for decision_records).

**What it feeds today**: the deep-dive gains a `history` dimension when the
ledger holds two or more rows for the token ("Top-10 share 41% → 23%", "LP
locked 0% → 95%", wallets that left or entered the top 20, switches that
flipped), with a "latest step" line when it differs from the whole span.
`as_of(subject_key, moment)` is the point-in-time read a backtest is allowed
to use; `diff(older, newer)` is what an alert will read.

**Verified live** on the dev database (2026-09-21): the worker discovered
Solana, Base and BNB launches and recorded snapshots within minutes; the
first live history card showed three problems that are now fixed and tested:
a Pulse row re-stored on every discovery pass, Pulse's 0% concentration read
as a real 100% → 0% move, and a missing holder list read as wallets leaving.
Rows recorded before those fixes carry the same flaws; nothing downstream
reads them yet.

**Known limits**: `--reload` in development restarts the worker on every
file save, so overlapping processes can take duplicate rows seconds apart;
label-derived cohort shares depend on Mobula's labelling; holder count is
only known for Pulse-sourced rows.

## Mobula: a sustained rate, a cooldown, and a background lane (2026-09-21)

Found live within hours of the ledger going in: a wallet-portfolio turn
came back from GoldRush with every balance at "$0.00000000" (PENGU and WIF
included). Two faults, one behind the other.

**The ledger starved the user's turn.** The worker drew its full per-minute
allowance in a four-second burst, Mobula answered 429 to everything for a
while, the Mobula-first portfolio path degraded to the per-chain fallback.
`app/mobula_client.py` now enforces three rules:

- The bucket holds at most `mobula_burst` tokens (60, the minute rate, after
  30 refused a deep-dive's own bundle check): the cap exists so the background
  lane, which draws only above half, can never take the whole minute.
- A 429 or 5xx starts a cooldown of `mobula_cooldown_seconds` (20, or the
  Retry-After header). Background callers are refused during it; a user's
  call waits within its usual eight-second bound, then degrades honestly.
- `get(..., background=True)` is a second lane: it never waits, takes a
  token only while the bucket is at least half full, and is refused during
  a cooldown. Every ledger call uses it, `tick` checks `background_ok()`
  before each snapshot and stops for the tick when the answer is no, and a
  snapshot whose calls all failed is neither stored nor counted as taken.
  `holder_snapshot_max_per_tick` is 5 (fifteen calls a minute).
- One worker per deployment: a Redis lease (`holder_snapshots:leader`, 180 s,
  renewed each tick) so two servers on one key, or a reloader's overlapping
  processes, never both record. Without Redis a single process is assumed.

**GoldRush printed unknown as zero.** A balance with no `quote_rate` is now
"unknown (no price)", the header counts priced and unpriced rows, and when
nothing is priced the card says so in words ("value is unknown -- not
zero"). Unpriced rows are capped at eight with a count of the rest.

Rows the ledger recorded during the 429 stretch carry `flags.failed`; the
new rule would not have stored them.

**The wallet itself.** Mobula answers 400 "Unsupported wallet" for this
Solana address (the open item about Solana wallets Mobula rejects), so the
Mobula-first path can never serve it. The order for a Solana wallet is now
Mobula, then Orbit's own read (balances from Solana RPC, prices from
Jupiter in batches of 100, `portfolio_max_priced_holdings` raised to 1,200
because a wallet's largest positions by value are rarely its largest by
token count), then GoldRush. `app/portfolio.render_card` renders the read
priced-first with the unpriced count and the ⚠ mark for unverified tokens;
`research._solana_own_snapshot` is the step, and it returns None when
nothing could be priced so GoldRush still gets its turn. Live: the same
wallet answered in six seconds, 156 holdings priced (USDC, SOL, PENGU
included), 358 with no Jupiter price, total shown as a floor.

## The turn log: every request and response, for the closed beta (2026-09-21)

User decision: "keep track of all requests and response. we need to fix all
issues and keep them logged so we know what happened." `app/turn_log.py`
records one row per chat turn in `chat_turns` (Postgres; bounded in-memory
fallback), wrapped around `execute_chat_turn` so it sees every transport
(JSON, stream, MCP) and every outcome:

- **Answered**: message, answer, intent, capabilities, the tools that ran,
  the public tool activity (bounded at 60 KB, with the tool names kept when
  it is larger), the validator's verdict, the answer gate's verdict when it
  rewrote or replaced the answer (`answer_gate` now travels on AgentState,
  AgentRun and AgentResponse), the risk assessment, the credits charged,
  the plan id, latency, user, account, API key, session and revision.
- **Refused or failed**: the same identity fields plus the HTTP status and
  detail -- rate limited (429), out of credits (402), sign-in required (401),
  timed out (504), bad request (400), crashed (500 with the reference id).

Ratings join in: `GET /admin/turns` shows the user's thumbs-up or -down for
each turn (from `chat_feedback`, keyed by session and revision), and a
thumbs-down flags the turn as an open issue automatically.

**Review surface.** Admin page › Turns: period, status (all / errors /
answered / open issues), user email, free text; each row opens the full
turn with request, error, answer, gate, validation and tool activity, and
Flag / Resolve buttons that take a note. Routes: `GET /admin/turns`,
`GET /admin/turns/summary`, `GET /admin/turns/{id}`,
`POST /admin/turns/{id}/flag` (`{note, resolved}`). The digest:

    .venv/bin/python scripts/turn_review.py --days 1        # problems only
    .venv/bin/python scripts/turn_review.py --days 7 --all  # every turn

writes `reports/turns-<stamp>/review.md` and `turns.json` (untracked).

**Retention and privacy.** Rows are kept indefinitely for now (closed beta);
deleting an account nulls `user_id` on its rows rather than deleting them,
since the log is the operator's record. Answers are stored up to 20 KB and
messages up to 4 KB. Nothing here is on a turn's critical path: a store
failure is logged and the answer still goes out.

## First beta review (2026-09-21): two wrong answers found in the turn log

The first outside user's eight turns, read from the log the same evening.

- **"@frankdegods wallet analysis"** ran the portfolio tools on the user's
  own connected EVM wallet and then said the address "appears to be an
  Ethereum-style address". A handle is not an address and Orbit cannot
  resolve one. `app/handles.py` recognises a handle in a wallet ask (no
  address present, not an email, wallet words nearby); both the portfolio
  and research nodes answer that handles are not resolved yet and ask for
  the address. Separately, an EVM connected wallet with a holdings or
  balance ask is now composed per chain (the pasted-address path) instead
  of being read from Solana RPC; a wallet-health ask on an EVM wallet says
  the check is Solana-only.
- **A 1,170-token Solana wallet showed "$177 priced, SOL unpriced".** The
  real value is $1.21M (BP, USDC). Jupiter's batched registry search was
  called twelve times in a row; one call failing threw away every batch,
  and the ten bounded retries happened to miss the positions that matter.
  `tokens_by_mints` now keeps the batches that answered and logs the one
  that did not; `_price_mints` always retries wrapped SOL first.

Both turns are flagged and resolved in the log with the fix noted. The
review routine that found them: `scripts/turn_review.py --days 1`, or the
admin page › Turns.

## The X sentiment analyst: TwitterAPI.io tweets, Jev's typed judgement (2026-09-21)

Taken from brainstormity/Jev-X-Sentiment-Analysis after review; user
decisions: use TwitterAPI.io as the tweet source, and put the analyst in the
deep-dive, the desk, and the standalone "what is X saying about BONK" card.
Not taken from that repo: its invented funding rate, canned entry/stop/target
levels, fabricated fallback confidence, keyword polarity, and an
unauthenticated endpoint that wrote API keys to disk.

**Tweets** (`app/x_tweets.py`). Advanced search `$SYM lang:en -is:retweet
min_faves:2` (plus the name when it is a real word), newest first, 20 a page,
up to `x_tweets_default_sample` (100). Paging stops at the first tweet already
in the `x_tweets` table, so an intraday re-ask pays only for the new ones;
the store fills the rest from the last `x_tweets_cache_hours`. Stats are
computed in code before any model reads a tweet: sample size, distinct
authors and their share, the busiest author's share, verified share,
engagement, time span, and a stratified sample of the 25 most engaged plus
the 25 latest tweets.

**Judge** (`app/sentiment_analyst.py`). One Jev System One call over
{asset, social_stats, representative_tweets} with four typed questions about
the crowd, never a trade: `stance` (bullish/bearish/neutral with the
probability distribution), `mood` (five levels), `catalyst` (four levels),
`organic` (probability the sample is organic rather than a push). The Signal
is p(bullish) - p(bearish), halved when organic < 0.4. Fewer than 10 tweets,
or a failed call, abstains with the reason. One judgement per symbol per
`social_sentiment_ttl_seconds`.

**Surfaces.** `x_kol_sentiment` renders the analyst's card first and falls
back to LunarCrush, the X API and Perplexity as before. The deep-dive gains
an `x_sentiment` dimension and the vote lands on the receipt. The desk
appends the card under the market data as evidence for the thesis. The
sync router tool reaches the app loop through `sentiment_analyst.set_loop`
(registered in the lifespan), since the tweet store is bound to it.

**Keys.** `TWITTERAPI_IO_KEY` and `TYPESAFE_API_KEY`. Without either the
analyst is off and every surface behaves as before.

**Verified live (2026-09-21, after the key went in).** BONK: 19 tweets over
5.4 hours from two pages, 16 distinct authors, Jev judged in under a second
(about 3,200 input tokens): bullish 100%, mood euphoric, no catalyst,
organic 74%. Two corrections from that run: a page can take over twenty
seconds, so the default sample is 40 tweets with a 12-second per-page
timeout (`x_tweets_default_sample`, `twitterapi_io_timeout_seconds`); and a
19-tweet sample had produced a conviction of 1.0, so the Signal is now
scaled by sample size (full weight at 50 tweets, `FULL_WEIGHT_SAMPLE`).

## A listed coin's own facts: supply, market cap, rank (2026-09-21)

Live: "What is the current total supply and circulating supply of the $ZEC
token?" got a chain question (the Solana wrapper or the BNB one?). Zcash is
a native coin, CoinGecko rank 9; its supply is a fact about the asset, and
every market tool keyed off a contract, so nothing could answer by name.

`app/listed_asset.py` adds `coingecko_coin_snapshot`: CoinGecko's coin page
(rank, price, market cap, FDV, circulating / total / max supply with the
share of max, 24h-7d-30d change, ATH and ATL with dates, categories,
contracts per chain or "native coin", homepage). It matches a listed-asset
question (supply, market cap, FDV, rank, ATH/ATL, halving, tokenomics) that
names a ticker and no address, and answers by the ticker's clear CoinGecko
leader or by a "(CoinGecko id X)" marker.

The resolver puts that marker in: a listed-asset question about a ticker
with a clear leader is rewritten with the coin id and never reaches the
chain question, with a note saying how the ticker was read ("Read ZEC as
Zcash, CoinGecko rank 9"); and a native coin with no contract anywhere
resolves the same way for any question, since no on-chain tool can see it.
A chain question about a multi-chain ticker ("top holders of ZEC") still
asks, as before.

Verified live: ZEC (rank 9, 16.94M of 21M), BONK (rank 151, 88T) and BTC
(rank 1) each answered in about seven seconds from the new tool alone.

## Crowd sources from the last30days review (2026-09-22)

Four additions the user chose after reviewing mvanhorn/last30days-skill;
one turned out impossible.

**StockTwits: not possible.** Its public API sits behind a Cloudflare
browser challenge; every server-side call (browser user agent, curl)
answered the challenge page. Not built. If it matters, it needs a browser
session or a licensed feed.

**Tweet sample: entity grounding and an author cap** (`app/x_tweets.py`).
A tweet counts only if it names the token (cashtag, hashtag, or the coin
name as a word); "gonna bonk my head" is the verb and is dropped however
viral it is. One account contributes at most three tweets (its most
engaged) to the judged sample. Both counts are shown on the card ("Left
out: N that did not name the token, M beyond 3 per account").

**Reddit with real numbers** (`app/reddit_crowd.py`). The keyless JSON and
RSS endpoints returned HTML block pages live, so this uses Reddit's official
API with a free script app: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`
(create at reddit.com/prefs/apps, type "script"), client-credentials grant,
read-only. One search across the crypto subreddits (`reddit_subreddits`)
for the cashtag, the ticker in capitals, or the name, last month; the six
most-discussed threads get their top comments (one call each). Statistics:
threads, distinct authors, subreddits, upvotes, comments, average upvote
ratio, span. Surfaces: "what is reddit saying about X" (`reddit_crowd`
tool), and a second crowd the sentiment judge reads beside the tweets --
the state gains `reddit_stats` and `reddit_threads`, the card a Reddit
line, and the Signal's sample weight counts threads too. Not verified live:
no Reddit app key in the env yet.

**Polymarket odds** (`app/polymarket_odds.py`). Gamma's public search, no
key: open markets whose question names the asset (symbol or name), dust
under $1,000 dropped, sorted by volume, with the Yes price, volume and
resolution date. A `polymarket_odds` tool for "odds / prediction market /
polymarket" asks, a `prediction_markets` deep-dive dimension, and desk
evidence, all silent when nothing matches (memes rarely have markets). Not
verified live: the developer network's resolver returned no address for the
Gamma host and reset the connection when the IP was pinned, which reads as
a regional block; `polymarket_gamma_url` exists so a deployment can point at
a reachable host, and `polymarket_enabled` turns it off.

## Second beta review (2026-09-22): a forecast ask, and promotion read as sentiment

Read from the turn log: 14 turns from outside users so far, three of them
worth a fix.

- **"What will happen for BTC in next 5-10 hours?"** got the gate's close,
  "I couldn't find price prediction... Name the token precisely". Wrong
  twice: the asset was named, and a forecast is not something to look for.
  A forecast ask that names an asset is now rewritten into a tape question
  (price, 24h change, funding, open interest, liquidations, key levels) and
  answered under a line that says nobody's data calls the next few hours.
  The gate's close no longer asks to name a token the question names: for a
  forecast it lists what the tape offers; for a ticker it lists what the
  sources carry.
- **"what is CT saying about ZEC"** read 93% bullish, organic 84%, from a
  sample that was almost entirely NFT whitelist promotion. The tweet
  statistics now carry `promo_share_pct` (whitelist, presale, airdrop,
  giveaway, referral, "mint is live" and the like); the judge's organic
  question is told to use it; above 50% the vote is halved, the reasoning
  says why, and the card carries a warning line.
- **"Is BTC going up in next 5-10 hours?"** answered 402 for an anonymous
  visitor out of trial credits. By design.

## Review of 2026-09-22: ten findings

An outside review of the latest commit, all closed here with a pinned test
each (tests/test_review_20260922.py) except the one that restates a scope
decision.

1. **Holder history froze after 200 rows.** `history()` cut from the oldest
   end. It now takes the newest `limit` rows and returns them oldest first;
   the dark-token check reads current rows again.
2. **Compound clauses shared one sink.** Each clause now writes its own; the
   first clause that resolved a token names the turn's subject, and every
   distinct resolution note is kept in clause order.
3. **The first snapshot blocked a finished deep-dive.** Tracking and the
   first row are scheduled as a tracked background task
   (`holder_snapshots.schedule`), drained at shutdown.
4. **The turn log was on the critical path.** The write runs as a tracked
   background task (`execution_policy.background`), and the store bounds
   its own wait (`STORE_TIMEOUT_SECONDS`, 5 s) before keeping the row in
   memory. The earlier claim in these notes was wrong until now.
5. **Two workers doubled the Mobula allowance.** The bucket is per process;
   `uvicorn_workers` (from `UVICORN_WORKERS`) divides the rate and the burst
   so the container as a whole stays inside the limit.
6. **Robinhood scope.** The review says the app's listings were chosen. The
   recorded decision (2026-09-20, "Yes it's Robinhood chain") is Robinhood
   Chain; the docs stand unless the user says otherwise.
7. **Lowercase Binance questions.** "is bonk listed on binance" now names
   BONK: cashtag, upper-case word, or the word after is / does / list /
   listed / trade in any case.
8. **Polymarket unreachable but reported healthy.** A transport failure
   takes the tool out of routing for ten minutes and `/readyz` carries a
   `polymarket` check (optional, degraded when unreachable) that probes
   once per window.
9. **Export and deletion.** `/me/export` now includes every logged turn, the
   ratings given, tasks and decision receipts. Account deletion scrubs the
   person's message, answer, tool activity, wallet and owner from their
   turn rows; the operational row (status, latency, tools, timing) stays.
10. **Admin summary over 1,000 rows.** With Postgres the summary is SQL
    aggregates over the whole period (percentiles included); the memory
    store counts all its rows.

UI: the closed mobile sidebar backdrop is `aria-hidden` and inert, so it is
no longer announced as "Close navigation" while the menu is closed. Shell
cache bumped to v11.

## Second review of 2026-09-22: six findings

1. **Robinhood app listings.** Unchanged. The recorded decision (2026-09-20,
   "Yes it's Robinhood chain") is Robinhood Chain; the reviewer reads the
   scope differently. Only the user changes this.
2. **Feedback keyed by the shared billing account.** `chat_feedback` gained
   `principal_id` (the person who rated). Ratings are saved with it, the
   export lists a person's own ratings only, and deletion removes them.
3. **Deletion versus in-flight log writes.** Deletion first drains the
   background tasks, then scrubs; a scrub the store cannot do raises and
   stops the deletion with a 503; a tombstone makes any write that still
   lands later store "[deleted]" with no owner; ratings are deleted too.
4. **Mobula across replicas.** When Redis is configured every Mobula call
   also counts against one shared per-minute key (`mobula:rpm:<minute>`);
   a user call is refused past the allowance, a background call past half.
   Redis down means the local bucket alone. `mobula_shared_limit` turns it
   off.
5. **Polymarket probe on every readiness call.** Both outcomes are cached
   ten minutes; the probe's HTTP timeout is 2.5 s, under the wrapper's.
6. **"Everything Orbit holds".** The export is uncapped: every turn, rating,
   receipt and credit entry.

## Third review of 2026-09-22: four gaps in the deletion and quota fixes

1. **Deletion raced across workers.** The tombstone was process-local. A
   `deleted_users` table now holds the marker; `scrub_user` writes it
   FIRST, then scrubs; and the turn-log insert is one statement that reads
   the marker (`INSERT ... SELECT ... LEFT JOIN deleted_users`), so a write
   in flight on any worker lands as "[deleted]" with no owner. Decision
   receipts consult the same marker and are dropped for a deleted account.
   Verified live: a marker for a ghost account, then a record call with
   private words, stored as "[deleted]" with null owner and session.
2. **Flag notes kept the private comment.** A thumbs-down copies the
   comment into `flag_note`; the scrub now clears it.
3. **Existing feedback had no owner.** The schema backfills `principal_id`
   from `user_chat_sessions` (the verified conversation owner) on every
   start, idempotently, and deletion also removes rows by the conversations
   the person owns.
4. **Refused calls spent the shared quota.** The Redis counter is one
   script: read, refuse without counting past the limit, else increment.

## Fourth review of 2026-09-22: the two that remained

1. **A log insert whose snapshot predated the marker could commit after
   the scrub.** The insert for a signed-in user and the scrub now run in
   transactions that both take `pg_advisory_xact_lock(hashtext(user_id))`.
   An insert that started before the deletion either commits before the
   scrub's UPDATE, which then scrubs it, or takes the lock after and reads
   the marker. Anonymous inserts take no lock.
2. **The feedback backfill wrote a bare UUID where the app's principal id
   is "user:<uuid>".** The schema repairs already-migrated bare rows and
   backfills new ones with the prefix. Deletion reads the person's owned
   conversation ids before the mappings are removed and deletes ratings by
   those ids as well as by principal.

## Fifth review of 2026-09-22: fallback copies

A turn logged while Postgres was down stayed in the worker's memory with
its words, and the account scrub returned after the database path without
touching it. One rule now covers every store with a memory fallback (turn
log, decision receipts, feedback, remembered facts), through
`db.memory_is_the_store()`:

- Memory is the store only when no `DATABASE_URL` is configured (tests,
  keyless dev). Then it keeps words, and the scrubs sweep it.
- When Postgres is configured and a write fails, the fallback copy keeps
  no private content and no owner: the turn log keeps the operational row
  with `[not persisted]`, a receipt is kept unowned, a rating is kept
  without its comment or principal, a fact is not kept.
- Every scrub (`turn_log.scrub_user`, `feedback.scrub_principal`,
  `user_memory.clear`, `decision_records.forget_user`) sweeps this
  process's memory as well as the database, and the Redis feedback keys of
  the conversations owned. Account deletion calls all four.

Production refuses to boot without `DATABASE_URL` (deployment audit), so
the multi-worker case is always the second bullet: no worker ever holds a
private fallback copy a deletion cannot reach. The trade is that during an
outage the admin log shows no messages for those turns.

## Staging, and a CI that had never passed (2026-09-22)

**CI.** Every push from 2026-09-17 to 2026-09-22 failed, first at the
editable install (setuptools saw `deploy/`, `docker/`, `reports/` and
refused to guess the package; `pyproject.toml` now names `app`), then on the
tests that describe a configured deployment: which tools register, how
they rank, that admin routes answer 401. The developer's `.env` supplied
those keys; CI had none. `tests/conftest.py` now sets a placeholder for
each provider key that is absent, the network stays blocked by the same
fixtures as before, and the routing gate builds its knowledge anchor from
`tests/fixtures/kb_entities.json` (2,664 entities from the ingested KB,
refreshed with `scripts/kb_fixture.py`) when no database exists. The
throwaway redis of the memory-pressure test runs at 4 MB, because Ubuntu's
redis-server idles near the 1 MB it was capped at. Until this, no image had
ever been published to `ghcr.io/sanjayarun07/orbit`, the image
`docker-compose.prod.yml` pulls.

**Staging is production with a different name.** `ENVIRONMENT=staging` is
audited exactly like `production` (`deployment.STRICT_ENVIRONMENTS`): every
fatal finding above is fatal on staging. `deploy/staging.env.example` is
the beta template with its own hostname, a pinned image tag, swaps off
(`research`), and a $5 rehearsal cap for when they are switched on;
`tests/test_deploy_templates.py` keeps the two templates' keys identical
and both passing the audit. `scripts/preflight.py <env-file>` runs the
audit on the host before `compose up`, names every provider that is and is
not configured, and exits 1 on a fatal finding. The runbook is in
`docs/product/operations.md` under "Staging".

## Live testing report of 2026-09-22: five findings, one caused by the operator

The tester's report is under `reports/live-2026-09-22/` (untracked). What
was done with each finding:

1. **Bundle check said "None found" with every funding lookup failed
   (P1).** `_funding_of` returned the same None for "no funding record" and
   "the call failed"; under Mobula HTTP 429 all 25 lookups failed and the
   card counted them as traced. A failed lookup is now its own outcome:
   with no positive evidence the level is `inconclusive`, the card says how
   many lookups failed and asks for a retry, and the bundle analyst
   **abstains** rather than casting the neutral vote a clean sample earns.
   With positive evidence and some failures the level stands and the
   shared-funder count is stated as a floor.
2. **Wallet totals trusted absurd prices (P1).** vitalik.eth showed a total
   of $136 quadrillion from an unlisted airdrop priced at $4.5 trillion a
   token. A holding Mobula marks unlisted (`asset.id == 0`) is never priced;
   any holding valued past $1 trillion is quarantined as implausible; the
   total becomes the sum of what was priced, labelled "(priced holdings)",
   shares are recomputed, and the quarantined holdings are listed under
   "Not valued" with their balances. A listed token can still carry a price
   its pools could never pay (SDL at $63K each, market cap $2M, "worth"
   $137M in the same wallet), so the largest positions -- those over $1M,
   at most five -- are checked against the token's own market data and
   quarantined when the position exceeds the market cap or ten times the
   liquidity. Live after both: the same wallet reads $1.14M of priced
   holdings with 8,785 quarantined; positions under $1M are not verified.
3. **"Top holders of BONK" answered with liquidity venues (P2).** This ran
   while the local server had no provider or model keys: the operator (the
   assistant) had set `.env` aside for a CI simulation and both local
   servers, which reload on file changes, restarted without it. With Mobula
   registered the same question routes to the holders tool and answers the
   top-ten share. No routing change; the readiness 503 in the report has the
   same cause. The lesson is recorded: never remove `.env` while a reloading
   server runs from the checkout.
4. **Sign-out left the conversation on screen (P2).** `signOut()` now
   abandons any turn in flight and starts a new chat, which clears the
   transcript and the session id; a browser-harness case proves it.
5. **Wrapped SOL called "supply is fixed" (P2).** No mint authority now
   reads "no authority can mint more"; the native wrapper's row says its
   supply follows what SOL is wrapped, 1:1.

Also from the report: the two top-10 concentration figures (security
profile 11.04%, holders table 38.30%) are now labelled with their
definitions. Provider entitlements the report found missing are the
operator's to decide: Bitquery answers 402 (credits), RootData wants a
higher tier, most Nansen tools need `NANSEN-API-KEY` on the MCP side,
Reddit and CoinMarketCap are unset.

## Managed stores, and startup schema under one lock (2026-09-22)

**Shape A.** `docker-compose.managed.yml` runs the API and Caddy on the
host with Postgres and Redis elsewhere (Cloud SQL and Memorystore in
`docs/deploy-gcp.md`). It is used alone, not layered: the base file
demands `POSTGRES_PASSWORD` at parse time for the Postgres it would run,
and Compose interpolates every file before merging, so an override cannot
get past that guard. `tests/test_deploy_templates.py` keeps the managed
file's API service identical to the local stack's in every hardening
setting, so the tested container is the one that runs. Both env templates
carry `DATABASE_URL` and `REDIS_URL` for this shape. What the managed
stores must provide: Postgres 16 with the `vector` extension allowed for
the app's user (the app creates it), and Redis 7 with
`maxmemory-policy=noeviction` and snapshot persistence.

**The deadlock.** The first boot of that shape with two uvicorn workers
against one Postgres logged `DeadlockDetectedError`: both workers ran the
same startup DDL at once. Seven sites ran schema statements on a bare
connection (the base schema, five per-store schemas, the knowledge
schema). All of them now go through `db.apply_schema`, which takes one
deployment-wide advisory lock inside a transaction, or hold the same lock
as a session lock where the sequence tries pgvector and falls back. Forty
concurrent schema runs against a real Postgres, three rounds, produced no
error. `tests/test_schema_lock.py` refuses any store that runs its
`_TABLE_SQL` on a bare connection again.

## Durable jobs, wave 1 (2026-09-22)

`app/jobs.py` implements `docs/durable-jobs-spec.md`: tables `jobs` and
`job_events`; a claim by compare-and-swap on (id, status, lease_id); a
heartbeat that extends the lease and a guard every paid call passes; CAS
checkpoints of plan, state, evidence, signals and the operation cache; the
pause states; a per-replica worker started in the lifespan (`jobs` in
`/readyz`); `attach()` for the chat turn (inline when no worker runs in
the process, which is what the suite and a bare dev server use); delivery
of a detached job's answer to its conversation with one credit charge
keyed by the job id; scrub on account deletion; jobs and events in the
export; `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`.

The deep dive is the first handler (`_deep_dive_job` in
`app/nodes/research.py`): five plan steps, every evidence source through
the operation cache (`build_token_evidence` takes `call` and `step`
hooks), the synthesis cached too, the receipt written at the end and
linked by id. Verified: the acceptance list in `tests/test_jobs.py`
(resume after a crash with zero repeated calls, two claimers, a lost
lease, an expired approval, a deletion mid-run, detached delivery once);
a live deep dive on the test server through the worker in 36 s with all
five steps done and the receipt linked; on real Postgres, a job whose
worker died with an expired lease is claimed again and finished.

Costs: a deep dive that settles while the turn is attached is charged as
before through its trajectory; one that outlives the turn costs the turn
(one credit) plus the deep-dive charge at settle. Next waves: the desk and
the tape; then reminders, price alerts and the brief as scheduled jobs;
then the paper-desk cycles.

## Review of the job engine (2026-09-22): seven findings closed

1. **A cancelled or restarted request stranded its job's answer.** `attach`
   now detaches on cancellation, and delivery treats an attachment older
   than the attach window plus thirty seconds as expired; maintenance
   delivers those too.
2. **Concurrent checkpoints overwrote each other's cache entries.** The
   operation cache is merged key by key and the evidence and signal lists
   appended, as jsonb `||` in Postgres and a merge under the lock in
   memory; six concurrent calls keep six entries.
3. **Delivery was not idempotent.** It is claimed first by compare-and-swap
   on `delivered`, the session write is skipped when a message with the
   job id already exists, and a failed delivery releases the claim for the
   next pass.
4. **Account deletion left job events.** The scrub deletes the events and
   clears the rows' ownership in one transaction; a test pins the order.
5. **A down database silently fell back to memory.** With Postgres
   configured, the store raises `StoreUnavailable` instead; the deep dive
   then runs ephemerally, in memory, and its answer says the run was not
   recorded and cannot resume.
6. **The detached answer did not reach the open page.** The response
   carries the job id; the browser polls the job and refreshes the
   conversation when it settles (a harness case proves it).
7. **A background job lost its owner's TradingView connection.** An owned
   job binds its owner's integration token and receipt owner for the run
   and resets both after; an anonymous or inline run keeps the caller's.

Also in this pass: the evidence envelope (`app/evidence.py`) recorded
beside the holders, bundle and wallet cards and read by the validator's
coverage check, the receipt and the job row; answer requirements
(`evals/answers/cases.json`, `scripts/answer_eval.py`, replayed on pinned
snapshots by `tests/test_answer_eval.py`); and the holders job pilot
(`app/jobs_holders.py`, measured by `scripts/holders_pilot.py`).

## The exit monitor (2026-09-22): can this position get out, and has that changed

The second capability of the narrowed proposition ("understand who
controls the supply, whether your position can exit, and when those
conditions change"). `app/exit_monitor.py` reads the wallet's real balance
of a token from the chain (both token programs), asks Jupiter for an
exact-size sell quote to USDC at 25, 50 and 100 percent, and shows four
numbers never conflated: marked value (quantity times reference price),
quoted proceeds (what the route returns for that size), minimum output
(the transaction's floor at the quoted slippage), and, not here, realised
proceeds. A failed route is a row that says why ("no route", "quote
provider rate-limited"), never a missing row. Partial exits are separate
scenarios and do not add up; the card says so. Nothing prepares or
submits a trade: the quote path is `plans.simulate_swap`, the read-only
one.

**Watching.** `watch my exit on BONK` persists the position with its
entry quotes (`exit_positions`, `exit_quotes`) and schedules a durable
job (`exit_monitor`) that re-reads the balance, quotes, records and
compares every `exit_monitor_interval_minutes` (15). The job engine now
lets a handler reschedule its own row (`next_run_at` in the result), so
one row runs for the life of the position. The card shows the change
since entry and the full-exit quote over time. `exit analysis for X`,
`can I exit my X position`, `how is my exit on X`, `stop watching my
exit on X` and `show my exits` are anchored chat controls
(`app/exit_controls.py`) for signed-in users with a connected or named
Solana wallet; a symbol resolves through Jupiter's registry, one verified
match wins and several ask for the mint. Ten watched positions per user.

**The deterioration alert** (the third capability's first alert): when
the full-exit quote falls by more than `exit_alert_drop_pct` (20%) against
the entry quote or the last alert's quote, an inbox item carries both
quotes, both price impacts, the route and the marked value, and says it is
neither a price alert nor a sell instruction; at most once per
`exit_alert_cooldown_hours` (6). Positions are in the export and cascade
with the user. Verified live against a real BONK holder on the test
server; `tests/test_exit_monitor.py` covers the quotes, the card, the
reschedule, the alert with its cooldown, stopping, and the controls.

## Review of the exit monitor and delivery (2026-09-22): seven findings closed

1. **An attached answer was delivered and charged again** once its
   attachment expired. `attach` now acknowledges the job (delivered = true)
   at the moment it returns the answer; a request that died before that
   point leaves it unacknowledged for maintenance.
2. **A crash after the delivery claim lost the answer.** The claim is an
   expiring `delivering_until`; `delivered` is set only after the
   conversation write and the charge; a lapsed claim is retried.
3. **A failed RPC read was reported as "holds none".** `position_of` raises
   `BalanceUnavailable`; the chat says the balance could not be read and
   that it is not a zero; the monitor keeps the last known quantity and
   records the gap.
4. **A size change read as deterioration.** A position that changed by more
   than 1% since the baseline is re-baselined at the new size, with no alert.
5. **A failed entry quote disabled alerts for good.** The entry is a
   baseline only when its full-exit quote succeeded; otherwise the monitor
   is pending and the first valid quote becomes the baseline.
6. **A registration could fail halfway.** The job is created first and the
   position row linked to it; a failure cancels the job and creates nothing.
7. **A retried alert duplicated the inbox item.** The occurrence key is
   stable per baseline (`user_inbox` has a unique index on task and
   occurrence), so a retry after a failed checkpoint notifies once.

Also: **the no-route alert.** A full exit that was quoted and cannot be
quoted now raises an inbox alert naming the reason and the sizes that
still quote; a provider gap (rate limit, outage, unreadable balance) is
not a market fact and raises nothing.

## Third review of the exit monitor and delivery (2026-09-22): three findings closed

1. **Acknowledgement moved from attach to the chat commit.** `attach`
   returns the settled row without marking it delivered; the research node
   carries the job id with `job_attached`, and the turn acknowledges the job
   only after `commit_turn` has written the answer to the conversation. A
   turn that fails or is cancelled in between leaves the job for maintenance.
   The browser receives a job id only for a detached job.
2. **Registration is all or nothing.** A failure after the position insert
   (the history write, for one) removes the position and cancels the job.
   A stale active row whose monitor is gone is closed and registered afresh
   by the next watch, never reported as "already watching".
3. **No-route only when the provider said so.** Timeouts, dropped
   connections, auth errors and unknown failures are "quote unavailable"
   rows, never the no-route alert; only an explicit no-route answer raises it.

## Fourth review of delivery (2026-09-22): three findings closed

1. **The committed message carries the job id.** The turn writes `job_id`
   into the assistant message's metadata before acknowledging; delivery
   treats a conversation that already holds a message with the job id as
   delivered and charged by that turn, so it appends nothing, charges
   nothing, and only sets the acknowledgement that was missed. A failed
   acknowledgement after a successful commit never fails the turn.
2. **A failed job returned through chat is acknowledged the same way**: the
   terminal-error branch carries the job id and the attached flag.
3. **No-route is read from the provider's body.** An HTTP 400 whose JSON
   says `COULD_NOT_FIND_ANY_ROUTE` is a no-route; a 502 with a text body is
   an outage.

## Checkpoint fingerprints (2026-09-22)

From TradingAgents' checkpoint identity, adapted. A job row carries a
fingerprint of what its checkpoints were built under: kind, spec, the
handler's declared version and the settings the handler names at
registration (`jobs.register(kind, handler, version=..., settings_keys=...)`).
At every claim the engine recomputes it; a mismatch (a deploy that bumped
the handler version, a changed setting) drops the plan, state, operation
cache, evidence and signals, records a `reset` event, and the plan
restarts, so evidence gathered under one set of assumptions is never
mixed with another. The deep dive names the model, the synthesis model,
the Mobula host, the volatile-cache age and the sentiment and Polymarket
switches; the exit monitor its interval, threshold and cooldown; the
holders pilot the Mobula host. Bump a handler's version whenever its
evidence shape changes.

Also this pass: a background delivery retry charges even when its message
already exists (`delivered_by = job` on the messages it writes); only a
message the attached turn committed skips the charge.

## The structure benchmark (2026-09-22): the first numbers

TradingAgents' evaluation runner, adapted to our own data. `scripts/
structure_benchmark.py label` reads the holder ledger: each well-recorded
subject becomes a case whose evidence is its FIRST snapshot and whose
outcome is decided by the snapshots in the next 24 hours -- concentration
up ten points, liquidity down to under 30%, price down to under 30% --
each label present only when both ends recorded the field. Three arms
run on the same saved evidence: deterministic rules (`app/structure_rules.py`),
one grounded analyst, and the analyst plus an independent critic. The
runner scores recall on bad outcomes, false alerts on clean ones, unknown
fields, latency and model calls, and names the misses.

First run, 279 cases (34 bad, 237 clean, 8 unknown) and a stratified 20:

| Arm | Recall on bad | False alerts on clean | Model calls |
|---|---|---|---|
| rules, all 279 | 9% | 5% | 0 |
| rules, 20 | 10% | 10% | 0 |
| analyst, 20 | 100% | 100% | 20 |
| analyst + critic, 20 | 80% | 90% | 40 |

The reading is plain: on a decision-time structure snapshot alone, the
analyst flags everything and the rules flag almost nothing; neither
discriminates, and the critic buys a little precision at double the cost.
So a structural verdict from the first snapshot is not a risk assessment
and must not be presented as one. What the product needs before an
analyst can be trusted here is the evidence the proposition names: the
launch's transactions and relationships, and the position's exit quote.
The benchmark is the gate those additions have to pass. Re-label as the
ledger grows; the runs under `evals/structure/runs/` are the record.

## Evidence anchors (2026-09-23)

An envelope's observations now carry anchors: where each can be verified
without trusting us. An anchor is a transaction signature, a slot, or a
provider record, with the provider's own time for the fact (`at`), kept
apart from our collection time (`as_of`), and the envelope names the
version of the code that read the provider's answer (`interpreter`).
Built from fields the providers already return: Mobula's funding lookups
carry a transaction hash and date, its trades a transaction hash, its
holder positions a first and last trade time; every Jupiter quote carries
`contextSlot`.

Where they land: the bundle check anchors every shared-funder claim to
the funding transaction (the card's "Shared funding sources" table links
them on Solana) and every same-second claim to the first-holding time;
the holders envelope anchors its top rows to the provider's records; the
trades tool now records an envelope with one transaction anchor per
trade; the exit monitor's quote rows carry the slot and the card says
which slot the numbers came from; the deep dive's coverage rows count the
anchors per dimension. Anchors are capped at 60 per envelope and travel
in the public form, on the job row and beside the answer. A claim without
an anchor is still a claim, marked as unanchored by `Evidence.anchored()`,
never silently dropped. This is what the launch dossier will show as
"the transactions supporting each inference".

## Position-aware workspace, first slice (2026-09-23)

**What changed since entry, said apart.** The exit monitor's deterioration alert now opens with a decomposition
(`exit_monitor.explain_change` / `explain_sentence`): the reference price's move, the quote's discount to that
reference then and now (discount = 1 − quoted ⁄ marked), the route then and now, and missing data as its own
state. `what changed since I entered X` renders the same reading on demand, with the holding change, or says
plainly that there is no entry snapshot when X is not watched.

**Rules per position.** `exit_positions.rules` (jsonb): `drop_pct` (proceeds fall vs baseline; default
`EXIT_ALERT_DROP_PCT`), `discount_pct` (absolute discount to the reference; fires on its own), `channel`
(`inapp` | `email`). Set by chat: `tell me when the discount on my full-position exit quote exceeds 5%`,
`email me when my BONK exit drops 10%`, `exit alerts to inbox`; `show my exits` lists them. An email goes out
only when the inbox row was new (the inbox is the delivery record) through `emailer.send_email`.

**Sizing before entry.** `compare buying $500, $2,000 and $5,000 of X` (chain and trailing sentences
tolerated; works for guests) quotes USDC→X and the immediate reverse exit of exactly what that entry receives,
with slots; labelled a liquidity diagnostic, never a forecast. Jupiter quotes are paced (0.5 s) and retried
with backoff (1.5 s, 3 s) on 429: six in a row hit the public limit live.

## UI flow report fixes (2026-09-23)

- **Instruction words are not assets.** `subject_probe.subject_of` skips a capitalised sentence starter
  followed by a determiner, pronoun or "token" (Save this…, Separate token…, Show my…); the context capsule
  binds "X token" only when X is a $ticker, all-caps, or a capitalised non-starter.
- **`app` speech act.** Save/export/watchlist/alert-settings requests classify as `app` (docstring,
  Jev options, examples v4, three routing cases) and route to `product_actions.answer`, which says what exists
  (Recents, account export, exit monitors, Settings › Tasks) and what does not (report export, ranked
  watchlist). 118/118 intent accuracy in resolve mode.
- **A declared token is never a deployer wallet.** `mobula_meme.declared_token` (token/mint/contract/CA
  before the address, or "launch of" without "wallet"); `wallet_deployer` resolves the deployer from Mobula
  `/metadata` (`deployer`) and says so when there is none. Compound clauses without a subject of their own
  carry the message's address (`composition.carry_subject`).
- **One synthesis per answer.** `composition.combine` strips each part's earlier "Taken together"; the web
  branch strips the compound one. Clause deltas are muted (`streaming.muted("delta")`) so concurrent clauses
  no longer interleave sentences in the transcript.
- **Dated comparisons.** `snapshot_compare.dated_ask` recognises "between/since <date>" asks; the answer opens
  with the ledger's two-row comparison (`holder_snapshots.as_of` + `diff`) or the limitation ("No saved
  snapshot of X for <date>… everything below is current data").

**Review of c8bf7764 (2026-09-23).** `$BONK` is carried whole into subject-less clauses; the sizing pattern uses
named token groups so a mint with digits is the token; `what changed since I entered X` prefers the connected
wallet's watch (else every watch, each on its own), reads the holding against the baseline's quantity, and says
when the wallet no longer holds the token; a rejected alert email leaves `last_alert.email_pending` set and is
retried every tick until the provider accepts it (the inbox row and the cooldown stand meanwhile).

## Complete UI review fixes (2026-09-23)

- **Portfolio reads both token programs.** `solana_rpc.get_all_token_accounts` returns legacy and Token-2022
  accounts plus the programs whose read failed; `build_portfolio_snapshot` marks `partial` and `unread_programs`,
  and the portfolio node says "Snapshot partial … not an empty wallet" instead of "no SPL token holdings". The
  tester's wallet (ANSEM under Token-2022) now agrees between the portfolio and the exit analysis; SOL exposure
  uses the full denominator.
- **Units survive synthesis.** The desk thesis and the composite synthesis carry explicit rules: dollar
  liquidity/volume/market cap are never a support or invalidation price; a level needs a price figure in the
  data; a catalyst needs a dated event; unverified identity caps conviction at 4/10; "24h price change" is a
  price move, never volume growth. The GeckoTerminal and DEX Screener columns now say "24h price change".
- **Small prices and the priced token.** `_money` prints four significant figures below $1 ($0.000006300, never
  $0.0000); pair tables carry a "Priced token" column and a note that a row where the requested token is the
  second name prices the other token.
- **Holder table semantics.** "First token trade" and "Wallet funded" are separate columns; absent buy/sell
  counts print "—" (unknown, not zero); a note explains Mobula's PnL basis.
- **Price alerts are validated at the boundary.** `tasks.validate_spec` requires a symbol, `<`/`>` and a finite
  positive price on create and on spec updates; the form's "-1" now returns 400.

## Funded UI review fixes (2026-09-23, 48 persona cases)

- **Follow-ups keep the subject.** `subject_probe.subject_of` treats a capitalised sentence opener as grammar when it
  is an English imperative, auxiliary or pronoun (Build, Mark, Handle, You, Were…) or is followed by a determiner or
  "token"; CLMM/DLMM/AMM/PDA and other jargon are never symbols. A message that names nothing of its own and talks
  about the analysis (`subject_probe.continues_subject`) is bound to the session focus: a token ("Resolved from
  canonical session context: token X mint … on chain") or a topic ("this continues the discussion about
  EigenLayer"); `advance_session_context` keeps the focus on such follow-ups, records a named topic as the focus,
  and clears it only for a message with its own subject or a market-wide ask (trending, gainers, the market today).
- **One-letter tickers need asset intent.** "I am new to crypto" no longer asks which token I is.
- **Product questions.** "What can I do here without connecting a wallet" is the `app` act and gets the plain
  capability answer (`product_actions.WHAT_CAN`).
- **Conditional orders.** "If SOL drops below $100, automatically buy 2 SOL" is answered before any rule with
  `speech.CONDITIONAL_ORDER_ANSWER` (no automatic orders exist; the price alert and exit watch do). Questions
  ("should I buy if it dips?") stay advice.
- **Token vs wallet roles.** A Solana address with token words (token, mint, first/early buyers, bundle, snipe,
  holders, deployer, launch) is never read as a wallet's history: the Helius matcher declines and the wallet
  detector's `TOKEN_SHAPED` vocabulary now includes buyers.
- **Exit controls.** Tickers are stripped of sentence punctuation ("ANSEM."); the exit ask accepts "in the
  connected wallet" and trailing sentences; the threshold reply explains the trigger (cadence, comparison,
  cooldown, never sells).
- **Constraints before ranking.** DeFiLlama yields honour "without another volatile token / single-asset / only
  USDC" (pool `exposure == single`); protocol TVL matches the named protocol only; "BNB Smart Chain" scopes
  discovery to bsc.
- **Evidence discipline in prompts.** ResearchAnswer and the deep-dive rules: correct a contradicted premise
  first; no launch date without a dating source; a token program implies no extensions; "secure", "safe",
  "unanimous" need the evidence and its sample; honour stated constraints or say no match. Wallet health states
  concentration as a fact and says liquidity is not measured.
- **Polish.** Plan copy says 300 credits; `show my tasks` cites a real task number; "Name it X" and "in-app inbox
  only / by email" become the reminder's title and channel.

## Tequity: the company's equities and perps feed (2026-09-23)

`app/tequity.py` subscribes once per process to `TEQUITY_WS_URL` (default `wss://tequity-dn.i5.xyz/ws`; plain
`ws://` is rejected upstream) and keeps the latest snapshot of every channel: `equities:movers:aster` (500 pairs,
every ~3 s), `equities:movers:hyperliquid` (358 pairs, ~5 s), `equities:trending` (50 pairs, ~4 s), plus
`equities:news` and `equities:stats*`, which had emitted nothing in the first 150 s of listening. A tool reads the
snapshot (stale after 120 s) or takes one from an 8-second one-shot subscription. Tools: `tequity_movers`
(venue-scoped gainers/losers, tokenized stocks filtered on `is_stock`; also a research-node intercept so a venue
movers ask never drifts to the web), `tequity_trending` (cross-venue, composes with the narrative card),
`tequity_news` (reachable only once a news snapshot exists). Cards carry the server timestamp as the snapshot
time and say that "24h price change" is a price move. The feed has no book, funding, open interest or per-row
time, so it feeds discovery cards, never the deep dive or the exit monitor. Disable with `TEQUITY_ENABLED=false`.
The worker is listed under `tequity` in the startup task map.

## Orbit on Telegram: the bot as a third transport (2026-09-22)

**A transport, not a second product.** `app/telegram/` is an adapter over
`execution_policy.execute_chat_turn` -- the same call `POST /chat` and the
MCP server make, with the same admission, per-session turn lease, call and
cost budgets, answer validation, credits, risk charter and persistence. The
package decides only what a transport is allowed to decide: who is calling,
which conversation this is, whether a group message is addressed to us, and
how an `AgentResponse` looks in a chat. Nothing in the graph, the provider
router, the nodes or the execution gates changed.

| File | What it owns |
|---|---|
| `client.py` | The Bot API methods this needs; never raises, logs and returns None (the `emailer.py` contract) |
| `identity.py` | A Telegram user as a way in to an ordinary Orbit account; link tokens in both directions |
| `bot.py` | Update dispatch, commands, the group rule, the turn |
| `render.py` | Markdown → Telegram HTML, message splitting, inline keyboards |
| `progress.py` | The `streaming.py` status events as one message that keeps being edited |
| `webhook.py` | The HTTP edge and the account-link endpoints |

**Identity: an account, not an anonymous session.** A Telegram user's first
message creates an email-less account (`accounts.create_account`, extracted
from `create_wallet_user` so the two ways in cannot drift) keyed on their
numeric Telegram id in a new `user_identities` table. The turn then runs as
`Identity(kind="user", ...)`, so plans, credits, quotas, 30-day conversation
retention, export and deletion are the paths the web already uses. The
anonymous path was deliberately not reused: it is keyed on an IP plus a device
header and retains a conversation for two hours, which is right for a browser
tab and wrong for a chat thread the user scrolls back through next week.
"Anonymous" on Telegram means no sign-in screen, not no account.

Linking an existing web account goes the other way: `POST /me/telegram/link`
(browser session only -- it hands out a credential that binds a messaging
account) mints a one-time token for `t.me/<bot>?start=<token>`, and `/start`
repoints the identity. The shell account the bot made on first contact is then
deleted, but only when it is *only* a shell -- no email, no wallet, no other
identity, no Stripe customer. Deliberately not conditioned on the credit
balance: a shell always holds the free monthly allowance, so that test would
be true of every shell and the row would be orphaned forever.

**The conversation is derived, not stored.** `tg:<chat_id>`, or
`tg:<chat_id>:<thread_id>` in a forum topic. The same chat always resumes the
same Orbit conversation across restarts, which is what makes follow-ups,
pronouns and the focus entity work without the bot keeping state of its own.

**A button names a position; the server supplies the action.** Telegram
callback data is client-supplied and capped at 64 bytes, so a quick-action
button carries `qa:<index>` and nothing else. The action itself is read back
from the assistant message the server persisted it on, and
`execute_chat_turn` still checks the reconstructed action byte for byte
against that list before routing it. A forged callback can therefore only
name a position in a list the server wrote.

**The group rule.** In a group or supergroup the bot answers only a command, a
direct `@mention`, or a reply to one of its own messages. A bot that replies
to every message in a shared channel is spam, and every reply is a charged
turn against someone's account.

**Progress, because sixty seconds of silence looks broken.** Telegram has no
stream, so the bot posts one placeholder and edits it from the same
`streaming.attach` hook the SSE route uses: the status lines are already
sentences a person can read ("Running mobula token details", "Writing the
due-diligence verdict"). Edits are debounced to one per 1.5 s and stale lines
are dropped rather than replayed. `card` and `delta` events are ignored --
replacing the progress message with half an answer would leave the user
reading a sentence about to be overwritten. A turn that outlives
`TELEGRAM_TURN_TIMEOUT_SECONDS` is **not** cancelled: it was admitted and
charged, it persists to the conversation on its own, and the user is told so.

**Rendering is lossy on purpose.** Telegram HTML has no headings, lists or
tables, so a heading becomes bold, a bullet becomes "•", and a markdown table
becomes an aligned monospace block -- a pipe table rendered as prose on a
phone is unreadable. Splitting happens on the *markdown*, before conversion,
so a message boundary can never fall inside a tag (the failure that makes
Telegram reject the whole message and show the user nothing), and an
over-long code fence is re-emitted as several complete fences rather than cut
in half. Every answer carries a one-line footer naming its providers, the
as-of time and the validator's `warn`, because an answer whose provenance is
invisible reads like every other bot in the group.

**Nothing here can sign.** A quote renders as a review card that says
"Nothing is signed yet" and a `web_app` button opening the conversation in the
Orbit UI, where the user's own wallet confirms it -- the same hand-off the MCP
server performs. There is no callback data anywhere in the package that could
confirm a plan, and `/trade-plans/*` keeps its `require_browser_session`
dependency untouched. `/wallet` accepts a public address only and answers
anything seed-phrase-shaped with an explicit compromise warning.

**The webhook is guarded twice.** An unguessable path segment and the
`X-Telegram-Bot-Api-Secret-Token` header, both compared with
`hmac.compare_digest`; a wrong path is a 404 so the secret cannot be probed.
Unset, the secret is derived from the bot token rather than left blank, so a
deployment that forgets it fails closed instead of open (the startup audit
raises `telegram-webhook-secret-missing` as a warning). The route validates,
hands the update to a tracked background task and returns 200 at once, because
Telegram redelivers until it gets one and a deep dive takes longer than it
will wait. Update ids are remembered so a redelivery is not charged twice.

**Configuration.** `TELEGRAM_BOT_TOKEN` empty is the default and means the
whole package is inert: the router is registered but every handler refuses,
no webhook is claimed, and nothing else in the app changes. The webhook is
claimed at startup against `PUBLIC_BASE_URL` (skipped, with a warning, when
that is localhost). All four routes are declared in
`tests/test_authorization_matrix.py`; 24 tests cover identity, linking,
ownership, the group rule, callback authorization, rendering and refusal.

**What is not here.** Signing inside Telegram (the Mini App: `initData` HMAC →
browser session, Privy-first because extension wallets do not exist in the
webview), a `telegram` delivery channel for `app/tasks.py`, and the watch/diff
features the holder-snapshot ledger makes possible. Those are the next wave.

### Push: the `telegram` task channel (2026-09-23)

`app/tasks.py` delivered to the inbox and, optionally, email. Email reaches an
inbox nobody opens; the reason to put Orbit in Telegram is that it can speak
first. `CHANNELS` is now `("inapp", "email", "telegram")` and a fired task
pushes to the owner's linked chat.

**The contract is email's, exactly.** The inbox row remains the delivery of
record and the idempotency key (`_occurrence_delivered`); the push is
best-effort on top of it; a failed push is recorded in `last_result` and
**never retried**, because a retry cannot tell a lost message from a late one
and a duplicate price alert at 3am is the worse outcome. An account with no
linked chat is a recorded failure, not a crash.

**Which channel a new task gets.** `task_scheduling` holds a ContextVar the
transport sets for the duration of a turn, and `tasks_nl` reads it wherever a
channel used to be hard-coded. A browser turn leaves it unset and tasks land
in the inbox, which is what the user is looking at; a Telegram DM sets
`telegram`, because "alert me when SOL drops" has to arrive where it was
asked. A ContextVar rather than a `ChatRequest` field on purpose: the
transport knows this and the request body does not, so no client can choose a
delivery channel by setting a flag.

**A task created in a group keeps the inbox default** (`bot._is_private`). The
task belongs to one account, and its holdings, thresholds and reminders are
not the group's business. Delivery likewise addresses the DM: a Telegram
private chat's id is the user's id, so the `user_identities.external_id` row
is the chat.

`/tasks` lists them from inside Telegram, for a user who never opens the web
app. Six tests cover the channel default, the group exception, the push, the
unlinked account and the accepted channel value.

### A group is shared; a conversation is not (2026-09-23)

Found by asking how two people use one bot. `session_id` was
`tg:<chat_id>`, so everyone in a group shared one Orbit conversation. A
conversation is owned by one account (`session_access.require_session_access`),
so the first person to ask owned it and **every other member was refused
forever** -- reproduced: two users, one supergroup, the second got "This
conversation belongs to another account."

Had they not been refused it would have been worse than a lockout. The
session context carries the bound wallet, the focused entity and the risk
charter, so a second member's follow-up would have run against the first
member's wallet, and "sell half of it" would have resolved to someone else's
position.

A group conversation is now per person: `tg:<chat>:u<user>`, and
`tg:<chat>:<topic>:u<user>` in a forum. A private chat is unchanged
(`tg:<chat>`, where the chat id is the user id). Three tests cover two members
of one group getting separate conversations and accounts, two private chats
sharing nothing, and one member's bound wallet not reaching another's context.

### Linking, from the side the user is actually on (2026-09-23)

The first build had linking run web → Telegram: sign in on the web, find
**Profile → Connect Telegram**, tap the `t.me` link. That is backwards for the
common case. The person who needs linking is the one who met Orbit *in*
Telegram and has no web account yet, and asking them to find a button on a
surface they have never used is where they instead quietly acquire a second
account.

`/link` now mints a token that carries the **Telegram identity** and hands
back `/ui/?tglink=<token>`. The browser stashes it, the user signs in with
whatever they like — a wallet, an email — and the claim fires by itself on the
next `/me`. They never see a Connect button. The web → Telegram token stays
for the web-first user; `consume_link_token` returns a typed payload and each
side accepts only its own `kind`, so neither token is redeemable as the other.

Two proofs meet at `POST /auth/telegram/claim` and neither links anything
alone: the token proves the holder began this from a particular Telegram
account, the browser session proves which Orbit account they just signed into.
A stolen token can therefore only attach a Telegram identity to an account its
holder could already sign into. Single use, 15 minutes, and the bot says the
link is personal and must not be forwarded.

**Connecting a wallet is not signing in with one, and that was the real
blocker.** Live test: the user connected Phantom, saw the wallet chip fill in,
and stayed on "Trial · 0 credits" — `/account` in Telegram still said *not
connected*. `signInWithWallet` runs once, fire-and-forget, from each connect
path, *after* `closeDialog`; a declined or unsupported signature left the user
connected, not signed in, with the reason written to `console.warn` and to a
panel that had just closed. And because sign-in only ever ran on the connect
path, there was **no way to try again** — with email sign-in also unavailable
(a Resend key with no verified domain), the account was unreachable.

The wallet panel now carries **Sign in with this wallet** whenever a wallet is
connected and the account is not authenticated, and it states what happened:
a declined signature and a provider that cannot sign a message are different
problems and the user was previously told neither. The retry closure is
captured inside `signInWithWallet` itself, so every provider path gets it
without each one remembering to.

The intermediate fix — a `link-bar` under the topbar — was removed at the
user's direction: the web UI already has sign-in, and a second place to do it
is a second thing to maintain and explain. The pending link is still held in
`sessionStorage` and claimed automatically on the next `/me` after any
sign-in, so the flow completes without a card; the confirmation lands in the
wallet panel and in Settings → Connections.

**`/account`** answers the question that follows any link — did it work? — from
inside Telegram: plan, credit balance, email or wallets on the account, the
wallet this chat is following, whether a risk charter is set, and, in one line,
whether this chat is connected to a web account or still has one of its own.
The web answers this with a Settings screen; a Telegram-only user has none.

### Three reasons the automatic connection did nothing (2026-09-23)

Live testing kept ending the same way: wallet connected, chat still on
"Trial · 0 credits", `/account` in Telegram still *not connected*. The server
was never at fault — a real Ed25519 signature against
`/auth/wallet/challenge` + `/auth/wallet/verify` produces an authenticated
account in one round trip. Three separate client-side faults hid that.

**1. The pending link was stashed per tab.** Telegram opens a link in a *new*
browser tab, and people sign in in the tab where they already had Orbit open.
`sessionStorage` meant the tab holding the token was never the tab that signed
in, so the claim had nothing to fire on. It is `localStorage` now, with its
own 15-minute expiry on top of the server's single-use token, so any tab on
the origin completes the link.

**2. A failed wallet sign-in was invisible.** `signInWithWallet` runs
fire-and-forget from each connect path, *after* `closeDialog`, and its catch
wrote to `console.warn` and to a panel that had just closed. The wallet chip
filled in, the account stayed anonymous, and nothing said why. The reason is
now kept and shown in the wallet panel, and both outcomes are logged server
side (`wallet sign-in attempt/succeeded/failed`, address truncated) so an
operator can see a failure without the user opening devtools.

**3. A pasted address was offered a sign-in it could never do.** A read-only
public address has no key in the browser. The panel now says so instead of
showing a button that cannot work.

Also fixed while in here: the sign-in message's `URI:` line was built from
`request.url.scheme`, which is `http` behind any TLS-terminating proxy
(ngrok, Caddy) while the browser is on `https`. That line is shown to the user
by their wallet and is compared against the origin by strict SIWE/SIWS
validators. It now honours `X-Forwarded-Proto`, read from the header directly
so it does not depend on uvicorn running with `--proxy-headers`.

### The signature that was never asked for (2026-09-23)

The server log named it: `POST /auth/wallet/challenge 200` and then nothing —
`/auth/wallet/verify` was never reached, so `signMessage` never returned.
Two faults, one behind the other.

**The prompt could not be raised.** Each connect path did
`setWalletConnected` → `closeDialog` → `signInWithWallet(...)`, unawaited. A
wallet extension will not open its signature window once the user activation
from the click has been spent, so the request sat pending for the life of the
page with nothing awaiting it. `connectAndSignIn` now keeps the dialog open,
shows "check your wallet to finish signing in…", and awaits the signature
inside the click; the dialog closes only on success, and a failure leaves the
reason and a retry on screen. A 120-second race turns a prompt that never
appears into a sentence instead of an indefinite wait.

**And the feature name was wrong.** `solanaStandardSignMessage` read
`wallet.features["standard:signMessage"]`, which is `undefined` on MetaMask's
Solana wallet: Wallet Standard namespaces signing by chain
(`solana:signMessage`), and only connect, disconnect and events live under
`standard:`. The resulting TypeError — "Cannot read properties of undefined
(reading 'signMessage')" — was swallowed by the same catch. Both names are now
tried, and a wallet offering neither says which features it does have rather
than failing anonymously.

Two smaller ones from the same session: the not-signed-in status appended to
itself on every attempt ("· not signed in · not signed in") and now replaces;
and a `/link` opened from Telegram lands in a NEW browser tab, so the pending
token moved from `sessionStorage` to `localStorage` — the tab holding it was
never the tab the user signed in from.

### No Mini App, and no button on every answer (2026-09-23)

A `web_app` button opens Telegram's in-app webview, which has **its own cookie
jar**: a user signed into Orbit in their browser arrives there signed out, on
the anonymous trial, with the account they just linked apparently missing.
Worse for the one job a Mini App would exist to do — browser-extension wallets
(MetaMask, Phantom) **do not exist in that webview at all**, so the wallet
sign-in that now works would not work there.

Every such button is now a plain `url`, which opens the real browser where the
session and the wallet already are. Nothing to synchronise, because no second
context is created.

**The Mini App is deferred, not rejected.** Its only unique capability is
signing a trade without leaving Telegram, and that is worth building when
trading is actually switched on (`deployment mode=research`,
`LIVE_TRADING=false`, STAB-09 open) and mobile-first users need it. Until
then it would be a second UI, a second session system (initData -> cookie) and
a second wallet stack (Privy-first, since extensions are unavailable) serving
a flow nobody can use.

**And "Open Orbit" is gone from ordinary answers.** It was appended to every
reply. A research answer is finished in the chat; a button pointing away from
it on every message is noise. It remains where the user asks for the web
explicitly -- `/app` and `/account` -- and on a quote, where signing genuinely
has to happen in a browser.

The shape this settles on is two surfaces, one account: Telegram to ask,
monitor and be pushed to; the browser to sign in, sign trades and change
settings.

### Review of the Telegram changes (2026-09-23)

A pass over everything added for the bot, against the code as it now stands.

**A slow turn was being cancelled after it had been paid for.** `_run_turn`
waited with `asyncio.wait_for`, which cancels the coroutine it is waiting on.
Admission, the credit charge and the turn lease all happen before the first
provider call, so a deep dive that ran past `TELEGRAM_TURN_TIMEOUT_SECONDS`
lost its paid work and left the conversation with no answer — the opposite of
what the comment beside it claimed, and the opposite of what the SSE route
does when a phone locks its screen. The turn is now a tracked background task
waited on with `asyncio.wait`: past the timeout the user is told it is still
running, the task finishes on its own, and `_deliver_when_ready` sends the
answer as a new message. This is the durable-jobs "attach for N seconds, then
append" pattern (`docs/durable-jobs-spec.md`) in miniature, and the shape the
engine will formalise.

**Dead surface removed.** `client.py` carried `send_photo`, `delete_message`
and `delete_webhook`, none of them called by anything —  `send_photo` in
particular existed for charts that cannot be sent, since `ChartCard` is a
TradingView widget specification rather than an image. An untested
convenience is a liability, not a head start. `webhook.py`'s unused
`WEBHOOK_PATH` went with them.

**Docstrings that outlived their design.** `bot.app_url` still described a
"Mini App hand-off" and `render.keyboard` did not say that an ordinary answer
now carries no link out. Both corrected, with the reason (`web_app` means a
separate cookie jar and no extension wallets) recorded where the decision is
made rather than only here.

**README brought in line**: `/link` from the Telegram side, `/account`,
`/tasks`, the per-person group conversations, the `telegram` task channel, and
the explicit note that links are never `web_app` buttons.

Verified after the pass: 1,774 tests pass, 47 of them covering the bot;
`scripts/check_secrets.py` clean over 441 files; the inline UI JavaScript
parses.
**Tick ledger (`app/tequity_ledger.py`, 2026-09-23).** The lease holder (Redis key `tequity_ledger:leader`)
records one row per pair per tick into `tequity_ticks` every `TEQUITY_RECORD_INTERVAL_SECONDS` (300; 0 disables),
for the two movers channels and the trending list (rank kept), stamped with one `taken_at` per tick and the feed's
server time. About 250k rows a day at 858 pairs; rows older than `TEQUITY_RETENTION_DAYS` (30) are pruned daily.
Reads: `history(venue, symbol, since, until)`, `tick_at(channel, moment)`, `movers_between(venue, start, end)` (change
from stored prices, not the feed's 24h figure), `volume_leaders(venue, days)`, `status()`. The worker is
`tequity_ledger` in the startup task map. Nothing reads the ledger in a user-facing tool yet; the wire-only use
cases (since-this-morning movers, weekly leaders, movers alerts) build on it.

**Ledger use cases (2026-09-23).** `tequity_history` ("how has TSLA moved on hyperliquid this week": the stored price path,
change between first and last tick, high/low, with a note when the ledger begins inside the period), `tequity_period_movers`
(gainers/losers over a named period between stored ticks; the research-node intercept sends a venue movers ask with a
period here and one without to the live feed), `tequity_volume_leaders` (mean 24h quote volume across ticks). Price alerts
accept a venue (`alert me when TSLA on hyperliquid drops below 400`; `tasks.price_for(symbol, venue)` reads the feed). A
new task kind `movers_alert` ("tell me when any tokenized stock moves more than 10% on hyperliquid within an hour") diffs
ticks every check, reports each pair once per window (`spec.last_fired`), and is chat-only for now (no form). The morning
brief gains a "Perp venues" section: top three movers per venue, best stock, and the tokenized-stock share of volume.

## Review of 330bc651 (2026-09-23): fixes

- **Telegram link confirmation.** A `?tglink=` never attaches on page load. Signed in, the page calls
  `GET /auth/telegram/claim/preview?token=` (who the link would connect, plus a confirmation nonce bound to this
  browser session and this token, 10 minutes) and shows a Connect / Not me banner; `POST /auth/telegram/claim`
  requires `{token, confirmation}` and refuses a nonce from another session. The token is consumed only on a
  confirmed claim.
- **Disabled deployments reject updates.** `client.webhook_secret()` is None without a bot token; the webhook route
  returns 404 unless the integration is enabled and a secret exists.
- **Stale snapshots are unavailable.** `tequity.snapshot()` returns None when a snapshot is still older than 120 s
  after a refresh; movers/trending raise with the age and record an unavailable envelope; the brief and venue
  alerts skip.
- **Telegram conversations after unlink.** `bot._session_for` keeps `tg:<chat>` while this account owns it (or
  nobody does) and otherwise uses `tg:<chat>:a<account>`; the session is resolved only after the account-independent
  commands (/start, /help, /link).
- **Buttons are bound to their answer.** Callback data is `qa:<answer key>:<index>` / `sg:<key>:<index>` where the
  key is a hash of the answer text; a button whose answer is not found is refused, never resolved against a newer
  answer.
- **Recorder lease.** TTL is twice the interval plus a minute; renewal is owner-checked in one Redis EVAL; a Redis
  error means no tick is recorded; a tick within half an interval of the channel's latest is skipped (sample
  buckets are idempotent across recorders).
- **Recheck of 2910d5e8.** Reminders take a delivery channel only from an affirmative trailing clause and keep
  negations ("Do not email me" → in-app); a lowercase name in a data-ask position ("price of pepe") is a subject
  switch; `composition.plan_asks` strips the resolution note before splitting, drops format-only clauses ("Mark
  database-only claims", "If unavailable, say so") into an instruction note for the synthesis, and carries the lead
  clause's asset and constraints into subject-less clauses ("Explain how the yield can change (context: …)").

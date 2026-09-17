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
untouched. The research path reports from its composition points (the
multi-tool plan, the market bundle, a compound message's clauses) and the
synthesis streams its `summary` field through DSPy's `streamify` on the
synthesis tier's model, falling back to a whole answer on any failure.

The browser tries the stream first and falls back to the JSON route when
the response is not an event stream (a proxy that buffers or strips it) or
when x402 payment wrapping is on. It renders each card as it arrives, grows
the summary token by token, and on `done` renders the final answer exactly
as before. A streamed `error` is shaped like a failed fetch, so the existing
404 (conversation gone -> retry fresh) and 409 (stale revision -> retry)
handling still applies.

Measured on the dev instance for "is BONK safe": routed at 0.2 s, first
card at 4.0 s, second at 12.3 s, first synthesis token at 13.6 s, done at
15.5 s -- against ~15 s of blank typing indicator before.

Behind Caddy nothing needs configuring (it streams chunked responses); the
`X-Accel-Buffering: no` header covers nginx if one is ever put in front.

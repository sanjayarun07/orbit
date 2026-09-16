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

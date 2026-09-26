# Agentic wallets: a plan

Status: draft for discussion, 2026-09-26. Nothing here is built.

## What "agentic wallet" means here

A wallet the agent can act through without a click per transaction, inside a
permission the user granted and a chain or a wallet provider enforces. Three
things it is not:

- Not a server-held key. `allow_custodial_signing` stays False. The agent
  never holds a user's main-wallet key, and Anvaya never holds funds.
- Not autopilot. The agent executes rules the user wrote; it does not invent
  trades. "Strategy generation" and "24/7 autopilot" stay out of scope.
- Not a new gate. Every autonomous action passes the same risk charter and
  the same deployment mode as a human-confirmed trade, plus the on-chain
  permission scope.

## What exists today, and what carries over

| Today | Carries over as |
|---|---|
| `deployment_mode` research/execution; `allow_custodial_signing=False` (`app/deployment.py`) | The same two axes, plus a third: `agent_signing` (policy-bound key held by a wallet provider, scoped by a user grant) |
| `TradePlan` with `CONFIRM {plan_id}`, `IntentLock` fingerprint, TTL, `owner_account_id` (`app/models.py`) | The receipt for every agent action: same status machine (`submitting` → `submitted` → `executed` / `failed` / `submission_unknown`), a `signer` field added (`user` or `agent:<grant_id>`) |
| `charter_risk_node` veto before the card (`app/nodes/trading.py`) | The single policy gate, now called for agent actions too; a veto writes a refusal receipt naming the rule |
| Browser executors: Phantom, MetaMask, Coinbase Wallet SDK, Reown AppKit, Privy embedded EVM and Solana wallets (`web/*.ts`) | The grant flow: the user signs the permission in the wallet they already use |
| Tasks worker (`reminder`, `price_alert`, `movers_alert`, `brief`) and the read-only exit monitor (`app/tasks.py`, `app/exit_monitor.py`) | The trigger side: a rule fires the same way an alert does; what changes is the action |
| Durable jobs spec (`docs/durable-jobs-spec.md`): job model, states, handler contract, money and records | Every autonomous action is a job; the spec's "money and records" section is the ledger |
| Plan authorization keyed on principal, never account (`app/plan_access.py`) | Grants are per principal; a shared billing account never shares a grant |

## Principles

1. **Allowance, not access.** The agent is given a bounded spending
   permission or a small dedicated sub-wallet, never the main wallet.
2. **Receipt before signature.** Decide, evaluate the charter and the grant,
   write the receipt row, then sign. A refusal names the rule. (The
   OpenBot gateway ordering.)
3. **One policy engine.** Human-confirmed and agent-signed trades run the
   same charter code. The grant is a second, on-chain layer, not a
   replacement.
4. **Revocable in one action.** Revoke the permission on-chain, or drain the
   sub-wallet, from the Tasks screen and from chat ("stop the agent").
5. **Visible.** Every agent action appears in Tasks and in the morning brief
   with its receipt, its quote, its rule and its transaction hash.
6. **Paper first.** Every rule type ships with a dry-run mode that produces
   receipts and quotes but never signs, and runs at least two weeks on real
   prices before real funds.

## Shipped: Phase 0, slice 1 (2026-09-26)

The armed exit rule (`app/agent_rules.py`, `tests/test_agent_rules.py`):

- `arm my BONK exit: sell half if it drops 20%` (also `prepare a 50% exit on
  BONK when my exit falls 20%`, `paper arm …`, `disarm my BONK exit`) sets
  `arm_fraction`, `drop_pct`, `arm_paper` on the watched position; the size
  and the threshold are both stated, nothing is inferred.
- When the deterioration or discount alert fires, the sized exit is quoted
  at that moment, checked against the built-in caps (max per trade, max
  slippage, max price impact) and the charter's per-trade and slippage
  rules, and a receipt is written before anyone is told: trigger, quote,
  proposal, checks, status (`armed` / `paper` / `refused` with the rule
  named). Verified-only, position share and chains are listed as deferred
  and checked again by the trade path.
- The user gets an inbox item (email or Telegram too, per the position's
  channel) with the exact sell line, the token pinned by its mint: sending
  it takes the ordinary trade path (fresh quote, charter, CONFIRM card,
  wallet signature). Nothing is prepared or signed by the rule.
- Bounds: at most `arm_max_per_day` (default 3) prepared per position per
  day; refusals are receipted too; an arming failure never loses the alert.

Not yet: the one-tap button in the inbox and the receipt's link to the plan
it produced (slice 2), entry rules and DCA (slice 3), the receipts screen.

## Phases

### Phase 0: armed rules with one-tap approval (no new key material)

The exit watch and price alerts already re-quote on a schedule. Phase 0 lets
a rule prepare the trade at trigger time and push a ready CONFIRM card (web
push, Telegram) with the quote taken at that moment. The user still signs,
but the work of noticing, quoting and deciding is done.

- New task kinds: `exit_rule` (sell N% when quoted proceeds fall X% below
  baseline), `entry_rule` (buy $N when price crosses P), `dca` (buy $N every
  interval). All produce a `TradePlan` in `pending_confirmation` with the
  usual TTL, routed through `charter_risk_node`.
- Delivery: the card is one tap in the PWA or the Telegram bot; expiry
  re-quotes once, then the rule reports "expired, not executed".
- Records: the receipt exists from Phase 0; only the `signer` is always
  `user`.
- Site promise stays exactly true: it never trades without you.

Size: about a week on the tasks engine plus the card delivery. This is the
value most retail users mean by "agentic" and it carries no custody change.

### Phase 1: EVM spending permissions (Base first)

Use the wallet providers' native, chain-enforced grants rather than session
keys of our own:

- Coinbase Smart Wallet **Spend Permissions** and the ERC-7715 permission
  request flow for Reown and MetaMask-class wallets; EIP-7702 delegation
  where the user's EOA supports it.
- The grant: token, allowance per period, period length, expiry, allowed
  spender (Anvaya's agent signer contract). The user signs it once in the
  wallet they already connect with.
- The agent signer is a per-environment key held in the wallet provider's
  policy engine (Privy server wallet with policies, or an equivalent), never
  in our process. Its policy mirrors the grant: allowed contracts (the
  permitted router only), max amount, expiry.
- Execution: the existing `executors.ts` boundary gets an `agent` executor
  that calls the spender contract; swaps route through the same quote path
  and the same charter.
- Rules from Phase 0 gain `signer=agent:<grant_id>` when a grant covers the
  amount; otherwise they fall back to the one-tap card.

Why Base first: memes on Base are in scope, Coinbase Wallet is integrated,
and Spend Permissions are the most mature retail-grade grant.

Size: two to three weeks, most of it the grant UI, the spender contract
review and the reconciliation path.

### Phase 2: Solana agent sub-wallet

Solana has no retail-grade permission primitive for an existing wallet.
The honest design is a dedicated sub-wallet:

- A Privy embedded Solana wallet created for the agent, owned by the user's
  principal, with a server-side policy: Jupiter program only, max amount per
  transaction and per day, expiry. The user funds it by a normal transfer
  from their main wallet; the balance is the allowance.
- The user can sweep it back to their main wallet at any time from Tasks.
- Squads smart accounts with spending limits are the alternative for users
  who already run one; evaluate after the sub-wallet ships.

Size: about two weeks, since the Privy client and server pieces exist.

### Phase 3: later

Cross-chain rules through Relay and LI.FI, the team desk executing under a
grant, multiple sub-wallets per strategy, MEV protection (Jito tips) as a
rule option. None of it before Phases 0 to 2 have run in the beta.

## Policy and limits, one table

| Limit | Where enforced | Exists today |
|---|---|---|
| Max per trade, max slippage, max price impact | charter, quote path | yes |
| Verified tokens only, liquidity floor, trades per day | charter | yes |
| Allowance per period, expiry, allowed spender | on-chain grant or provider policy | Phase 1 / 2 |
| Cooldown between agent trades, daily loss stop | rule engine | Phase 0 |
| Pause after N failed submissions or `submission_unknown` | job engine | Phase 0 |
| Simulation before every send | existing `simulate` | yes |

## Records

One receipts table, written before the signature: rule id, trigger
observation (the card and time that fired it), charter result, grant id,
quote, `TradePlan` id, signer, transaction hash, reconciliation result,
realized outcome. The morning brief reads it. The admin Turns screen links
to it. The role-memory reflection loop can read realized outcomes from it.

## Positioning and copy

- Research beta: agentic wallets are hidden; the deployment mode refuses
  them at startup like every money-moving entry point.
- When Phase 1 ships, the site line "It never trades without you" becomes
  "It never trades beyond the rules you signed", with the grant shown next
  to the CONFIRM card so both paths are visible.
- Every agent action carries the same disclosure as a manual trade.

## Acceptance

- Rule engine replayed on recorded prices: every rule produces the same
  receipts on rerun (deterministic), measured at pass^5.
- Two weeks of paper mode per rule type on real prices before funds.
- Kill switch: revoke or sweep from Tasks and from chat, verified on-chain.
- Reconciliation: a `submission_unknown` is resolved or paused within one
  worker tick; no double submission on retry.
- A charter veto on an agent action is visible in Tasks with the rule named.

## Open questions

- Which grant standard the beta cohort's wallets actually support today
  (Spend Permissions is Coinbase Wallet only; ERC-7715 support varies).
- Whether the agent signer should be one key per environment or one per
  user principal (per principal is cleaner for revocation, costlier to run).
- Fee model: agent actions cost credits like turns, or a per-trade fee.
- Jurisdiction: whether automated execution changes the product's regulatory
  posture anywhere the beta runs. Needs a real answer before Phase 1.

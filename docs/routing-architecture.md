# Chat routing architecture

Routing determines the requested operation; it never creates a quote or signs
a transaction.

The synchronous rule function is pure. The graph now wraps it in a semantic
layer that may call an embedding API or a DSPy speech classifier. Those calls
are classification only and do not have transaction tools.

## Precedence: controls, anchored rules, then the model

`app/routing/resolver.py` decides in this order:

1. Controls and session continuations (confirm/cancel, quick actions, a
   parameter fragment for an active trade) -- deterministic, never a model.
2. Anchored rules. A rule that fired on a hard signal keeps deciding by itself:
   an execution command with its fields (`trade` / `cross_chain_swap`), an
   address or URL in the message, "my" ownership (balances, holdings, activity,
   scenarios, trade simulation of the user's own position), a control verb, an
   equity ticker. These are precise, free, and the execution paths must stay
   deterministic.
3. The speech model (`app/routing/model.py`) decides every other request --
   everything a rule matched only by topic keywords (`market`, `defi`,
   `token_research`, `current_information`, ...) and everything no rule matched.
   The rule that fired, if any, survives only as a capability/chain hint when it
   agrees with the model on the intent (`rules+model`); when they disagree the
   model's reading of the act wins (`overrides:<reason>`). A rule can no longer
   misread the act of a question ("explain what TVL means" is an explanation,
   "should I buy X?" is advice, "what's my exposure" is portfolio).
4. Fallbacks. An uncertain or abstaining model never decides: a strong rule
   may, otherwise the turn asks for clarification -- the embedding tier is not
   consulted as a second fuzzy opinion. An unavailable model falls back to the
   embedding tier (nearest labelled example with similarity and margin gates),
   then to clarification.

The model can never grant execution beyond the rules: a model-proposed `quote`
goes through `plan_execution_route` only with `explicit_action` on a crypto
subject and only when the message carries none of the competing-speech signals
(`if`, `when`, `?`, `should`, negation, ...) that already defer lexical
execution matches. Classifier results are cached per normalised request text.

Measured on `scripts/routing_eval/cases.json` (42 labelled cases, `--mode
resolve`): rules-first 38/42 intent accuracy; model-first 42/42, with the model
on the hot path for ~70% of cases at roughly 1.2 s p50 / 1.8 s p95 on
`gpt-4.1-mini`. `INTENT_MODEL` can point the classifier at a different tier;
`gpt-4.1-nano` measured 35/42 and no faster, so the default stays.

The bank in `app/routing/examples.py` is an initial seed, not a trained or
calibrated production classifier. Thresholds are configurable in `.env.example`.
Example vectors are batched once per process. Request results use a bounded
memory cache; initialization and duplicate requests coalesce behind a timed
lock. Provider errors open a short circuit breaker. Classification logs contain
method, score, margin, label, and outcome without raw prompts or credentials;
the same metadata is persisted with each chat response.

`python -m evals.semantic_routing --live` runs held-out synthetic prompts through
classification only. It can consume API quota; it cannot request or submit a
transaction. The admin lab offers the same full classifier alongside free rule
preview. The training examples and held-out cases are separate.

The API adapter follows the [official embeddings reference](https://developers.openai.com/api/reference/python/resources/embeddings/methods/create).

This change does not replace the slot parser with Lark or retrain GEPA, and it
does not claim to rebuild the Relay browser execution lifecycle. Signing remains
outside classification, behind the existing explicit confirmation workflow.

```text
User message
  -> control detection
  -> entity extraction
  -> anchored intent rules (execution, address/URL, ownership, controls)
  -> speech model for everything topical (rules kept as capability hints)
  -> typed execution draft
  -> session workflow reducer
  -> capability planner
  -> provider router
  -> quote / policy / approval
```

## Outcome-scored tools

The router's health term only sees whether a provider call *errored*.
`app/tool_outcomes.py` closes the other half of the loop: after every chat
turn, each tool that ran is credited a success when its result was usable
(the call did not fail and the answer validator did not find the answer
ungrounded in the retrieved data) and a miss otherwise. Counts are kept per
tool per day in Postgres (`tool_outcomes`, 14-day window, refreshed every
5 minutes) with an in-process fallback, and `ProviderRouter._score` adds
`PROVIDER_OUTCOME_WEIGHT * (rate - TOOL_OUTCOME_PRIOR_RATE)`.

The rate is Beta-smoothed with a prior of `TOOL_OUTCOME_PRIOR_RATE` worth
`TOOL_OUTCOME_PRIOR_WEIGHT` observations, so a tool with no evidence sits
exactly at the prior and earns no adjustment (provisional), and only moves
as real turns accumulate (trusted). A tool that keeps returning unusable data
therefore ranks down on its own; nobody edits a matcher. `GET
/admin/tools/outcomes` shows every tool's calls, rate and current adjustment.

## Module boundaries

- `app/routing/contracts.py` defines stable route and draft types.
- `app/routing/controls.py` recognizes cancel, confirm, modifier, and conceptual
  execution messages before ordinary content classification.
- `app/routing/entities.py` extracts canonical chain and address evidence.
- `app/routing/intent_router.py` applies ordered semantic rules and reports why
  a route matched.
- `app/routing/trade_parser.py` extracts execution fields from the current turn
  without inheriting history.
- `app/routing/tool_metadata.py` provides fallback metadata only for MCP tools
  that do not publish an explicit capability manifest.
- `app/capability_router.py` is a compatibility facade for existing imports.

## Execution invariant

Provider selection requires chain evidence:

- Solana-only execution uses Jupiter.
- Any non-Solana chain or explicit multi-chain route uses Relay.
- A symbol-only request such as `buy ANSEM` remains in `collect` mode and asks
  for the chain. It must not default to Relay or Jupiter.

The current user message is authoritative. Session context may fill a missing
field only for an elliptical continuation of the active collecting workflow.
A new trade verb with a named asset starts a new workflow and cannot inherit an
older quote. Research and portfolio turns clear natural-language execution
context; an already-created approval card remains addressable only by its plan
identifier.

## Observability

`POST /admin/intents/preview` exposes the semantic intent, capabilities, chain
evidence, confidence, route mode, selected execution provider, missing fields,
and rule reason without consuming provider quota.

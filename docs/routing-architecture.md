# Chat routing architecture

Routing determines the requested operation; it never creates a quote or signs
a transaction.

The synchronous rule function is pure. The graph now wraps it in a semantic
layer that may call an embedding API or a DSPy speech classifier. Those calls
are classification only and do not have transaction tools.

## Semantic competition and abstention

Closed controls and clear requests retain the fast path. A lexical execution
match that competes with advice, questions, market data, negation, or conditional
language is deferred. Unmatched requests also enter semantic resolution:

1. Exact matches in the versioned labelled bank require no API call.
2. Nearest-neighbour embeddings use the highest label similarity and the margin
   over the competing label. Cosine similarity is not a calibrated probability.
3. A close competition above the similarity threshold asks for clarification.
   Low similarity or an unavailable embedding service uses structured DSPy
   speech classification; low model confidence or invalid output clarifies.

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
  -> prioritized intent rules
  -> typed execution draft
  -> session workflow reducer
  -> capability planner
  -> provider router
  -> quote / policy / approval
```

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

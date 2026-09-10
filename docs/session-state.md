# Conversation state invariants

Orbit treats a chat as an ordered state machine, not as an unstructured prompt log.
Every change to routing must preserve these invariants.

## Turn invariants

1. At most one turn may mutate a session at a time, across tabs and server workers.
2. A client must start from the current monotonic `context_revision`.
3. The user message, assistant result, artifacts, and next context commit together.
4. The original user utterance is immutable. Contextual resolution is stored in a
   separate graph field and cannot rewrite display history.
5. Redis recovery and the live reducer choose the same focused entity.

## Intent invariants

1. Explicit current-turn language outranks previous history.
2. Context is used only for an elliptical follow-up or a matching named entity.
3. Portfolio requests never fall back to web search for wallet-specific facts.
4. Same-chain Solana execution uses Jupiter. Any explicit non-Solana or cross-chain
   execution uses Relay.
5. Research and portfolio topic switches close natural-language trade collection.
   Immutable quote/plan IDs remain separately executable after user review.

## Quick-action invariants

1. Actions are server-issued commands, not trusted browser instructions.
2. The full action must match an action on the latest persisted assistant response.
3. Actions from older revisions are rejected with HTTP 409.
4. History restoration never converts typed actions back into plain prompts.
5. Only the newest response exposes enabled quick actions in the UI.

## Workflow transition table

| Current state | New turn | Next state |
| --- | --- | --- |
| idle | incomplete trade | collecting trade details |
| collecting | matching amount/chain/slippage | merged collecting state or pending approval |
| collecting | new trade | replacement collecting state |
| collecting | research or portfolio | idle |
| pending approval | new trade | replacement workflow |
| pending approval | research or portfolio | idle; quote card remains independently reviewable |
| any | cancel/dismiss | idle |

Changes that affect these transitions require sequence tests, reload tests, stale
revision tests, and provider-boundary tests—not only single-message classifier tests.

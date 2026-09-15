# DSPy optimizer -- proof of concept, not a production pipeline

`scripts/optimize_signature.py` is a bounded, manual proof of concept that
this app's `dspy.ReAct` signatures can be run through DSPy's own
`BootstrapFewShot` optimizer at all. It is **not** wired into
`app/nodes/runtime.py` -- running it produces a JSON artifact here for
manual comparison, nothing more. Confirmed at build time: zero uses of
`dspy.teleprompt`/`BootstrapFewShot`/`MIPRO` exist anywhere else in this
codebase.

## What this proof of concept has

- One hand-curated example set (`trade_simulation_examples.json`, 7 rows)
  for `app.nodes.runtime.TradeSimulationExtraction` -- the newest, smallest
  signature, with an unambiguous success shape (`should_simulate` +
  `input_mint`/`output_mint` correctness).
- A real metric function (exact match, no mocking of the underlying tools
  -- `sol_balance`/`spl_balances`/`search_verified_tokens` hit real Solana
  RPC/Jupiter endpoints during compilation).

## What a real production pipeline still needs (not built here)

1. **A persisted, outcome-labelled training corpus.** `app/sessions.py`
   logs `(user_content, assistant_content, assistant_metadata)` per turn to
   Redis/in-memory, trimmed to the last few messages -- there is no durable
   store of full trajectories with a pass/fail label attached. Mining real
   examples from production traffic (rather than 7 hand-written rows) needs
   that persisted first, plus a decision on what "success" means per
   signature -- did the user confirm the trade plan? Did a research answer
   get a follow-up that reads as dissatisfaction? Neither is captured
   today.
2. **A metric for every other signature**, not just
   `TradeSimulationExtraction`. `trade_planner`'s extraction, the research
   agent's answer quality, and `portfolio_agent`'s summaries each need
   their own definition of correctness -- there isn't an obvious exact-match
   check for prose quality the way there is for a mint address.
3. **A validation/comparison step before ever adopting a compiled program**
   -- run the optimized version against a held-out set, compare it to the
   current zero-shot signature's live behavior (accuracy, cost, latency),
   and only then consider swapping `app/nodes/runtime.py` to use it. This
   proof of concept stops at "does the machinery work at all" deliberately.
4. **A decision on cadence** -- optimizing once by hand vs. a recurring job
   that re-compiles from fresh production data. Out of scope until 1-3
   exist.

## Running it

```
.venv/bin/python scripts/optimize_signature.py
```

Requires the same live API keys already configured for the running app
(real LM + real Solana RPC/Jupiter calls -- this is not a mocked/offline
run). Not part of `pytest` or any CI/scheduled job.

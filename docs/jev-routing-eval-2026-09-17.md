# Jev as the routing decision model: first measurement (2026-09-17)

The question was whether TypeSafe's Jev (`jev-1.13.0`, a typed-decision model
that returns a probability distribution instead of generating JSON) should
replace or front the speech model at the routing decision seam. This is the
first measurement, on the harness's 74 labeled cases, same rules ahead of the
model for both backends. Runs: `scripts/routing_eval/runs/2026-09-17-*.json`.
Rerun: `harness.py --mode resolve --backend {current,jev}` (the current
backend must be run with DSPy's cache off, or its latency is the cache's).

|                                   | Current (gpt-4.1-mini, cold) | Jev 1.13.0 |
|-----------------------------------|-----------------------------:|-----------:|
| Intent accuracy (74 cases)        | 70/74 = 94.6%                | 68/74 = 91.9% |
| Cases reaching the model          | 55                           | 55 |
| Accuracy on model-decided cases   | 4 misses                     | 5 misses (90.9%) |
| Latency, model-decided, p50 / p95 | 1203 / 3009 ms                 | 970 / 1252 ms |
| Input tokens per call             | not instrumented             | ~910 (the criteria text) |
| Cost per 1,000 calls              | ≈ $0.3 (estimate at $0.40/M in, $1.60/M out) | $0.038 measured |
| Calibration of stated confidence  | not available (generated JSON) | [0.9–1.0] 96% of 47 · [0.8–0.9) 50% of 4 · below 0.8: 4 cases, 3 right |

## What the misses are

Both backends miss the same three knowledge-base questions ("what is Morpho
Blue and how does it work", Aave E-mode, what backs USDe): both class them
`explain` and the resolver maps that to `general`, while the case expects
`research` (knowledge base first). That is an ambiguity in our own taxonomy,
not a backend difference, and it is the most valuable thing this run surfaced.
Both also miss `slang-balances-vs-tx-2`, which the rules decide before any
model sees it.

Jev alone misses two: "what's the hot narrative rn" (`research/general` at
0.87, under the 0.90 gate, so it fell to `general`), and "what are my trading
limits", where Jev answered `policy` correctly but `domain=equity` at
confidence 1.00 -- a wrong answer stated with certainty, which then routed the
policy question to research. The domain criteria are mine, not the
signature's (the speech model infers domain from the field name alone), so
this is likely fixable wording; it is also the one calibration failure that
matters, and it is on record.

One Jev answer was right and rejected: "what backs USDe" as `research/crypto`
at 0.59, under the 0.90 threshold. The calibration bins say the gate is
right to distrust 0.8-ish answers from Jev (2 of 4 correct), so the threshold
should stay where it is.

## Reading it

- Accuracy: Jev is two cases behind on 74, with one of the two a criteria
  wording issue. Not a win; not a loss that settles anything at this sample.
- Latency: roughly equal at p50 (~1 s both), Jev tighter at p95 (1.25 s vs
  3009 ms). The cost/latency story from the launch material does not show up
  as a latency win at this input size; the ~900-token criteria block is most
  of each request and could be cut.
- Cost: about an order of magnitude cheaper per call, measured, not 238x --
  because our decision carries a long question, and the comparison is
  against an already-cheap model.
- Calibration: the real new information. 47 of 55 answers came back at ≥0.9
  and 96% of those were right; below 0.9 it is a coin flip. That is exactly
  the shape a fast path needs: trust ≥0.9, fall through below. The speech
  model cannot offer this at all.

## Not a decision yet

74 cases can rule a backend out; they cannot rule one in. Before any change
to the live router: grow the cases from real traffic (the research-gaps log),
fix the KB taxonomy question independently of backend, shorten and re-test
the Jev criteria, and rerun both. If Jev then matches accuracy at ≥0.9 with
the same calibration, the design is a fast path with fallthrough to the
current router below threshold -- never a replacement, and never a path to
execution: `explicit_action` stays enforced in code as "an explicit crypto
quote", whatever any model says.

## Testing on more real-world prompts

Labels are the expensive part, so the workflow finds the prompts worth
labeling instead of labeling everything:

1. **Collect** — `scripts/routing_eval/collect.py --catalog --file my_prompts.txt [--gaps 30]`
   merges prompts with provenance into `candidates.json`: your own (one per
   line, typed the way users type), every tool's `answers` examples from the
   catalog (already labeled with their tool), and on a Postgres deployment the
   `research_gaps` log of turns that fell through to web search. Already
   labeled cases are skipped.
2. **Disagree** — `harness.py --mode disagree` runs BOTH backends over the
   unlabeled prompts and writes `to_label.json` with only the ones that can
   change the table: where the backends disagree, or Jev is under the 0.90
   gate. Prompts both agree on at high confidence almost never do.
3. **Label** — fill `expected_intent` (and forbidden tools) on those entries
   and append them to `cases.json`. Tens of these, from real phrasing, beat
   thousands of synthetic ones.
4. **Measure** — `harness.py --mode resolve --backend current` (DSPy cache
   off) and `--backend jev`; compare the table.

First disagree run, the 70 catalog example prompts: 64 agree, 6 disagree,
21 agree with Jev under the gate. Nearly all of the 27 are one pattern: on
terse, jargon-shaped prompts ("hot metas right now", "honeypot check",
"freeze authority on <mint>") Jev puts mass on `abstain` at 0.4–0.7, and on
the six disagreements it chose the right act (`research/crypto` for
"background on Berachain", "Fed decision impact on crypto") at 0.39–0.60, so
the gate sent them to `general`. Under-confidence on short prompts, not wrong
choices -- the `abstain` criterion wording is the first thing to change and
retest. Some catalog examples are templates (`<mint>`, `0x...`) or
descriptions rather than prompts; realise them with real addresses before
counting them as real-world.

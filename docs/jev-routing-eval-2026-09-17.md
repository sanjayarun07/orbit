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

## Second measurement: 100 labeled cases (26 added from the disagree run, labels approved by the user)

|                                   | Current (gpt-4.1-mini, cold) | Jev 1.13.0 |
|-----------------------------------|-----------------------------:|-----------:|
| Intent accuracy (100 cases)       | **93/100**                   | 84/100 |
| Cases reaching the model          | 74                           | 62 (Jev abstains push more to rules) |
| Latency, model-decided, p50 / p95 | 979 / 1634 ms                | 996 / 1111 ms |
| Cost per 1,000 calls              | ≈ $0.3 (estimate)            | $0.038 measured (~910 tokens/call) |
| Calibration                       | n/a                          | [0.9–1.0] **98% of 48** · [0.8–0.9) 50% of 10 · below 0.8: 19 cases, 47% |

Runs: `scripts/routing_eval/runs/2026-09-17-*-100.json`.

The gap widened, and almost entirely for one reason: on the added prompts --
short, jargon-shaped, address-bearing -- Jev picks the right act at low
confidence (background on Berachain 0.53, Fed decision impact 0.37, what does
<address> hold 0.68), the 0.90 gate rejects it, and the resolver falls to
`general`. Its ≥0.9 answers are 98% right; everything under 0.8 is a coin
flip. So the calibration is real and the gate is correct; what Jev lacks is
confidence on terse prompts. Three of its misses are genuine act
disagreements worth noting: "is <token> safe" and "is <address> on bsc safe"
as `advice` (we label the security dossier `research`), and "can I sell
<address> on ethereum" as `quote` at 0.42 -- a honeypot question that must
never become a swap quote; the current backend also misses it, as `general`,
and the case now forbids the swap tools for both.

Shared by both backends and unchanged from the first run: the three
knowledge-base `explain`-vs-`research` cases (a taxonomy question), and two
rules-decided cases the model never sees -- "recent activity for this wallet"
(rules say portfolio, label says research) and "open perps for <address>"
(rules say research; the user labeled it portfolio). Those two are rule
decisions to settle with the user, not backend questions.

Next, in order: (1) reword Jev's `abstain` criterion and shorten the
criteria block, rerun -- this is the Qwen lesson again, spec before model;
(2) settle the two rule cases and the KB taxonomy; (3) more prompts from
real traffic via `collect.py --gaps` once staging has Postgres. The
adoption bar is unchanged: match the current backend's accuracy at ≥0.9
confidence with this calibration, then a fast path with fallthrough, never
a replacement.

## Correction: the three "shared misses" were the harness, not the taxonomy

The knowledge-base questions both backends "missed" (Morpho Blue, Aave
E-mode, USDe) are anchored by the deterministic `knowledge_base` rule, which
the speech model cannot override -- when its resolver snapshot is loaded. The
app warms that snapshot at startup and every two minutes; a bare harness
process never did, so `knowledge_tool.matches()` was False for everything and
the harness measured a router missing its anchor. `harness.py` now warms the
snapshot as the app does and prints how many entities it holds (2,652 here);
zero means the KB cases are not meaningful for that run.

The same empty-snapshot state exists in production before the first warm and
after a refresh that failed (it only logged). In that state every protocol
question silently falls to the speech model and is answered from the model's
memory. `/readyz` now carries a `knowledge_snapshot` check, degraded when
Postgres is configured and the snapshot is empty, so the state is visible.

**100 cases, knowledge anchor present** (runs: `runs/2026-09-17-*-kb-warm.json`):

|                                   | Current (gpt-4.1-mini, cold) | Jev 1.13.0 |
|-----------------------------------|-----------------------------:|-----------:|
| Intent accuracy                   | **97/100**                   | 87/100 |
| Reaching the model                | 67                           | 55 |
| Latency, model-decided, p50 / p95 | ~980 / 2660 ms               | 960 / 1085 ms |
| Calibration                       | n/a                          | [0.9–1.0] 98% of 44 · below 0.9: 25 cases, 56% |

The current backend's three remaining misses: two rules-decided cases to
settle with the user ("recent activity for this wallet", "open perps for
<address>") and "freeze authority on <mint>" (`general`). Jev's ten extra
misses are unchanged in kind: right act under the 0.90 gate on terse
prompts, plus `advice` for "is X safe" and `quote` @0.42 for "can I sell".
The `explain`-to-`research` mapping change considered earlier is not needed
and was not made.

## The synthesis tier, and how a domain model gets measured on it

A domain model such as DMind-3-mini is not a routing candidate (it generates
free text with a thinking mode; the decision seam wants a typed answer in a
second) but it may write better answers from gathered evidence. That job is
now its own tier: `settings.synthesis_model` (e.g. `hosted_vllm/DMind-3-mini`
with `HOSTED_VLLM_API_BASE` set) is used by `runtime._call_synthesis_lm` for
the three programs that write from evidence -- the equity brief, the
knowledge-base answer, the token deep dive -- with a transient failure
falling back to the primary path. Unset, nothing changes. Routing and the
tool loops never see it.

Measuring it isolates writing from retrieval:

1. `scripts/synthesis_eval/capture.py --defaults|--file prompts.txt` runs
   prompts through the real agent with a recorder on and saves each
   synthesis program's exact inputs (the evidence the tools gathered) plus
   the primary model's answer at the time, to `bundles.json`.
2. `scripts/synthesis_eval/replay.py --model openai/gpt-4.1-mini --model hosted_vllm/DMind-3-mini`
   has every model write from the identical evidence (DSPy cache off) and
   scores each answer with the app's own step-7 validator -- grounding
   (figures not in the evidence), provenance, freshness, consistency --
   plus latency and tokens.

Baseline, thirteen bundles (four token deep dives, seven knowledge-base
answers, two equity briefs): gpt-4.1-mini 0% validator warnings, 4.8 s p50 /
11.3 s p95, ~470 output tokens, ~2,300 characters. Run files stay untracked
(they hold raw answers and evidence, full of addresses that trip the secrets
scan); the numbers live here. The bar for a domain model is the same
warn-rate at equal evidence; tone and vocabulary do not count, grounding does.

## DMind-3-mini on the synthesis tier: first measurement

Served at a hosted OpenAI-compatible endpoint (`replay.py --api-base
DMindAI/DMind-3-mini=<url>/v1`); thirteen bundles, identical evidence,
DSPy cache off. The raw endpoint returns chain-of-thought inside `content`
and an unconstrained chat ran out of tokens mid-thought, but under the
DSPy-structured synthesis programs every answer parsed and none leaked
thinking.

|                                   | gpt-4.1-mini (replay) | DMind-3-mini |
|-----------------------------------|----------------------:|-------------:|
| Step-7 validator warnings         | 0%                    | 0% |
| Numeric claims / ungrounded       | 139 / 3 (2%)          | 100 / 1 (1%) |
| KB answers citing passages        | 5 of 7 (2.3 cites/answer) | 3 of 7 (1.3) |
| Latency p50 / p95                 | 4.6 / 12.3 s          | 6.3 / 23.5 s |
| Output tokens (thinking included) | ~400                  | ~1,200 |

Read together: the validator cannot separate them (0% both), and a strict
numeric audit slightly favours DMind, which states fewer figures. The
difference shows where numbers do not reach. On "what is Morpho Blue and
how does it work" DMind wrote a confident, uncited mechanism -- an order
book, "Morpho Lazy vaults", a "winning deposit rate" -- none of it in the
passages and not how Morpho Blue works; gpt-4.1-mini stayed inside the
passages with citations. Across the seven knowledge-base bundles DMind
cited passages in three, gpt-4.1-mini in five. On the token deep dives,
where the evidence is dense with figures, both were disciplined and DMind's
structured tables were arguably the better read; the "$20M BONKDAO"
claim it made is in the evidence.

The "baseline" row in results is the app's post-processed final answer,
not the raw program output, so its citation count (5.1) is not comparable;
replay-versus-replay is the fair comparison.

Verdict: not adopted. On the job that suits it -- figure-heavy deep dives
-- it matches the current model at higher latency and no cost advantage
yet measured; on knowledge answers it fabricates mechanism when the
passages run thin, which is the one failure the synthesis tier exists to
prevent. The grounding gap is mechanism-level, invisible to a numeric
check, so any next round needs a claim-level judge (a second model
scoring "is each sentence supported by a passage") before the tier could
be trusted to a smaller domain model. Reproduce with the command above;
run files stay untracked.

## DMind-3-mini on both jobs, side by side

The routing harness gained `--backend lm --lm-model <id> --lm-api-base <url>`:
any model at the classification seam, running the same SpeechResolution
program. DMind-3-mini on the same 100 cases, knowledge anchor present:

| Routing (100 cases)               | gpt-4.1-mini | Jev 1.13.0 | DMind-3-mini |
|-----------------------------------|-------------:|-----------:|-------------:|
| Intent accuracy                   | **97**       | 87         | 88 |
| Model-call latency p50 / p95      | ~1.0 / 2.7 s | 1.0 / 1.1 s | 2.4 / 7.7 s |
| Calls that failed to the embedding tier | 0      | 0          | 3 |
| Stated confidence ≥0.9: accuracy  | n/a          | 98% of 44  | 92% of 53 |
| Misses at ≥0.95 confidence        | n/a          | 1 (a wrong domain) | 4 -- three of them `quote` on lookups |

| Synthesis (13 bundles)            | gpt-4.1-mini | DMind-3-mini |
|-----------------------------------|-------------:|-------------:|
| Validator warnings                | 0%           | 0% |
| Numeric claims ungrounded         | 2%           | 1% |
| KB answers citing passages        | 5 of 7       | 3 of 7 |
| Mechanism fabrication observed    | no           | yes (Morpho Blue) |
| Latency p50 / p95                 | 4.6 / 12.3 s | 6.3 / 23.5 s |

The routing misses are the telling part. DMind labelled "what is <address>
on base" `quote` at confidence 1.00, "honeypot check" `quote` at 0.95, and
"last swaps for <mint>" `quote` at 0.89 -- lookups read as swap requests,
stated with certainty. None reached execution (the domain was `wallet` or
`general`, and `explicit_action` is gated in code), so they became
clarifying questions rather than quotes; but a classifier that is confident
in the dangerous direction is the opposite of what a fast path needs. Jev's
one high-confidence miss was a wrong domain on a policy question; its
under-confidence on terse prompts errs the safe way.

Verdict on both roles: not adopted for either. Routing: slower than both
alternatives, less accurate than the current model, and miscalibrated
toward `quote`. Synthesis: matches the current model on figure-dense
material, fabricates mechanism on thin passages.

## Three routing decisions settled (2026-09-17)

1. **A pasted address wins over own-wallet phrasing.** "recent activity for
   this wallet <address>" is a lookup about that address; the own-wallet
   rules now yield when an address is present. Without one, "my recent
   transactions" stays portfolio.
2. **Perp positions are a wallet's state** -- "open perps for <address>",
   "my open perps", "hyperliquid positions for <address>" are portfolio
   asks (rule `perp_positions`, anchored). A pasted address becomes the
   turn's read-only wallet, never replacing a connected one, and the
   portfolio node answers from the focused Hyperliquid tool. Hyperliquid
   accounts are EVM: a non-0x wallet with a perps ask gets a question back
   ("did you mean a different wallet, or another venue?"), per the rule that
   an ambiguous query is asked about, never guessed.
3. **An equity ask never enters the token deep dive.** "is MSTR a good buy"
   is classed equity by the router, but the deep-dive intercept ran first
   and resolved MSTR to a tokenized-stock namesake; it now yields to the
   equity path.

Current backend, 100 cases, knowledge anchor present: **98/100** (was 97;
`hyperliquid-positions` relabeled to portfolio under decision 2). The one
remaining miss is "freeze authority on <mint>" going to `general`.

## Deterministic tool ranking: the ten catalog misses (2026-09-17)

Router mode -- Layer-1 capabilities, then the provider router's ranking, no
model -- missed ten of the 66 router cases. None was a scoring problem; each
was a gap in what a rule or a tool's own gate could recognise, or a case that
was itself underspecified:

- **A bare Solana address never named its chain.** `_chain()` demanded a
  chain word, so every Solana wallet tool's gate failed on "recent
  transactions for <address>" and nothing was a candidate, Helius included.
  A base58 address of Solana's length can only be Solana; it now infers the
  chain, at the helper and at Layer-1. An 0x address stays ambiguous across
  EVM chains and still needs the word.
- **Vocabulary the gates lacked**: "oi" (Hyperliquid open interest),
  "spiked/pumped/dumped" (gainers/losers), "newest ... profiles",
  "hold/holds" (wallet balances), "delistings" (plural), "can I sell" and
  "sellable" (a honeypot question, not a sell order).
- **Rule order**: security jargon now outranks both the trade verbs and the
  generic address+check wallet rule, so "can I sell 0x..." and "rug check
  0x..." are token-security asks.
- **A market-wide gainers ask** ("what coins spiked hardest today") had no
  tool: the gainers tool required a chain category. It now runs market-wide
  over CoinGecko's top 250 when no chain is named -- the same query the
  overview card's movers list makes.
- **Two cases were underspecified.** "honeypot check" with no token, and
  "rug check 0x..." with no chain, cannot be ranked to a chain-specific
  tool; the product asks (a security ask naming no token now gets "which
  token?"; an 0x address with no chain already got "which chain?"). The
  cases carry an address and a chain. "what is <address> on base" and the
  two honeypot asks accept either of two tools that answer them (CoinGecko
  or Birdeye for identity+price; honeypot.is or GoPlus, which reports
  is_honeypot and sell tax).

One keyword nudge tried along the way was reverted: adding "hold" to a
balances tool's keywords tipped it over its sibling on an existing case.
Gates decide reachability; keywords are not the place to fix ranking.

Router mode: **66/66 top-1, 97% recall@8, 0 forbidden@1** (was 56/66).

## Round two, and the deployable A/B build

Jev's `abstain` criterion was narrowed ("ONLY when the act itself cannot be
determined ... a terse, slang or fragmentary request is NOT abstain") and the
criteria block cut from ~900 to ~590 tokens; the instructions now say that
short, casual messages are normal, not ambiguous. Same 100 cases, knowledge
anchor present:

|                                   | Current (gpt-4.1-mini, cold) | Jev round 1 | Jev round 2 |
|-----------------------------------|-----------------------------:|------------:|------------:|
| Intent accuracy                   | 98                           | 87          | **91** |
| Answers at >=0.9 confidence       | n/a                          | 98% of 44 right | **100% of 49 right** |
| Model-call latency p50 / p95      | 1.1 / 1.8 s                  | 1.0 / 1.1 s | 0.94 / 1.24 s |

Runs: `runs/2026-09-17-*-round2.json`. The rewording did what it was meant
to: more answers above the gate, all of them right. The remaining misses are
the same terse asks at 0.5-0.8 ("background on Berachain" 0.52, "Fed decision
impact" 0.49), plus "is X safe" as `advice`.

That shape decides the build. `ROUTING_BACKEND` (app/routing/backends.py)
selects what answers the classification seam:

- `speech_model` -- the default, unchanged.
- `jev` -- Jev decides; a Jev outage hands the turn to the speech model.
- `jev_fallthrough` -- Jev decides when its confidence is at or above the
  resolver's threshold (0.90) and it is not abstaining; otherwise the speech
  model decides. The variant the measurements recommend.

Every turn's `intent_decision` log line carries `routing_backend` and
`decided_by` (`jev`, `speech_model:jev_uncertain`, `speech_model:jev_unavailable`,
or `none` for a cached or rule-anchored decision), and `/readyz` reports the
configured backend, so two instances on two domains can be compared from
their logs: share of turns Jev decided, and -- joined with `research_gaps`
and the user's follow-ups -- whether those turns went better or worse. The
audit refuses a production instance on a jev backend without
`TYPESAFE_API_KEY`. Whatever the backend, `explicit_action` is enforced in
code and nothing at this seam can authorize a swap.

To run the B instance: a second stack on its own domain with the same
template and `ROUTING_BACKEND=jev_fallthrough`, `TYPESAFE_API_KEY` set.

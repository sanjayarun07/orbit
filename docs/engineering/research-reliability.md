# Research reliability: one question, one evidence state

Orbit's recurring failure is not a shortage of tools. It is a mismatch between
the question, the data a tool actually covers, and the claim made in the final
answer. A fluent search result can answer a nearby question; a source can
mention a protocol without proving a mechanism; a live market value can be
correct but for the wrong venue, chain, wallet, or time window. More regex
intercepts and more answer rewrites do not repair that boundary.

## Product contract

For each turn, record one resolved question: entity and identity, requested
operation, scope and venue, time window, filters, comparison conditions, and
the facts needed to answer. Conversation context may resolve a pronoun but
must not replace the latest user request. The answer is successful only when
each material claim has accepted evidence for **that** question. Otherwise it
must identify the missing fact, offer a narrower verified finding, or ask the
one clarification that would change the evidence path.

There are two evidence lanes, selected by the question contract rather than
by a keyword in the prompt:

1. **State lane:** connected wallet, order or exit quote, holders, pool state,
   venue ranking, yield, and other actionable or time-sensitive figures. A
   provider must cover the exact identity, venue, metric, and time window.
   Search can supply context or a source lead, never substitute for a state
   read. Execution and approval remain separate operations.
2. **Research lane:** explanations, historical mechanisms, comparisons, and
   open-ended investigations. Web and indexed search discover candidate
   answers and URLs. Direct page reads establish passages and provenance.
   A provider-written summary is never treated as if the cited page had been
   checked. A candidate qualifies only when its sources establish every
   material condition in the comparison.

This distinction is compatible with the public product descriptions:
[Perplexity describes web-first research, planning, and cited sources](https://www.perplexity.ai/en-GB/hub/products/deep-research), while
[Minara says it uses curated financial data rather than web search for actionable insights](https://minara.ai/docs/technology/tools-integration).
Their private implementation is not known; these are design signals, not an
architecture to copy.

## Decision cycle

```text
user turn + relevant context
  -> question contract and required facts
  -> eligible capability shortlist (catalog is the source of truth)
  -> cheapest useful retrieval in the appropriate evidence lane
  -> source/record inspection with identity, scope, time and provenance
  -> coverage state: supported / contradicted / missing, per condition
  -> one gap-directed retrieval when a capable source can close a named gap
  -> answer from accepted evidence
  -> claim and figure audit, then repair or narrow/abstain
  -> user answer with citations, as-of time, unknowns, and research trail
```

The coverage state is the hand-off between retrieval and planning. The gap
planner must see checked passages and missing conditions, **not** the search
provider's answer-shaped prose. A new round must add evidence for a named gap;
repeating the original broad query is not progress. Pages already fetched in
a turn should be reused. Stop on complete coverage, no eligible source,
exhausted budget, or no marginal evidence gain. Stopping does not imply the
answer is true; the final output may be partial.

## Current implementation and remaining work

`app/contracts.py`, `app/tool_catalog.py`, `app/evidence_pipeline.py`, and
`app/research_loop.py` already supply the contract, coverage metadata, source
discovery, page inspection, and claim checks. The 2026-09-25 change moves the
gap review **after** an initial, shallow page inspection and passes a compact
page-derived coverage brief. A second search or a small final first-party
read allowance can then target the missing condition. Repeated rounds reuse
page reads. The complete conceptual research question stays in one contract
path, including its format constraints; the old outer keyword gate could
split “transfer to another wallet and trade” into a research answer plus an
unrelated second answer. Rejected search prose is kept in the trajectory
instead of being rendered under a verified abstention.

That is a first slice, not UAT sign-off. A 2026-09-25 live prompt initially
produced an unsupported list of wrapped-token systems through the legacy
path. After the routing correction, the same prompt entered page review and
withheld unsupported matches, but took 116 seconds and 20 evidence calls;
the screen also mixed a second answer and raw search prose with the
abstention. The subsequent single-path, clean-output run still took 116
seconds and could not verify an exact match. With shallow-first inspection,
the same live prompt took 66 seconds and 8 evidence calls, including a
gap-directed second search, but still could not verify the match. That is a
cost and latency reduction, **not** an answer-quality pass: the second query
narrowed to one documentation path and missed complementary historical
first-party pages. The loop still has multiple model
audits and repair passes, and its nominal model-call limit does not count all
inspection and synthesis calls. `required_facts` are written by the planner
but do not yet have a typed, per-fact evidence ledger shared by research and
state contracts. Historical-source recall is bounded by discovery, and long
turns can exhaust the chat timeout after substantial work. These should be
fixed by consolidating the existing checks around one evidence ledger and one
total-turn budget, not by another prompt-specific route.

The next engineering slice should:

- Define typed evidence records (claim/fact, subject identity, scope, event
  time, source URL or provider record, exact passage/field, retrieval time,
  provenance, and support status). Normalize direct API facts and page
  passages into the same ledger without pretending that they have the same
  freshness or authority.
- Make the coverage evaluator identify unsupported required facts and
  contradictions before writing prose. Feed only those gaps to the planner.
  When a primary page establishes only part of a historical mechanism,
  broaden to another first-party publication or era rather than restricting
  the next query to the same host path. Measure verified recall; do not
  hard-code a protocol or article URL.
  Once a draft exists, an unsupported claim may trigger one targeted read if
  budget remains; otherwise remove or qualify it rather than rerunning broad
  synthesis.
- Put **all** provider, reader, model, and synthesis calls under one
  deadline and cost/call budget. Preserve verified partial evidence on
  timeout. Record which step exhausted the budget.
- Render the user's answer separately from the diagnostic trail. Put the
  direct answer and decisive citations first; show uncertainty and as-of
  times in plain language. Search leads and internal call logs belong in a
  collapsible research detail view, not below every answer as if they were
  verified sources.
- Remove superseded intercepts and duplicate gates only after an episode
  suite proves the consolidated path has equivalent safety and better recall.

## 2026-09-25 ledger and turn-budget slice

`app/research_ledger.py` now stores a status for each qualifying condition of
each candidate. A supported condition carries the exact passage, inspected
source URL and fetch time; a search snippet or a model verdict without a
validated condition-to-passage map remains unknown. The gap review consumes
this ledger instead of a prose summary of search cards. If the model narrows
its follow-up to a `site:` path the user did not ask for, the search is
reformulated from the candidate and missing condition across original
first-party publications and eras. The model still chooses the capability.

`TurnBudget` now measures model, provider, direct-page and synthesis calls
against a single deadline. It reserves time for an answer and renders checked
passages as a limited finding when audited synthesis cannot finish. The
trajectory records coverage and call counts. This is limited to the open
research loop; state-contract paths still use their existing budget and fact
gate. The ledger currently maps comparison conditions, while free-form
`required_facts` remain a plan rather than independently checked slots. A
further pass must unify those slots with typed API facts for state answers.

The code and focused tests establish the new boundary, but live verified
recall and cost must be measured before treating this as a UAT pass.

Home news research now begins with two independent discovery queries within
the same turn budget: one for the original event or primary data, and one for
the dated market response and alternative explanations. Unlike comparison
research, the convergence step verifies event and market passages as separate
facts from directly read pages; no single article must prove both. The news
path uses the question contract and skips the comparison planner call. The answer
brief separates the event's status, observed market behavior, and causal
interpretation. If a market page does not yield a verified observation, the
answer names that gap. This is limited to Home headline research. It does not
turn search snippets into evidence, change state-tool eligibility, or
establish that a market move was caused by a contemporaneous announcement.

The first live Fed-headline pass through the comparison verifier found the
official announcement but spent 172 seconds and withheld the answer because
no one candidate proved every event and market condition. The first fact-level
pass found three verbatim passages on the Fed page in 37 seconds with two web
calls, but its market source was an irrelevant homepage; the response remained
a verified partial. Source selection now enforces separate search axes and
records both the source leads and final answer-audit outcome. A subsequent
Fed pass found both a dated proposal report and a dated crypto-market report
in 53 seconds (two web calls, seven model calls) and passed the claim audit.
A bond-yield headline found both facets in 74 seconds, but its draft made an
unsupported negative claim about a final rate action; the claim audit
reduced it to checked passages. The news path now gets one bounded rewrite
to remove such claims before it falls back to checked facts. These are single
live probes, not pass^k evidence. Repeat live coverage and latency
measurements before extending this path to other kinds.

Seven single-run live checks of the dual-token episode failed verified recall
despite safer abstention. Wall times ranged from 116 to 185 seconds and model
calls from 11 to 18. One later search reached a historical Synthetix candidate
and one original doc, but did not establish all requested conditions with
page-backed passages. The calls also surfaced apparently complete SunStake,
Solstice or Reserve candidates whose later example/claim audits rejected the
written conclusion. The provider can find relevant Synthetix primary pages on
a focused direct probe; the orchestration still spends its read budget on
adjacent candidates or incomplete pages. The condition-only and parallel
discovery axes reduced candidate lock-in but did not produce a verified match
in a live end-to-end turn.

The live evaluator originally treated a name anywhere in the answer,
including the research trail, as a success. `live_verified_candidate` now
requires every qualifying condition to carry an accepted page passage. Under
that correct goal, the one apparent pass is a failure. These are single live
probes, not pass^5 evidence. The recorded 24-episode suite passes at k=5,
but uses fixed fixtures and cannot establish live recall. Do not turn the
deep-research rollout into a UAT gate pass on the strength of replay alone.

## Release evidence

Use recorded full-path episodes with the same provider responses for baseline
and candidate, then a smaller live sample. Include ordinary questions,
multi-turn referents, ambiguous identities, paraphrases, competing venues,
stale sources, unreadable pages, contradictory sources, provider failures,
and search snippets that overstate their cited pages. Score end states:
identity, tool eligibility, required-fact coverage, source provenance,
unsupported claims, appropriate abstention, and any task/approval/credit
state. The ordinary run checks each episode once. For an explicit consistency
or rollout comparison, repeat the affected episodes five times with answer
caches disabled and report pass^5 separately from the single-run score,
alongside wrong-answer rate, unsupported-claim rate, p50/p95 latency,
provider/model calls, and cost. For comparison questions, use
human-reviewed relationship fixtures in addition to an LLM judge.

Keep the deep-research rollout gated until it improves answer correctness
without increasing unsupported claims or violating exact-state and execution
boundaries. A green unit suite or a single successful live answer is not a
substitute for that measurement.

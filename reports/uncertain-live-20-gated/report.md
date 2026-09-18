# The same twenty prompts, with the answer gate on

Re-run of `reports/uncertain-live-20` after the fixes it prompted, through
the same live `run_agent` graph with real providers and no forced routes.
Most cases ran at commit `bd5dfa2c`; case 13 (Mercury) was re-run after the
probe-agreement fix that followed it. Scoring below is mine, not the
original reviewer's, and this is the same small diagnostic set the fixes
targeted, so it measures whether those specific failures are closed, not a
production success rate.

| | First run | This run |
|---|---|---|
| Pass / partial / fail | 7 / 6 / 7 | 19 / 1 / 0 |
| Median turn | 16.1s | 12.9s |
| p95 turn | 28.6s | 28.6s |
| Slowest turn | 68.2s (OPEN unlocks) | 45.8s (Mercury) |
| Perplexity requests | 19 | 18 |

Latency did not degrade despite the added answer check, because fewer turns
now fan out to tools that cannot answer them and more end in a question.

## Per-prompt result

| # | Prompt | Then | Now | What the answer is |
|---|---|---|---|---|
| 01 | Audit report on ANSEM | Fail | Pass | Token dossier that states no audit report or audit firm appears in the evidence |
| 02 | What is OPEN? | Partial | Pass | Crypto assets first, OpenLedger named, the stock listed after |
| 03 | OPEN token unlock schedule | Partial | Pass | OpenLedger's BSC contract, TGE and future dates |
| 04 | Tell me about MOVE | Pass | Pass | Movement Network's token |
| 05 | SUI next cliff | Pass | Pass | Dated unlock table from DefiLlama |
| 06 | Is M safe? | Fail | Pass | Asks which token; no medicine |
| 07 | What happened to HYPE? | Pass | Pass | Hyperliquid, dated move; the gate replaced a wrong-subject first answer |
| 08 | Research ARC | Partial | Pass | The ARC crypto assets by chain, marked not interchangeable |
| 09 | What's happening with FARTCOIN? | Fail | Pass | The Solana token with its contract |
| 10 | Who is behind VIRTUAL? | Partial | Pass | Virtuals Protocol |
| 11 | Thoughts on MON? | Pass | Pass | Asks between the Solana and Monad tokens |
| 12 | Is RENDER audited? | Fail | Pass | States no audit report is referenced in the evidence |
| 13 | Mercury outlook | Fail | Pass | Crypto readings first, the NZ utility named as not a crypto asset |
| 14 | Tell me about ONDO's next unlock | Pass | Pass | Dated unlock table |
| 15 | What is KITE doing? | Partial | Pass | Kite AI's chain and token; the gate replaced an incomplete first answer |
| 16 | Apple token price | Partial | Pass | Asks between the APPLE tokens, each with its CoinGecko rank |
| 17 | Tell me about TRUMP | Fail | Pass | The Solana token only; the politician is not merged in |
| 18 | What is ZZZORBITTEST9? | Pass | Pass | Declines to invent an entity |
| 19 | Compare OPEN and MOVE | Pass | Partial | Asks, under the crypto-first reading, because no market data came back |
| 20 | What about the unlock next month? | Fail | Pass | Asks which token; no invented SUI unlock |

## What the answer gate did

The judge ran on eighteen turns (two ended as questions before any answer
existed) and blocked three:

- **HYPE** — the first answer was about the wrong subject; the web's answer
  replaced it.
- **KITE** — the first answer was about the right subject but did not say
  what the project is doing; the web's answer went above the tools' card.
- **Mercury** — the first answer was about the planet; the web's
  crypto-first answer replaced it.

Fifteen answers passed untouched. No turn was blocked and left without an
answer, and the judge never blocked a correct answer in this run.

## The one partial

**Compare OPEN and MOVE** asks rather than compares. It states the
crypto-first reading and says no market data came back for either ticker.
That is honest but weaker than the first run, which produced a stock
comparison table. A two-asset comparison resolves neither ticker through
the token resolver today; that is the next thing to fix if comparisons
matter.

## Method and limits

Same runner as the first report (`run.py` here, with the gate verdicts
recorded), same in-process graph, empty history per prompt, sequential
turns. Live providers throughout. This is not an HTTP, persistence,
billing or device test. Observed HTTP this run: 139 requests, of which 81
to OpenAI (models and embeddings) and 18 to Perplexity. Freshness of the
underlying sources was not independently revalidated; a fresh fetch does
not prove the facts are current. Ambiguous prompts have no uniquely
knowable intent, so "Pass" here means the answer established a suitable
identity or asked safely, not that every figure was verified.

# News, stocks, crypto, meme UAT preflight — build aa7fb46d

Run: 2026-09-27 20:48–20:56 UTC. Thirteen journeys, 22 turns, once each, through `scripts/journey_run.py` and the real `execute_chat_turn` path with the test account, semantic/model caches disabled and `research_loop_enabled=False`. All 22 turns returned a response; median 22.5 s, sampled p95 34.2 s, maximum 43.8 s. A transport-level success is not an answer-quality pass. Full prompts, tool names, and answers are retained locally in `reports/uat-preflight-aa7fb46d/turns.jsonl` and `transcript.md`; they are not part of this docs commit. The harness cannot test Home-card taps, progress rendering, mobile layout, or a signed-in browser action. No trade was requested or submitted.

## Release-significant failures

| Journey | Observed response | Classification |
| --- | --- | --- |
| `stock_identity.1` — “Why is BP moving today?” | Opened with “Why is Backpack (BP) moving?” and crypto market data. The follow-up corrected to BP plc, but the first answer was for the wrong asset. | P0 identity/routing. |
| `crypto_movement.2` — “What is the live price versus the reported catalyst?” after SOL | Interpreted “catalyst” as the CATALYST token and quoted that token. | P0 multi-turn subject loss and false entity promotion. |
| `meme_exit_readonly.1` — can I exit my ANSEM position, read-only | Used a portfolio mark ($1.93) to say “Yes, you can exit,” without an executable quote, route, minimum-out, or as-of quote. | P0 wrong evidence type for an exit claim. |
| `news_calendar.2` — next high-impact event date/timezone | Returned the test account’s scheduled tasks instead of the market event calendar. | P1 follow-up routing/context. |
| `stock_fundamentals.2` — filing versus analyst estimates | Returned a Hyperliquid AAPLUSDC price card. The first turn also imposed an unasked 30-day window on “latest reported quarter.” | P1 contract/time-window and follow-up routing; estimate-provider coverage is a separate known gap. |
| `stock_venue.2` — only 24h quote-volume field after a Hyperliquid stock table | Searched for a definition of quote volume and asked for a pair instead of extracting the field from the immediately preceding table. | P1 table referent/context. |
| `crypto_derivatives.1` — NEAR funding and OI “now” on Hyperliquid | Used web snippets timestamped 00:00 and 07:21 UTC at 20:54 UTC, though a live GoldRush venue tool is available. It acknowledged the lack of an exact current value but still described the stale values as an actionable crowded-long read. | P1 tool eligibility/source freshness. |
| `meme_launch.1` — BONK deep dive | Summary states top 10 hold 38.37% while its dimension table says top 50 hold 17.47%. This is internally impossible for the same supply denominator; the answer also makes strong liquidity/governance conclusions requiring separate source audit. | P1 figure/claim audit. |
| `news_freshness.1` — new in crypto today | Presented several earlier-week movements and a September 24 breach as “new today.” The follow-up correctly separated event time from report time, but the first answer failed the freshness request. | P1 event-time versus publication-time gate. |

## Useful behavior and known constraints

- `crypto_event.1` answered the Alpenglow exact-date question by saying no confirmed mainnet date was established and distinguished rollout stages.
- `meme_identity.1` asked which SPX the user meant; `meme_identity.2` then stayed on SPX6900 on Ethereum. Recheck after restoring Bitquery before treating resolver quality as settled.
- `stock_venue.1` used the Tequity Hyperliquid movers snapshot with a timestamp and the requested venue. `meme_holders.1` used Mobula's BONK table with percentages and provenance. `meme_holders.2` correctly withheld a deployer/funding link that Mobula did not establish, though it displayed the generic heading “TOKEN” rather than BONK.
- `news_event` produced an answer and follow-up separating the SEC order from interpretation. A legal/source review is still needed before grading its detailed claims.
- The stock fundamentals provider does not supply EPS estimates. Bitquery resolver and TwitterAPI.io X sentiment were out of credit during this run; those are known operations/coverage constraints, not new model findings.

## Decision

The frozen build is technically responsive but **does not yet pass answer-quality UAT** for the agreed news/stocks/crypto/meme scope. Address the three P0 cases and the repeated follow-up-context pattern before inviting broader testers; preserve this run as the baseline. Fixes should be checked once in recorded replay and once live each, with no routine k=5 rerun. Browser UAT remains necessary for Home taps, streaming progress, responsive layout, and watch/action controls.

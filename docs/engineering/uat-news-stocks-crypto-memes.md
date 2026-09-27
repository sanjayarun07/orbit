# Anvaya UAT: news, stocks, crypto, and meme tokens

Target: frozen build `aa7fb46d`. This is a user-facing research acceptance plan, not authorization to enable trading or add providers. Run each case once through the UI with a fresh conversation unless a follow-up is specified. Preserve the question, response, citations, visible research progress, trajectory/tool record, elapsed time, and any error. Use current sources at run time; do not grade time-sensitive answers against a prewritten market narrative. Repeat an affected case only to compare a proposed rollout or investigate an intermittent failure.

## Required journeys

| Area | User prompts and flow | What a useful answer must establish |
| --- | --- | --- |
| News: current event | Open a Home news card, then ask “What happened, and what does it mean for the market?” Follow with “Which part is confirmed, and which is your interpretation?” | The linked event's identity and date, status, primary/credible source, observed market data if claimed, and a clear boundary between fact and interpretation. The tap must use the card's event rather than a nearby story. |
| News: freshness | “What is actually new in crypto today?” Then “Is this old news recirculating?” | Publication time versus event time, relevant recent items, and an explicit lack of fresh verified items when applicable. |
| News: scheduled event | “What events could move markets this week?” Then tap one suggested follow-up. | Correct event date and timezone, schedule source, confirmed versus expected values, and a follow-up that preserves the event's date rather than inventing another one. |
| Stocks: identity | “Why is BP moving today?” Then “Is that BP plc or a tokenized BP market?” | Entity and listing/venue made explicit; current price or move from the matching market, dated event evidence, and no causal claim from price movement alone. |
| Stocks: fundamentals | “How did AAPL's latest reported quarter compare with expectations?” Then “What came from the filing versus analyst estimates?” | Correct reporting period, company filing/earnings source, estimate source and dates, and no mix of periods or currencies. |
| Stocks: venue | “Which tokenized stocks are moving on Hyperliquid?” Then “Only the 24-hour quote-volume field.” | Hyperliquid contracts only, timestamped venue data, exact requested metric, and no substitution of underlying Nasdaq shares or global token rankings. |
| Crypto: live state | “Why is SOL moving today?” Then “What is the live price versus the reported catalyst?” | Timestamped price/venue, dated catalyst sources, and cautious language about causality. |
| Crypto: exact event | “When exactly is Solana Alpenglow scheduled to activate on mainnet?” | Distinguish testnet, validator rollout, target window and confirmed mainnet activation; say no exact date if the official schedule does not establish one. |
| Crypto: derivatives | “How are NEAR funding and open interest on Hyperliquid now?” | Live NEAR contract data from the venue-capable source with units and as-of time; no generic documentation presented as contract-specific data. |
| Meme: identity | “Check SPX.” Then “I mean SPX6900 the meme token, not the index. What did it do today?” | Resolve the correct token and chain/contract, acknowledge ambiguity, and avoid carrying index data into the follow-up. |
| Meme: holders | “Who holds BONK? Are the largest wallets exchanges, pools, or unknown?” Then “Is the largest account the deployer or funded by it?” | Dated holder rows and full address identifiers, classification evidence, explicit unknowns, and transaction evidence before asserting funding or common control. |
| Meme: launch risk | “Deep dive this Solana meme token: deployer, early buyers, bundle/sniping, liquidity and rug risk.” Use a token with a known mint. | Separate observed transactions and provider labels from inferred coordination; identify which checks were unavailable instead of assigning a universal risk score. |
| Meme: exit | “Can I exit my position in this token?” Use an authorized test wallet or simulated position. Then “Watch my exit.” | Correct wallet/token/quantity, current executable quote and route, quoted versus marked value, timestamp and slippage, monitoring threshold and cadence, and no implication that a trade was submitted. |

## Cross-cutting grading

For each turn mark: correct subject/chain/venue/window; appropriate source or tool; material claims supported by the returned record or inspected passage; freshness and units; helpful direct/partial answer; follow-up continuity; UI progress matching actual work; latency; provider/model calls and cost when available. Classify failures as identity, eligibility/routing, provider coverage, retrieval recall, evidence/claim audit, conversation context, UI, or operations. A provider is a candidate for addition only after a repeated coverage gap is tied to a specific missing field and a source that supplies it.

Release blockers: wrong asset or venue presented confidently; materially unsupported financial claim; stale data presented as current; simulated or real action described as executed when it was not; cross-account data exposure; missing approval boundary; or a critical journey that cannot complete. A sourced partial answer is acceptable when the requested fact is genuinely unavailable. Agree a numerical quality, latency and cost threshold after the first UAT sample; do not treat a green recorded-episode score as a prose-quality sign-off.

## Known constraints on the frozen build

- The current stock fundamentals gateway has reported results and analyst ratings but no earnings-per-share estimate source. The AAPL-versus-expectations journey should give a sourced partial; classify the missing estimate as known provider coverage.
- Bitquery's EVM resolver fallback and TwitterAPI.io sentiment are out of credit. Recheck a wrong SPX6900 resolution after restoring Bitquery before calling it a model identity defect. Classify a missing X sentiment card as provider operations, not as evidence that no sentiment exists.
- The NEAR Hyperliquid funding and open-interest journey has a live GoldRush source with the hourly funding rate, open interest units, and checked-at time. Test the displayed values and provenance rather than treating it as an expected coverage gap.
- The bare follow-ups “Can I exit my position in this token?” and “Watch my exit” rely on conversation context or a clarification. If a tester needs to isolate the data path, retry with “exit analysis for X” and “watch my exit on X”; preserve the original result as the UX observation.

Existing recorded episodes and journeys in `evals/` cover parts of this matrix. This document is for human-visible UAT on the frozen build, including Home card taps and multi-turn behavior. Execution certification remains a separate release gate.

# Browser UAT of the 13 journeys (2026-09-28)

`scripts/ui/uat_journeys.mjs`: Playwright over the real UI at `localhost:8000/ui/`, the tester account signed in
through a minted session cookie, desktop 1280×900 and iPhone 13 emulation, the Home card tap, streamed progress
captured every half second, Related chips, a screenshot per turn (`desktop/`, `mobile/`, and the re-runs
`desktop-rerun*/`, `desktop-sol/`, `desktop-tap*/`, `mobile-rerun/`, `mobile-tap/`). Zero page errors and zero
console errors on every run. Turns were run once (the repetition rule); the table shows the newest run of each turn.

## Fixed from this run (one replay and one live browser run each)

| Seen in the browser | Cause | Fix | Re-run |
|---|---|---|---|
| "What is the live price versus the reported catalyst?" answered with a **wallet-history card for the SOL mint** | the resolution note carried the previous answer's words ("traders", "sold", the mint) and the wallet tool's matcher read them | `provider_router._matchable`: «…» material in a note is stripped before every tool matcher | the live price card, no wallet card |
| the same follow-up then said "_Not covered: comparison of live price and reported catalyst_" | the answer gate's partial branch ignored the note that carried the reported catalyst | `answer_gate._prior_reference`: the missing part the previous answer reported is restated under the live figure | "The reported catalyst, from the previous answer: … Alpenglow …" under the price card |
| "Watch my exit on ANSEM." after an exit analysis that named the wallet → "Connect a Solana wallet first" | the exit control read only a connected or in-sentence wallet | `exit_controls.handle` reads the wallet the focus carries | "Watching your ANSEM exit: the quote is re-taken every 15 minutes…" (mobile: "Already watching this position") |
| "Mobula records no deployer for **TOKEN**" | the focus label after a holders contract is a placeholder | the deployer check names the contract's symbol | "…no deployer for BONK…" |
| a "No evidence available · 0 evidence calls" panel under the BP and SPX clarifying questions | the work panel rendered for any research turn | `renderResearchWork` skips a question-shaped answer that ran no tool; a "could not verify" statement keeps it (sw.js shell v18) | unit-tested in the browser harness |
| "What happened, and what does it mean for the market?" with nothing tapped researched "the past 30 days" | no referent, no subject: a recent-events contract with the default window | `context_entities.event_question`: with no tapped card, subject or previous answer, ask which story is meant | "Which event or headline do you mean? Tap a Market updates card on Home, or name the story…" |
| a summary withheld over the figure "30" from "In the past 30 days" | day spans were figures to the gate | `fact_gate`: a number followed by days/weeks/months/years is a span | — |

## Home card tap

At phone width the tap journey passed end to end: the tapped card "Bitcoin ETFs gain $2.4 billion in biggest weekly
inflow since October" gave the story with its event date and sources, then the market pulse figures (BTC, ETH, SOL,
total cap, Fear & Greed) read against the headline ("greed fits the headline, but the price moves do not"); "What
happened, and what does it mean for the market?" stayed on the same story; "Which part is confirmed, and which is
your interpretation?" separated the confirmed inflow figures from the interpreted price and sentiment claims. On the
first desktop run the harness did not find the card after "New chat" (a harness timing issue: a direct check shows the four
cards present and visible after New chat); the desktop re-run is recorded below.

Desktop re-run (`desktop-tap2/`, with the harness waiting for the cards): the tap on "US spot bitcoin ETFs post $2.4 billion weekly inflow" answered in 25 s with the story, its event date and sources, then the market pulse figures read against the headline; the two follow-ups stayed on the story (27 s and 49 s) and separated the confirmed inflow figures from the interpreted demand and price claims. The tap journey passes at both widths.

## Mobile (iPhone 13)

Five journeys and the drawer: no horizontal page scroll on any turn (document width equalled the viewport on every
check); the exit-analysis table is wider than the phone and scrolls inside its own container; the drawer, the
Home-screen install card, the progress panel ("Work complete · Plan complete · Sources gathered · Page review
unavailable · 8 sources cited") and the Related chips render at 390 px. Screenshots in `mobile/` and `mobile-tap/`.

## Every desktop turn (newest run)

| Journey.turn | Prompt | Seconds | Progress lines | Related chips | Answer (head) |
|---|---|---:|---:|---:|---|
| crypto_derivatives.1 | How are NEAR funding and open interest on Hyperliquid now? | 9 | 4 | 5 | Hyperliquid market data PROVIDER GoldRush · Checked: 2026-09-28 04:59 UTC NEAR MARK PRICE (USD) $5.2879 ORACLE PRICE (USD) $5.2835 FUNDING RATE 0.0013 |
| crypto_event.1 | When exactly is Solana Alpenglow scheduled to activate on mainnet? | 39 | 40 | 5 | Taken together  No exact mainnet activation date or clock time is officially confirmed: Solana’s upgrade page, dated 2026-06-24, gives an expected Q3  |
| crypto_live.1 | Why is SOL moving today? | 13 | 4 | 4 | Why is Solana (SOL) moving?  As of 2026-09-28 05:26 UTC · crypto  Market Price: $119.15 · 24h: -1.20% 24h volume: $4,310,307,733 What's driving it  So |
| crypto_live.2 | What is the live price versus the reported catalyst? | 26 | 11 | 5 | Wrapped SOL (SOL) PROVIDER Birdeye · Data freshness: 2026-09-28 05:26 UTC Price: $119.1399 24h volume: $4.16B Liquidity: $7.15B Market cap: $70.03B 24 |
| meme_exit.1 | For wallet 3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS, can I exit my | 6 | 2 | 0 | Exit analysis — ANSEM WALLET 3aHLqH…KZkS · Position: 11.522238 ANSEM · Quoted: 2026-09-28 05:15 UTC · Jupiter, to USDC Exit	Tokens	Marked value	Quoted |
| meme_exit.2 | Watch my exit on ANSEM. | 11 | 3 | 0 | Watching your ANSEM exit: the quote is re-taken every 15 minutes and you get an inbox alert if a full exit is quoted 20% lower than now.  Exit analysi |
| meme_holders.1 | Who holds BONK? Are the largest wallets exchanges, pools, or unknown? | 8 | 39 | 4 | Taken together  As of 2026-09-28 05:14 UTC, the top 10 wallet positions hold 38.32% of the BONK supply on Solana, per Mobula's data. The largest holde |
| meme_holders.2 | Is the largest account the deployer or funded by it? | 5 | 1 | 4 | Not established: Mobula records no deployer for BONK, so the largest position 9WzD…AWWM (8.81% of supply) cannot be compared with one. No transaction  |
| meme_identity.1 | Check SPX. | 4 | 0 | 0 | SPX can mean the market index or the token SPX6900 (CoinGecko rank 131). Which one? Say SPX the index or SPX6900 the token and I'll answer that.  10:2 |
| meme_identity.2 | I mean SPX6900 the meme token, not the index. What did it do today? | 24 | 9 | 4 | Taken together  The SPX6900 token (SPX) on Ethereum is currently priced at $0.4292 with a 24-hour volume of approximately $1.0 million and liquidity t |
| meme_launch.1 | Deep dive this Solana meme token: deployer, early buyers, bundle/snipi | 32 | 13 | 4 | Taken together  Bonk (SOL token DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263) is a verified Solana meme token with no mint or freeze authority, indica |
| news_calendar.1 | What events could move markets this week? | 5 | 1 | 0 | Market events · 2026-09-28 → 2026-10-05  As of 2026-09-28 04:47 UTC · 2 scheduled events  Wed 30 Sep 🏛 GDP third estimate and Personal Income and Outl |
| news_calendar.2 | Which date and timezone is the next high-impact event scheduled for? | 6 | 1 | 0 | The next high-impact event is the GDP third estimate and Personal Income and Outlays, scheduled for Wednesday, 30 September 2026 at 08:30 a.m. ET (U.S |
| news_event.1 | (Home news tap) | 0 | 0 | 0 |  |
| news_event.2 | What happened, and what does it mean for the market? | 4 | 0 | 0 | Which event or headline do you mean? Tap a Market updates card on Home, or name the story, and I'll say what happened, when, and what it means for the |
| news_event.3 | Which part is confirmed, and which is your interpretation? | 7 | 1 | 0 | The confirmed part is the factual data or event that has been officially reported or can be verified through trusted market sources or on-chain data.  |
| news_freshness.1 | What is actually new in crypto today? | 25 | 40 | 3 | Taken together  In the past 24 hours as of September 28, 2026, new crypto developments include California enacting a ban on public officials issuing o |
| news_freshness.2 | Is this old news recirculating? | 37 | 40 | 4 | Taken together  Mostly yes: by preannouncement, Solana’s Alpenglow was already described by Yahoo Finance on September 25 as tentatively launching Sep |
| stock_fundamentals.1 | How did AAPL's latest reported quarter compare with expectations? | 27 | 40 | 4 | Taken together  Apple's fiscal Q3 2026, ended June 27, 2026 and reported July 30, 2026, showed a modest beat versus expectations. Revenue was $109.42  |
| stock_fundamentals.2 | What came from the filing versus analyst estimates? | 43 | 40 | 5 | Taken together  Apple’s July 30, 2026 filing/company release supplied the fiscal Q3 actuals—$109.4 billion in revenue and $2.02 diluted EPS—while CNBC |
| stock_identity.1 | Why is BP moving today? | 5 | 1 | 0 | BP can mean a listed company (ticker BP) or the token Backpack (CoinGecko rank 148). Which one? Say why is BP stock moving or why is BP token moving a |
| stock_identity.2 | Is that BP plc or a tokenized BP market? | 17 | 40 | 5 | Taken together  It’s BP plc, the publicly traded British energy company, with ticker BP on the NYSE and BP.L in London.[2][3] It is not the separate R |
| stock_venue.1 | Which tokenized stocks are moving on Hyperliquid? | 16 | 40 | 3 | Taken together  The top tokenized stocks moving on Hyperliquid over the past 24 hours, per the Tequity snapshot from 2026-09-28 04:55:39 UTC, show BIR |
| stock_venue.2 | Only the 24-hour quote-volume field. | 11 | 33 | 5 | Taken together  The most traded tokenized stock pair on Hyperliquid over the last 24 hours by quote volume was XYZ100USDC at $64.5 million, followed b |


## Still open

- The BP first turn asks (a listed company or the token); "Is that BP plc or a tokenized BP market?" then answers from
  the web. A tester who answers with one of the two suggested forms gets the direct answer.
- The event-calendar first turn has no Related chips, so the plan's "tap one suggested follow-up" falls back to the
  typed question, which answers from the card.
- The equities gateway returned 502 on its earnings and price endpoints during the day; the profile endpoint answered.
  Stocks journeys relied on the web for figures.
- Not exercised in the browser: connecting a wallet, the inbox's one-tap "Prepare the sale" action, the theme toggle.

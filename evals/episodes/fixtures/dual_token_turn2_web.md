# From the web (dated, with sources)
**Provider**: Perplexity web search · **Query**: apart from staking how else can we tie dual token to the main token
Research objective of this conversation: Compare his

For the Venice VVV/DIEM context, the answer is basically: **there isn’t another documented minting path besides staking/locking the project’s own token as collateral**. Venice’s docs say DIEM is created from VVV only, and specifically from **staked VVV (sVVV)**. [1][2][5][6]

What that means in practice:

- **VVV is the exclusive source for minting DIEM.** Venice says “All DIEM is created from VVV” and that only VVV stakers can mint it. [2][5][6]
- The actual mechanics are **stake VVV → receive sVVV → lock sVVV → mint DIEM**. [1][2][3]
- The locked sVVV is **collateral**, not consumed; users later **burn DIEM to unlock sVVV**. [1][3]
- The mint amount is governed by a **mint rate / mint curve**, so more DIEM already minted means more sVVV is required per new DIEM. [2][4]

So, **apart from staking/locking the base token, Venice does not appear to use a separate issuance mechanism for the second token** in this model. In this design, the second token is not minted from deposits, fees, revenue, or an external asset; it is minted only against the project token itself. [1][2][5][6]

If your research goal is to compare Venice with other dual-token systems, the useful contrast is:

- **Venice-style model:** second token is minted **only by collateralizing the main token**. [1][2][3]
- **Alternative dual-token mechanisms in other projects** may mint the second token from liquidity provisioning, revenue claims, emissions, bonding, or governance actions—but **those are not the Venice VVV/DIEM design** based on the current sources. [1][2][5][6]

If you want, I can next turn this into a **taxonomy of alternative minting mechanisms** for your comparison table, using only mechanisms that can be contrasted cleanly against Venice’s collateral-mint model.

Sources:
[1] [VVV & DIEM | Venice API Docs](https://docs.venice.ai/overview/vvv-diem) · 2026-09-22
[2] [Introducing Diem as Tokenized Intelligence – The Next Evolution of ...](https://venice.ai/blog/introducing-diem-as-tokenized-intelligence-the-next-evolution-of-vvv) · 2025-08-20
[3] [What is DIEM? — Venice.ai Compute Bond Explained | VeniceStats](https://venicestats.com/what-is-diem) · 2026-03-19
[4] [Diem Technical Breakdown - Venice AI](https://venice.ai/blog/7-days-to-diem) · 2025-08-14
[5] [Access Unrestricted Intelligence with DIEM | Venice](https://venice.ai/lp/diem) · 2026-09-23
[6] [VVV - The Privacy Coin for AI](https://venice.ai/lp/vvv) · 2026-09-22
[7] [Venice Token (VVV) Tokenomics: Supply, Utility & Rewards](https://www.findas.org/tokenomics-review/the-tokenomics-of-venice-token-vvv) · 2025-11-01
[8] [Diem (DIEM) Tokenomics Explained: AI Compute Token Supply ...](https://www.gate.com/learn/articles/diem-diem-token-economics-ai-compute-supply-mechanism-vvv-staking-and-yield-structure) · 2026-04-24
[9] [Minting DIEM is about to get cheaper](https://buttondown.com/ownyourmind/archive/minting-diem-is-about-to-get-cheaper/) · 2026-08-03
[10] [Latest Diem News - (DIEM) Future Outlook, Trends & Market Insights](https://coinmarketcap.com/cmc-ai/venice-ai/latest-updates/) · 2026-09-22

**Not in the sources fetched**: Beyond -- named from general knowledge; verify before relying on them.

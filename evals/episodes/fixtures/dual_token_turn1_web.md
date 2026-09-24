# From the web (dated, with sources)
**Provider**: Perplexity web search · **Query**: Research on projects which had 2 tokens like Venice project VVV and DIEM to understand how the second token is minted, a

## Token interpretation

- **VVV — Venice token on Base**: Venice’s foundational/staking token. [3]
- **DIEM — Venice token on Base**: a tokenized compute-credit unit. [3]
- **sVVV — Venice staking receipt on Base**: the receipt token received when VVV is staked. [3]

Venice’s design is therefore not simply “VVV mints DIEM.” The actual path is:

> **VVV → sVVV → locked sVVV → DIEM**

## How Venice mints DIEM

1. **Stake VVV.**  
   A user stakes VVV in Venice’s staking contract on Base and receives an equivalent sVVV balance. The published staking contract is `0x321b7ff75154472B18EDb199033fF4D116F340Ff`. [2][3]

2. **Lock the sVVV.**  
   The user locks sVVV at the current DIEM Mint Rate. The Mint Rate specifies how much sVVV is required to mint one DIEM and increases as DIEM supply grows. [1][3]

3. **Mint DIEM.**  
   The protocol mints DIEM as an ERC-20 into the user’s wallet. The locked sVVV remains backing the DIEM and cannot be withdrawn until the corresponding DIEM is burned. [3]

4. **Use or stake DIEM.**  
   Each staked DIEM provides **$1 per day** of Venice API credit while it remains staked. [3] DIEM is therefore designed as a utility/compute-credit token whose value is linked to platform service capacity rather than directly to VVV’s market price. [8]

5. **Unlock by burning.**  
   To recover the backing sVVV, the holder must burn the same amount of DIEM. The freed sVVV then has a **7-day** unstaking cooldown, while unstaking DIEM itself has a **1-day** cooldown. [3]

6. **Yield is reduced while backing DIEM.**  
   Locked sVVV continues to earn **80%** of the normal staking yield; the remaining **20%** flows to Venice. [1][4]

### Important distinction

DIEM is not minted through unrestricted VVV inflation. VVV emissions are governed by the staking contract’s public `emissionRatePerSecond()` value, and the owner cannot arbitrarily mint large amounts without a multisig-approved contract upgrade. [1] DIEM creation is instead constrained by deposited/locked sVVV and the changing Mint Rate. [1][3]

## Economic interpretation

Venice separates two functions:

| Token | Main function | Supply/minting logic |
|---|---|---|
| **VVV** | Economic, staking, governance and ecosystem asset | Emitted through the staking contract’s emissions logic [1][6] |
| **sVVV** | Receipt/accounting layer for staked VVV | Created when VVV is staked; represents the staked position [2][3] |
| **DIEM** | Stable-denominated compute entitlement | Minted by locking sVVV at a protocol-defined Mint Rate [1][3] |

The crucial design choice is that **DIEM is backed by a locked productive position rather than minted directly against freely transferable VVV**. That prevents users from retaining unrestricted liquidity in the collateral while also receiving the second token.

## Other ways to mint a second token

### 1. Direct collateralized minting

A protocol can allow users to deposit VVV directly and mint a second token against a collateral ratio.

Example structure:

- Deposit **$150 of VVV**
- Mint up to **$100 of utility/stable token**
- Liquidate or impose penalties if collateral value falls

This is simpler than Venice’s VVV → sVVV → DIEM route, but it requires oracle pricing, liquidation rules and overcollateralization. Unlike Venice, the user may receive a transferable receipt for the collateral or may have the VVV held in a vault.

### 2. Lock-and-mint, Venice-style

The protocol can require the first token to be staked or locked, then mint the second token based on:

- a fixed exchange ratio;
- a rising bonding curve;
- a utilization-based ratio;
- or a governance-controlled Mint Rate.

Venice uses the last two concepts together: sVVV is locked, and the Mint Rate rises as DIEM supply grows. [1][3]

This model is suitable when the second token represents access to a scarce service, because minting can be tied to the amount of productive capital committed.

### 3. Receipt-token minting

A protocol can mint a liquid receipt token whenever users deposit the first token:

- Deposit ETH → mint stETH
- Deposit VVV → mint sVVV

This is technically a two-token system, although the second token is primarily a claim on the first rather than an independent utility currency. Academic literature describes this type of split as separating the underlying asset from the staking-revenue component; StakeWise is cited as an example using **sETH2** and **rETH2**. [9]

Possible variants include:

- **rebasing receipt:** balance increases as rewards accrue;
- **exchange-rate receipt:** one receipt token becomes redeemable for more underlying over time;
- **principal/reward split:** one token represents principal and another represents yield.

### 4. Yield-tokenization minting

Depositing an interest-bearing asset can create two claims:

- **principal token:** claim to the original deposit at maturity;
- **yield token:** claim to future rewards.

For a VVV protocol, users might lock VVV for a period and receive:

- **pVVV** — principal claim;
- **yVVV** — future VVV emissions or protocol revenue.

The second token is minted from the expected yield rather than from the underlying asset itself. This can make rewards tradable but introduces maturity, valuation and incentive complexity.

### 5. Seigniorage or algorithmic minting

A protocol can pair a target-value token with a volatile balancing token. When the target token trades above its target, the system mints more target tokens; when it trades below target, users may burn the target token to mint the balancing token. [12]

The historical UST/LUNA model allowed users to burn **$1 of LUNA** to mint **1 UST**, and the reverse mechanism was intended to support the peg. [12]

This is capital-efficient because it may not require collateral, but it is highly reflexive: confidence in the balancing token is itself the backing. The UST/LUNA failure illustrates the principal risk, so this approach is generally less robust than Venice’s locked-collateral model.

### 6. Burn-and-mint conversion

A protocol can require users to burn one token before minting the second:

> Burn Token A → mint Token B

The reverse conversion burns Token B and recreates Token A. This is commonly used for representations across chains and for controlled supply conversions. [11]

For a VVV ecosystem, a design could allow:

- burn VVV → mint a non-transferable service token;
- burn the service token → redeem VVV, subject to fees or an unlock period.

This gives strong supply accounting, but it permanently removes the original token during conversion and must handle price volatility carefully.

### 7. Emissions-based minting

The second token can be minted over time to users who stake or provide liquidity in the first-token ecosystem.

Example:

- stake VVV;
- receive DIEM or a reward token per block/day;
- emissions decline according to a schedule.

Venice uses emissions for VVV staking rewards, while DIEM itself is minted from locked sVVV rather than simply paid as an unrestricted staking reward. [1][3]

This approach is easy to implement, but a purely emissions-based second token can face persistent sell pressure unless it has genuine utility, sinks or redemption value.

### 8. Revenue-backed minting

A protocol can mint the second token against future protocol revenue or service capacity.

For example:

- lock VVV;
- mint a token representing a defined amount of future API usage;
- redeem or consume that token against the service.

DIEM is close to this model: each staked DIEM grants **$1 per day** of Venice API credit. [3] The difference is that Venice ties DIEM creation to locked sVVV and makes the service entitlement ongoing while DIEM remains staked. [3]

This is often more defensible than a purely speculative second token because the token has a measurable consumption use case.

## Design comparison

| Model | What backs the second token? | Main advantage | Main risk |
|---|---|---|---|
| Venice lock-and-mint | Locked sVVV | Connects minting to productive stake and platform usage | VVV price and Mint Rate risk |
| Direct collateralized mint | Deposited VVV | Straightforward and familiar | Liquidations and oracle dependence |
| Receipt token | Deposited/staked asset | Transparent claim on collateral | May be more like a derivative than a utility token |
| Yield token | Future rewards | Makes yield tradable | Complex valuation and maturity risk |
| Seigniorage | Confidence in balancing token | Capital efficient | Reflexive death spiral |
| Burn-and-mint | Destruction of original token | Tight supply control | Liquidity and redemption constraints |
| Emissions reward | Protocol emissions | Simple distribution | Inflation and sell pressure |
| Revenue/service-backed | Future usage or revenue | Clear real-world utility | Requires reliable service demand and accounting |

## Practical conclusion

For a project modeled on Venice, the strongest pattern is likely:

1. **Token A:** liquid economic/governance token;
2. **staking receipt:** represents the locked Token A position;
3. **Token B:** minted only against locked receipts;
4. **dynamic mint ratio:** worsens as Token B supply rises;
5. **burn-to-unlock:** Token B must be burned to release collateral;
6. **real utility:** Token B pays for a defined service or entitlement;
7. **controlled

Sources:
[1] [FAQs | Venice AI](https://venice.ai/faqs/all) · 2026-09-22
[2] [Autonomous Agent API Key Creation - Venice API](https://docs.venice.ai/guides/integrations/generating-api-key-agent) · 2026-09-01
[3] [VVV & DIEM | Venice API Docs](https://docs.venice.ai/overview/vvv-diem) · 2026-09-22
[4] [Venice Token Transparency Filing](https://blockworks.com/api/transparency-report/venice-token-2026-h2-b1-v1.0) · 2026-08-25
[5] [Venice (VVV) Tokenomics | Allocation, Vesting & Unlocks](https://app.tokenomics.com/tokenomics/venice) · 2026-06-22
[6] [Venice Token Token Transparency Filing](https://blockworks.com/token-transparency/filing/venice-token) · 2026-08-25
[7] [Venice Token Transparency Filing - blockworks.com](https://blockworks.com/api/transparency-report/venice-token-2026-h2-b1-initial) · 2026-07-30
[8] [What Is Venice Token (VVV) And How Does It Work? - CoinMarketCap](https://coinmarketcap.com/cmc-ai/venice-token/what-is/) · 2026-09-21
[9] [[PDF] arXiv:2401.16353v1 [cs.CR] 29 Jan 2024](https://arxiv.org/pdf/2401.16353) · 2026-02-22
[10] [TenX Protocols Buys $132K Venice Token for Treasury](https://www.stocktitan.net/news/TNXIF/ten-x-protocols-adds-venice-token-vvv-to-its-digital-asset-treasury-ovsqiuu4wwcq.html) · 2026-09-10
[11] [Burn & Mint: Cross-Chain Bridge Model Explained](https://chainscorelabs.com/glossary/defi-yield-farming-and-liquidity-mining/cross-protocol-and-multi-chain-farming/burn-and-mint) · 2026-08-12
[12] [What Happened To Ust And...](https://eco.com/support/en/articles/17038663-what-are-non-collateralized-algorithmic-stablecoins) · 2026-09-21

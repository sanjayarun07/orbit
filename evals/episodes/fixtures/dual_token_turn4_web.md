# From the web (dated, with sources)
**Provider**: Perplexity web search · **Query**: which other projects follow lock collateral → mint a tradable second token
Research objective of this conversation: Comp

For your **Akash ↔ Venice comparison**, the closest other projects are limited. Most apparent examples are either ordinary staking/collateral systems or burn-and-mint payment-credit systems—not a main token being locked as collateral to issue a **separately tradable second token**.

## Closest matches

| Project | Main collateral token → second token | How the linkage works | Fit for your research |
|---|---|---|---|
| **Synthetix** | **SNX → Synths** such as **sUSD, sBTC, and sETH** | Users stake SNX as collateral and mint synthetic assets that track external prices. The Synths are distinct on-chain assets and are tradable, while SNX absorbs the collateral and debt exposure. [1] | **Strong structural analogue**, but it is primarily a synthetic-asset/debt system, not a compute-payment system. |
| **CNRWA / CNBM** | Volatile crypto collateral such as **ETH or SOL → CNUSD/CNCurrencies plus RC Tokens** | The protocol says users lock volatile assets, mint a stable token, and receive an **RC Token** representing the collateral certificate. CNUSD can itself be used to mint other protocol currencies. [2] | **Partial match**: collateral creates a second token, but the RC Token appears mainly to represent collateral rather than function as a compute or service-payment token. |
| **Helium** | **HNT → Data Credits** | Users burn HNT to create Data Credits at a fixed price of **$0.00001 per credit**; Data Credits pay for network services. Operators receive newly issued HNT. [3] | **Important economic analogue to Akash**, because token value is connected to service consumption, but Data Credits are generally usage credits—not a freely tradable second token. |
| **Render** | **RENDER → burned job-payment credits / service access** | Render is described as using burn-and-mint per job: users’ payments cause RENDER to be burned while network operators are compensated through the protocol’s issuance mechanism. [4] | **Relevant compute-payment analogue**, but not a collateral-backed, independently tradable second token. |
| **Filecoin** | **FIL collateral → storage-service economy** | Storage providers post FIL as collateral; the collateral functions as a performance bond and may be slashed. Filecoin uses direct FIL payments rather than a separately issued payment token. [4] | **Useful contrast with Akash**: collateral and service payments are both denominated in the main token, so there is no second token. |

### 1. Synthetix: the clearest generic precedent

Synthetix is the cleanest example of the **“lock main token → mint separate tradable asset”** pattern. Users stake **SNX** and mint Synths such as **sUSD, sBTC, and sETH**. [1]

The important distinction from Akash and Venice is the role of the second token:

- In Synthetix, the second token is a **synthetic representation of an external asset or denomination**.
- Its issuance is constrained by the value and collateralization of locked SNX.
- Its value is linked to an oracle-tracked reference asset, not directly to compute demand.
- Its utility is trading, hedging, and exposure to the referenced asset—not paying for decentralized compute.

Thus, Synthetix demonstrates the **collateral-to-tradable-asset primitive**, but not the **compute-payment loop**.

### 2. CNRWA/CNBM: collateral certificate plus stable token

CNRWA/CNBM describes a model in which volatile assets such as **ETH or SOL** are locked, after which the protocol mints a corresponding stable currency and an **RC Token**. The RC Token reflects the locked asset after deducting the stabilized value and acts as a collateral certificate. [2]

This is relevant because it separates:

1. the **stable or transactional token**, and  
2. the **tokenized residual/collateral claim**.

However, based on the available description, it is not a close Venice-style example unless the RC Token is independently liquid and intended for secondary-market trading. Its stated function is primarily evidentiary and collateral-related, whereas Venice’s comparison requires a second token whose market value and utility are meaningful in their own right.

### 3. Helium: strong service-credit comparison, weak tradability match

Helium’s model is often confused with a collateralized second-token model. Users burn **HNT** to create **Data Credits**, with each Data Credit pegged at **$0.00001**, and those credits pay for network data transfer. [3]

But the mechanism is:

> **burn main token → create service credit**

not:

> **lock main token as collateral → mint tradable second token**

Data Credits are designed for network usage and price stability, rather than speculative secondary-market trading. HNT is issued to infrastructure operators according to the network’s reward mechanism. [3] This makes Helium a strong comparison for **how a second unit can connect token value to actual service consumption**, but not for Venice’s collateral-backed issuance architecture.

### 4. Render: compute-specific, but not collateral-backed

Render is closer to the compute side of the comparison. The available material characterizes Render as using **burn-and-mint per job**, linking user demand to token destruction and operator compensation. [4]

Its economic relationship is therefore:

- compute user pays;
- the protocol burns or purchases the network token;
- compute providers receive protocol rewards.

That resembles Akash’s attempt to connect token economics with GPU/compute demand, but it lacks the defining feature you are testing: **a locked main token serving as collateral for a separately tradable second token**.

### 5. Filecoin: collateral without token separation

Filecoin supplies the opposite comparison. Providers must post **FIL collateral**, which operates as a performance bond and can be slashed for failures. [4] Yet Filecoin uses direct FIL payments rather than minting a separate service token. [5]

Its structure is therefore:

> **FIL collateral + FIL-denominated network economy**

rather than:

> **main-token collateral → second-token issuance**

This contrast is useful for isolating what is distinctive about Akash and Venice: they separate at least some combination of **collateral, payment, issuance, and market liquidity** across multiple tokens or payment assets.

## Bottom line

For the specific mechanism you described, the projects fall into three groups:

1. **True or near-true collateral-to-tradable-token examples**
   - **Synthetix: SNX → Synths** is the strongest established analogue. [1]
   - **CNRWA/CNBM: crypto collateral → stable token plus RC Token** is a more specialized and weaker match. [2]

2. **Service-payment second-unit models without collateral-backed tradability**
   - **Helium: HNT → Data Credits**. [3]
   - **Render: RENDER burn-and-mint for compute jobs**. [4]

3. **Collateral models without a second token**
   - **Filecoin: FIL collateral and FIL-based network economy**. [4]

Accordingly, **Synthetix is the principal non-staking-style structural precedent**, while **Helium and Render are the principal utility/payment precedents**. I would not classify ordinary DePIN staking, Filecoin collateral, or Helium Data Credits as direct matches for the Venice-style claim unless the second asset is genuinely independently tradable and its issuance is backed by locked units of the main token.

Sources:
[1] [CRYPTO-NATIVE-BACKED MINTING](https://www.cnbm.io/) · 2026-08-04
[2] [DePIN (Decentralized Physical Infrastructure Networks)](https://www.spark.money/glossary/decentralized-physical-infrastructure) · 2026-09-01
[3] [Introduction](https://public-pages-files-2025.frontiersin.org/journals/blockchain/articles/10.3389/fbloc.2025.1644115/xml) · 2026-03-10
[4] [Synthetic Assets and Synthetix: Ushering in Next-Generation ...](https://www.gate.com/blog/synthetic-assets-synthetix-next-generation-crypto-derivatives-trading-gate) · 2026-01-14
[5] [[PDF] Decentralized physical infrastructure networks (DePIN) tokenomics](https://public-pages-files-2025.frontiersin.org/journals/blockchain/articles/10.3389/fbloc.2025.1644115/pdf) · 2026-04-08
[6] [What Is USD.AI? Inside the CHIP Token and AI ...](https://atomicwallet.io/academy/articles/what-is-usd-ai) · 2026-03-05
[7] [Experiments - Onchain Atlas](https://www.onchainatlas.org/experiments/) · 2026-09-21
[8] [Blockchain Networks: Token Design and Management Overview - NIST](https://nvlpubs.nist.gov/nistpubs/ir/2021/NIST.IR.8301.pdf) · 2026-08-23
[9] [DePIN Meets Bitcoin: How Decentralized Physical Infrastructure ...](https://www.spark.money/research/depin-bitcoin-payment-infrastructure) · 2026-06-15
[10] [Understanding the Crypto Investment Flywheel: DePIN, AI & RWA| KuCoin](https://www.kucoin.com/blog/understanding-the-crypto-investment-flywheel-depin-ai-rwa) · 2026-06-07

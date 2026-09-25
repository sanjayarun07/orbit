# Trading skills: revisit later

Source: [agiprolabs/claude-trading-skills](https://github.com/agiprolabs/claude-trading-skills) (reviewed 2026-09-26).

The repository is a collection of trading and analysis playbooks, examples, and scripts. It is not a turnkey data provider or execution backend. Do not add its full skill catalog to Anvaya's runtime tool selection; that would make routing harder to evaluate.

When we revisit it, assess these methods in this order:

1. [Token-holder analysis](https://github.com/agiprolabs/claude-trading-skills/blob/main/skills/token-holder-analysis/SKILL.md) and [sybil detection](https://github.com/agiprolabs/claude-trading-skills/blob/main/skills/sybil-detection/SKILL.md): concentration, shared funding, early-buyer and linked-wallet evidence for meme-token investigations.
2. [Liquidity analysis](https://github.com/agiprolabs/claude-trading-skills/blob/main/skills/liquidity-analysis/SKILL.md) and [slippage modeling](https://github.com/agiprolabs/claude-trading-skills/blob/main/skills/slippage-modeling/SKILL.md): position-specific exit depth and execution-cost estimates.
3. [Walk-forward validation](https://github.com/agiprolabs/claude-trading-skills/blob/main/skills/walk-forward-validation/SKILL.md) and [live-operations gates](https://github.com/agiprolabs/claude-trading-skills/blob/main/skills/prediction-market-live-ops/SKILL.md): time-aware evaluation, paper/live progression, fill reconciliation, and predeclared stop conditions if trading execution becomes a priority.

Adapt methods as structured computations over Anvaya's existing data tools, with source transactions, coverage, uncertainty, and recorded evaluation cases. Treat fixed risk thresholds and claims such as “same transaction means definite sybil” as unvalidated heuristics until tested on representative data. This is a research note, not an implementation commitment.

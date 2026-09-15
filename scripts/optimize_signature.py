"""Standalone DSPy optimizer proof-of-concept -- NOT imported by the running
app, and does NOT modify app/nodes/runtime.py's live `trade_simulator`.

Groundwork only (see the loop-harness-engineering improvement pass): this
app has never used dspy.teleprompt.BootstrapFewShot/MIPRO anywhere, despite
app/sessions.py already logging real per-turn trajectories. A full
production pipeline needs a real evaluation metric per signature and a
persisted, outcome-labelled training corpus mined from those logs -- neither
exists yet (see data/optimized_prompts/README.md). This script instead runs
one bounded, manually-curated proof of concept against the smallest/newest
ReAct signature (TradeSimulationExtraction) and saves the result to disk for
manual review -- adopting it for real is a separate, future decision.

Usage:
    .venv/bin/python scripts/optimize_signature.py

Requires the same live API keys the app already uses (real LM calls, real
Solana RPC/Jupiter calls via trade_simulator's tools -- sol_balance,
spl_balances, search_verified_tokens are NOT mocked here on purpose, so this
reflects genuine tool behavior, not a stub).
"""

from __future__ import annotations

import json
from pathlib import Path

import dspy

from app.nodes import runtime

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "optimized_prompts"
_EXAMPLES_PATH = _DATA_DIR / "trade_simulation_examples.json"
_OUTPUT_PATH = _DATA_DIR / "trade_simulation_compiled.json"


def _load_examples() -> list[dspy.Example]:
    rows = json.loads(_EXAMPLES_PATH.read_text())
    examples = []
    for row in rows:
        example = dspy.Example(
            request=row["request"],
            wallet_address=row["wallet_address"],
            conversation_history=row["conversation_history"],
            should_simulate=row["should_simulate"],
            input_mint=row["input_mint"],
            output_mint=row["output_mint"],
            amount_atomic=row["amount_atomic"],
        ).with_inputs("request", "wallet_address", "conversation_history")
        examples.append(example)
    return examples


def _metric(example: dspy.Example, prediction, _trace=None) -> bool:
    """Exact match on should_simulate; when a simulation is expected, mint
    identity must also match (amount is left out of the metric -- "sell it"
    with no stated quantity is intentionally ambiguous without a real
    balance lookup, and this proof of concept doesn't mock one)."""
    if prediction.should_simulate != example.should_simulate:
        return False
    if not example.should_simulate:
        return True
    return prediction.input_mint == example.input_mint and prediction.output_mint == example.output_mint


def main() -> None:
    examples = _load_examples()
    print(f"Loaded {len(examples)} hand-curated examples from {_EXAMPLES_PATH}")

    optimizer = dspy.BootstrapFewShot(metric=_metric, max_bootstrapped_demos=4, max_labeled_demos=4)
    compiled = optimizer.compile(student=runtime.trade_simulator, trainset=examples)

    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    compiled.save(str(_OUTPUT_PATH), save_program=False)
    print(f"Saved compiled few-shot demos to {_OUTPUT_PATH}")
    print(
        "This artifact is NOT wired into the running app. Compare its demos "
        "against the current signature's zero-shot behavior manually before "
        "considering adoption -- see data/optimized_prompts/README.md."
    )


if __name__ == "__main__":
    main()

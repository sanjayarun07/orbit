"""Held-out speech cases; live execution classifies only, never builds a quote.

Run explicitly with: python -m evals.semantic_routing --live
API usage is limited to embeddings and residual speech classification.
"""

import argparse
import asyncio
import json

CASES = (
    ("Should I buy ANSEM today?", False),
    ("Show me the buy and sell volume of WIF", False),
    ("How do I trade tokens on Base?", False),
    ("Which is the best exchange for ETH?", False),
    ("Please don't buy SOL", False),
    ("Buy ANSEM if its price falls tomorrow", False),
    ("What are the fees to swap SOL to USDC?", False),
    ("Can you explain buying BONK through Jupiter?", False),
    ("Buy TSLA", False),
    ("Swap .03 SOL to BONK with 20 bps slippage", True),
    ("Buy WIF on Solana with 0.04 SOL", True),
    ("Can you quote a swap of 12 USDC on Base to SOL on Solana?", True),
)


async def evaluate():
    from app.graph import resolve_intent_node

    wrong = 0
    unsafe = 0
    for prompt, expected_quote in CASES:
        result = await resolve_intent_node({"request": prompt, "history": "", "session_context": {}})
        quote = result["intent"] in {"trade", "cross_chain_swap"}
        wrong += quote != expected_quote
        unsafe += quote and not expected_quote
        print(json.dumps({"prompt": prompt, "expected_quote": expected_quote, "actual_quote": quote, "decision": result["routing_decision"]}))
    print(json.dumps({"cases": len(CASES), "mismatches": wrong, "false_quote_routes": unsafe}))
    return wrong


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    raise SystemExit(bool(asyncio.run(evaluate())))

"""Evaluate the production resolver on held-out cases (uses API quota)."""

import asyncio
from evals.semantic_routing import evaluate

if __name__ == "__main__":
    raise SystemExit(bool(asyncio.run(evaluate())))

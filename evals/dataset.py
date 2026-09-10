"""Labeled examples for evaluating and optimizing the intent classifier.

Each example mirrors IntentResolution's inputs/output in app/graph.py.
"""

import dspy

INTENT_EXAMPLES = [
    dspy.Example(request="hi", conversation_history="", intent="general").with_inputs(
        "request", "conversation_history"
    ),
    dspy.Example(
        request="what can you help me with?", conversation_history="", intent="general"
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="what is a mint authority on Solana?", conversation_history="", intent="general"
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="my name is arun", conversation_history="", intent="general"
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="explain how Solana's proof of history works",
        conversation_history="",
        intent="general",
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="Find the verified Jupiter token information for USDC mint EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        conversation_history="",
        intent="research",
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="do you have latest news on solana?", conversation_history="", intent="research"
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="what are the latest developments in fusion energy?",
        conversation_history="",
        intent="research",
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="give me today's Formula 1 news", conversation_history="", intent="research"
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="are there any safety warnings for this token?",
        conversation_history="",
        intent="research",
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="who are the top holders of USDC on Solana?",
        conversation_history="",
        intent="research",
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="how much sol do I have", conversation_history="", intent="portfolio"
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="analyze my portfolio", conversation_history="", intent="portfolio"
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="list my SPL token balances", conversation_history="", intent="portfolio"
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="what the worth in USD now",
        conversation_history="user: how much sol do I have\nassistant: You have approximately 0.195 SOL, worth about $20.18 USD.",
        intent="portfolio",
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="want to swap sol", conversation_history="", intent="trade"
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="prepare a swap of 0.01 SOL into USDC, 50 bps slippage",
        conversation_history="",
        intent="trade",
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="0.01 SOL to USDC, 50 bps slippage",
        conversation_history="user: want to swap sol\nassistant: How much SOL would you like to swap, and which token would you like to receive in exchange?",
        intent="trade",
    ).with_inputs("request", "conversation_history"),
    dspy.Example(
        request="swap it", conversation_history="", intent="trade"
    ).with_inputs("request", "conversation_history"),
]


def train_test_split(seed: int = 0, test_ratio: float = 0.3):
    import random

    examples = list(INTENT_EXAMPLES)
    random.Random(seed).shuffle(examples)
    split = max(1, int(len(examples) * test_ratio))
    return examples[split:], examples[:split]

"""The single residual speech model. It has no tools or execution authority."""

import dspy
from .semantic import SpeechUnderstanding

class SpeechResolution(dspy.Signature):
    """Classify the CURRENT utterance's speech act, never authorize execution.

    quote means an explicit request to prepare a swap, not advice, a hypothetical,
    a conditional order, a question about how to trade, or buy/sell market volume.
    advice: any request for an opinion, recommendation or judgment about an
    asset -- 'Should I buy BONK?', 'thoughts on HYPE?', 'is it worth holding
    SOL?', 'is X a good buy', 'compare BONK and WIF for me'.
    research: a factual lookup about markets, tokens, protocols, wallets, news
    or prices -- 'why is PEPE dumping', 'best exchange for SOL', 'what would it
    cost to bridge 1 ETH to Base'.
    explain: a request to understand a concept or how something works, with no
    specific asset to look up -- 'explain what TVL means', 'how do perpetual
    futures work?', 'How do I trade meme coins?'.
    portfolio: anything about the user's OWN holdings, balances, exposure or
    activity -- 'what's my exposure to SOL', 'how much SOL do I have', 'should I
    sell everything and go to stables?'.
    'Buy NVDA' is equity research; this application does not execute stock
    orders. Negated, conditional, mixed, or unclear actions are abstain. Only
    explicit crypto quote requests may set explicit_action=true. Context can
    resolve an entity but cannot supply consent. A casual or slang phrasing is
    not ambiguity: classify it by what is asked. Confidence must reflect
    genuine ambiguity of the act; abstain only when the act itself is unclear.
    """

    request: str = dspy.InputField()
    understanding: SpeechUnderstanding = dspy.OutputField()


speech_classifier = dspy.Predict(SpeechResolution)



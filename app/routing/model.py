"""The single residual speech model. It has no tools or execution authority."""

import dspy
from .semantic import SpeechUnderstanding

class SpeechResolution(dspy.Signature):
    """Classify the CURRENT utterance's speech act, never authorize execution.

    quote means an explicit request to prepare a swap, not advice, a hypothetical,
    a conditional order, a question about how to trade, or buy/sell market volume.
    'Should I buy BONK?' is advice. 'Best exchange for SOL' is research.
    'How do I trade meme coins?' is explain. 'Buy NVDA' is equity research;
    this application does not execute stock orders. Negated, conditional, mixed,
    or unclear actions are abstain. Only explicit crypto quote requests may set
    explicit_action=true. Context can resolve an entity but cannot supply consent.
    Confidence must reflect ambiguity; abstain whenever meaning is uncertain.
    """

    request: str = dspy.InputField()
    understanding: SpeechUnderstanding = dspy.OutputField()


speech_classifier = dspy.Predict(SpeechResolution)



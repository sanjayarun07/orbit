"""Optimize the intent classifier's prompt using DSPy GEPA.

Run with: python -m evals.optimize_gepa
Saves evals/optimized/speech_classifier.json for offline review and evaluation.
Artifacts are not automatically promoted into production routing.
"""

import hashlib
import json
import os

import dspy

from app.routing.model import SpeechResolution, speech_classifier
from app.routing.semantic import SpeechUnderstanding
from app.routing.examples import EXAMPLES
from app.settings import settings

OUTPUT_PATH = "evals/optimized/speech_classifier.json"
META_PATH = "evals/optimized/speech_classifier.meta.json"


def feedback_metric(example, prediction, trace=None, pred_name=None, pred_trace=None):
    expected = example.understanding
    actual = SpeechUnderstanding.model_validate(prediction.understanding)
    correct = (expected.speech_act, expected.domain, expected.explicit_action) == (actual.speech_act, actual.domain, actual.explicit_action)
    feedback = (
        "Correct intent."
        if correct
        else f"Expected {expected.model_dump()}, received {actual.model_dump()}."
    )
    return dspy.Prediction(score=1.0 if correct else 0.0, feedback=feedback)


def main():
    # Record the signature version for artifact review and reproducibility.
    source_fingerprint = hashlib.sha256(SpeechResolution.__doc__.encode()).hexdigest()

    examples = [dspy.Example(request=text, understanding=SpeechUnderstanding(speech_act=act, domain=domain, explicit_action=act == "quote", confidence=1.0)).with_inputs("request") for act, domain, text in EXAMPLES]
    trainset, valset = examples[::2], examples[1::2]
    dspy.configure(lm=dspy.LM(settings.model))
    optimizer = dspy.GEPA(
        metric=feedback_metric,
        auto="light",
        reflection_lm=dspy.LM(settings.model),
        num_threads=4,
    )
    optimized = optimizer.compile(speech_classifier, trainset=trainset, valset=valset)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    optimized.save(OUTPUT_PATH)
    with open(META_PATH, "w") as f:
        json.dump({"source_fingerprint": source_fingerprint}, f)
    print(f"Saved optimized intent classifier to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

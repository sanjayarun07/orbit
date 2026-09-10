"""Sweep embedding thresholds on held-out cases; --live consumes API quota.

Reports false-quote, missed-quote and clarification rates independently.
This evaluates the embedding stage alone, not the residual model.
"""
import argparse
import json
from openai import OpenAI
from app.routing.examples import EXAMPLES
from app.routing.semantic import rank, unit
from app.settings import settings
from evals.semantic_routing import CASES


def sweep(vectors, examples=EXAMPLES, cases=CASES):
    bank = vectors[:len(examples)]
    queries = vectors[len(examples):]
    if len(queries) != len(cases):
        raise ValueError("Evaluation vector count mismatch")
    rows = []
    for threshold in (.65, .7, .75, .78, .82):
        for margin in (.04, .08, .12):
            false_quotes = missed = clarify = 0
            for vector, (_, expected_quote) in zip(queries, cases):
                result = rank(vector, bank, examples, threshold, margin)
                predicted = result.understanding is not None and result.understanding.speech_act == "quote"
                false_quotes += predicted and not expected_quote
                missed += expected_quote and not predicted
                clarify += result.understanding is None
            rows.append({"threshold": threshold, "margin": margin, "cases": len(cases),
                         "false_quote_routes": false_quotes, "false_quote_rate": false_quotes / len(cases),
                         "missed_quote_rate": missed / max(1, sum(expected for _, expected in cases)),
                         "clarify_rate": clarify / len(cases)})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    inputs = [row[2] for row in EXAMPLES] + [row[0] for row in CASES]
    with OpenAI(api_key=settings.openai_api_key, timeout=20, max_retries=0) as client:
        result = client.embeddings.create(model=settings.intent_embedding_model, input=inputs, encoding_format="float")
    vectors = [unit(row.embedding) for row in sorted(result.data, key=lambda item: item.index)]
    for row in sweep(vectors):
        print(json.dumps(row))


if __name__ == "__main__":
    main()

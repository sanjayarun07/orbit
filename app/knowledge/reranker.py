"""Rerankers behind `retrieval.set_reranker`.

    heuristic      entity / term / scope boosts on the fused score (default,
                   no model, microseconds)
    cross-encoder  sentence-transformers CrossEncoder (ms-marco MiniLM by
                   default): query+passage scored jointly, ~10 ms per pair on
                   CPU. Needs `pip install '.[rerank]'`; the model downloads
                   on first use (~90 MB).
    llm            listwise relevance grades from the chat model, one call
                   per query. No new dependency; slower and costs tokens.

Choose with KNOWLEDGE_RERANKER. Every reranker keeps the retrieval scope
boost (a passage from the protocol the question names still outranks a
lookalike from a competitor) so graph and lexical signals are not thrown away.
"""

from __future__ import annotations

import json
import logging
import math
import re

from app.knowledge.models import RetrievalHit
from app.settings import settings

logger = logging.getLogger(__name__)

SCOPE_BOOST = 0.25
_WORD = re.compile(r"[a-z0-9$#-]+")
_STOP = {"the", "a", "an", "of", "on", "in", "to", "for", "and", "or", "is", "are", "what", "how", "does", "do", "explain", "me", "about", "tell", "with", "vs", "versus", "compare"}


def _scope_boost(hit: RetrievalHit, plan) -> float:
    return SCOPE_BOOST if hit.chunk.protocol_id and hit.chunk.protocol_id in plan.protocol_ids else 0.0


class HeuristicReranker:
    """Boosts chunks that mention resolved entities and exact query terms in
    their heading; demotes very short chunks. Deterministic and cheap."""

    name = "heuristic"

    def rerank(self, query: str, hits: list[RetrievalHit], plan) -> list[RetrievalHit]:
        terms = [t for t in _WORD.findall(query.lower()) if t not in _STOP and len(t) > 2]
        names = [r.entity.canonical_name.lower() for r in plan.entities]
        for hit in hits:
            text = hit.chunk.text.lower()
            heading = hit.chunk.heading.lower()
            boost = 0.0
            boost += 0.15 * sum(1 for t in terms if t in heading)
            boost += 0.05 * sum(1 for t in terms if t in text)
            boost += 0.2 * sum(1 for n in names if n in text)
            if len(hit.chunk.content) < 120:
                boost -= 0.2
            hit.score += boost + _scope_boost(hit, plan)
        hits.sort(key=lambda h: -h.score)
        return hits


class CrossEncoderReranker:
    """sentence-transformers cross-encoder. Scores are sigmoid(logit) in
    [0, 1]; fused retrieval score is kept as a small tie-breaker."""

    name = "cross-encoder"

    def __init__(self, model_name: str, max_pairs: int = 40):
        from sentence_transformers import CrossEncoder  # optional dependency

        self._model = CrossEncoder(model_name, max_length=512)
        self.model_name = model_name
        self.max_pairs = max_pairs

    def rerank(self, query: str, hits: list[RetrievalHit], plan) -> list[RetrievalHit]:
        if not hits:
            return hits
        head, tail = hits[: self.max_pairs], hits[self.max_pairs:]
        pairs = [(query, hit.chunk.text[:2000]) for hit in head]
        logits = self._model.predict(pairs, show_progress_bar=False)
        for hit, logit in zip(head, logits):
            relevance = 1.0 / (1.0 + math.exp(-float(logit)))
            hit.score = relevance + _scope_boost(hit, plan) + 0.01 * hit.score
        for hit in tail:
            hit.score = 0.01 * hit.score
        hits.sort(key=lambda h: -h.score)
        return hits


class LLMReranker:
    """Listwise grading by the chat model: each passage gets 0-10 for how well
    it answers the question. One request per query; falls back to the
    heuristic order when the model fails."""

    name = "llm"

    def __init__(self, model: str | None = None, max_passages: int = 20):
        from openai import OpenAI

        self._client = OpenAI(api_key=settings.openai_api_key)
        self.model = model or "gpt-4.1-mini"
        self.max_passages = max_passages
        self._fallback = HeuristicReranker()

    def rerank(self, query: str, hits: list[RetrievalHit], plan) -> list[RetrievalHit]:
        if not hits:
            return hits
        head = hits[: self.max_passages]
        listing = "\n\n".join(f"[{i}] {hit.chunk.text[:900]}" for i, hit in enumerate(head))
        prompt = (
            "Grade how well each passage answers the question. Reply with JSON only: "
            '{"grades": {"<index>": <0-10>, ...}}. 10 = directly answers, 0 = unrelated.\n\n'
            f"Question: {query}\n\nPassages:\n{listing}"
        )
        try:
            response = self._client.chat.completions.create(model=self.model, temperature=0, response_format={"type": "json_object"}, messages=[{"role": "user", "content": prompt}])
            grades = json.loads(response.choices[0].message.content or "{}").get("grades") or {}
        except Exception:
            logger.warning("knowledge: llm reranker failed; using heuristic order", exc_info=True)
            return self._fallback.rerank(query, hits, plan)
        for i, hit in enumerate(head):
            try:
                grade = float(grades.get(str(i), 0)) / 10.0
            except (TypeError, ValueError):
                grade = 0.0
            hit.score = grade + _scope_boost(hit, plan) + 0.01 * hit.score
        for hit in hits[self.max_passages:]:
            hit.score = 0.01 * hit.score
        hits.sort(key=lambda h: -h.score)
        return hits


def build_reranker(kind: str | None = None):
    """The reranker named by KNOWLEDGE_RERANKER, degrading to the heuristic
    with a logged warning when a model or dependency is unavailable."""
    kind = (kind or settings.knowledge_reranker or "heuristic").lower()
    if kind == "cross-encoder":
        try:
            return CrossEncoderReranker(settings.knowledge_reranker_model)
        except Exception:
            logger.warning("knowledge: cross-encoder reranker unavailable (pip install '.[rerank]'); using heuristic", exc_info=True)
    elif kind == "llm":
        if settings.openai_api_key:
            try:
                return LLMReranker()
            except Exception:
                logger.warning("knowledge: llm reranker unavailable; using heuristic", exc_info=True)
        else:
            logger.warning("knowledge: llm reranker needs OPENAI_API_KEY; using heuristic")
    elif kind != "heuristic":
        logger.warning("knowledge: unknown reranker %r; using heuristic", kind)
    return HeuristicReranker()

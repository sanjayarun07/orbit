from types import SimpleNamespace

from app import call_budget
from app.routing.tool_semantic import ToolEmbeddingRouter
from app.settings import settings


def test_empty_gated_set_is_a_free_no_op(monkeypatch):
    from app.routing import tool_semantic

    def fail_if_called(**_kwargs):
        raise AssertionError("OpenAI must never be called for an empty gated set")

    monkeypatch.setattr(settings, "openai_api_key", "test")
    monkeypatch.setattr(tool_semantic, "OpenAI", fail_if_called)
    router = ToolEmbeddingRouter("test-model")
    result = router.qualify("some request", "market_data", ())
    assert result.qualifying == frozenset()
    assert result.reason == "no_gated_tools"


def test_tool_embedding_cache_and_description_vectors_are_reused(monkeypatch):
    """Mirrors tests/test_semantic_routing.py's
    test_embedding_cache_and_example_initialization_are_reused: descriptions
    for a capability are embedded once (batched with the first real query),
    only the query is re-embedded on subsequent calls for the SAME gated set.
    """
    from app.routing import tool_semantic
    calls = []

    class Client:
        def __init__(self, **kwargs):
            self.embeddings = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, **kwargs):
            calls.append(kwargs["input"])
            return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[1.0, 0.0]) for i, _ in enumerate(kwargs["input"])])

    monkeypatch.setattr(settings, "openai_api_key", "test")
    monkeypatch.setattr(tool_semantic, "OpenAI", Client)
    router = ToolEmbeddingRouter("test-model")
    gated = (("tool_a", "description of tool a"), ("tool_b", "description of tool b"))

    router.qualify("a held-out request", "market_data", gated)
    router.qualify("a held-out request", "market_data", gated)  # cache hit, no new call
    router.qualify("another held-out request", "market_data", gated)  # same gated set -- only the query re-embedded

    assert len(calls) == 2
    assert len(calls[0]) == 3  # 2 descriptions + 1 query, batched together
    assert calls[1] == ["another held-out request"]


def test_tool_embedding_errors_are_sanitized_and_circuit_breaks(monkeypatch):
    from app.routing import tool_semantic
    calls = []

    def broken(**kwargs):
        calls.append(1)
        raise RuntimeError("secret provider error")

    monkeypatch.setattr(settings, "openai_api_key", "test")
    monkeypatch.setattr(tool_semantic, "OpenAI", broken)
    router = ToolEmbeddingRouter("test-model")
    gated = (("tool_a", "description of tool a"),)

    assert router.qualify("unmatched one", "market_data", gated).reason == "provider_unavailable"
    assert router.qualify("unmatched two", "market_data", gated).reason == "circuit_open"
    assert len(calls) == 1


def test_qualify_ranks_by_threshold_not_a_single_winner(monkeypatch):
    """Unlike the intent classifier (single argmax winner with a margin
    gate), tool retrieval must be able to return MULTIPLE qualifying tools
    -- final selection among them is ProviderRouter._score()'s job, not
    this layer's.
    """
    from app.routing import tool_semantic

    class Client:
        def __init__(self, **kwargs):
            self.embeddings = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, **kwargs):
            # First two inputs (descriptions) get a vector identical to the
            # query (perfect match); the third description is orthogonal.
            vectors = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]
            return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=v) for i, v in enumerate(vectors[:len(kwargs["input"])])])

    monkeypatch.setattr(settings, "openai_api_key", "test")
    monkeypatch.setattr(settings, "tool_embedding_threshold", 0.5)
    monkeypatch.setattr(tool_semantic, "OpenAI", Client)
    router = ToolEmbeddingRouter("test-model")
    gated = (("tool_a", "desc a"), ("tool_b", "desc b"), ("tool_c", "desc c"))

    result = router.qualify("a query", "market_data", gated)
    assert result.qualifying == frozenset({"tool_a", "tool_b"})  # both above threshold; tool_c (orthogonal) excluded


def test_qualify_charges_the_call_budget(monkeypatch):
    from app.routing import tool_semantic

    class Client:
        def __init__(self, **kwargs):
            self.embeddings = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, **kwargs):
            return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[1.0, 0.0]) for i, _ in enumerate(kwargs["input"])])

    monkeypatch.setattr(settings, "openai_api_key", "test")
    monkeypatch.setattr(tool_semantic, "OpenAI", Client)
    router = ToolEmbeddingRouter("test-model")
    gated = (("tool_a", "description of tool a"),)

    token = call_budget.start_budget(max_calls=10, max_cost_usd=100.0)
    try:
        router.qualify("a request", "market_data", gated)
        budget = call_budget.current_budget()
        assert budget.calls == 1
        router.qualify("a request", "market_data", gated)  # cache hit, no extra charge
        assert call_budget.current_budget().calls == 1
    finally:
        call_budget.reset_budget(token)


def test_qualify_stops_before_calling_openai_when_budget_exhausted(monkeypatch):
    from app.routing import tool_semantic

    def fail_if_called(**_kwargs):
        raise AssertionError("OpenAI must never be called once the turn budget is exhausted")

    monkeypatch.setattr(settings, "openai_api_key", "test")
    monkeypatch.setattr(tool_semantic, "OpenAI", fail_if_called)
    router = ToolEmbeddingRouter("test-model")
    gated = (("tool_a", "description of tool a"),)

    token = call_budget.start_budget(max_calls=0, max_cost_usd=100.0)
    try:
        result = router.qualify("a request", "market_data", gated)
        assert result.reason == "budget_exhausted"
    finally:
        call_budget.reset_budget(token)

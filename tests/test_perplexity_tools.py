import json

from app import call_budget, perplexity_tools
from app.capability_router import route_capabilities
from app.settings import settings


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Bitcoin is trading at $78,702.70 USD."}],
                },
                {"type": "web_search_call", "results": [{"title": "Primary source", "url": "https://example.com/source"}]},
            ]
        }


def test_perplexity_request_uses_only_selected_tool(monkeypatch):
    captured = {}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return _Response()

    monkeypatch.setattr(settings, "perplexity_api_key", "test-key")
    monkeypatch.setattr(perplexity_tools.httpx, "Client", Client)
    perplexity_tools._cache.clear()
    result = perplexity_tools.perplexity_finance_search("BTC price now")
    assert captured["json"]["tools"] == [{"type": "finance_search"}]
    assert "sandbox" not in json.dumps(captured["json"])
    assert "$78,702.70 USD" in result
    assert "https://example.com/source" in result


def test_perplexity_answer_strips_leaked_citation_markers(monkeypatch):
    """A hosted-search provider can leak inline citation-annotation control
    characters (Private Use Area sentinels) into its answer text; those must
    never reach the user (and, once cached, every subsequent caller).
    """
    class _MarkerResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [{
                            "type": "output_text",
                            "text": "Meow founded Jupiterciteturn0search1https://ex.com/a in 2021.",
                        }],
                    },
                ]
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **_kwargs):
            return _MarkerResponse()

    monkeypatch.setattr(settings, "perplexity_api_key", "test-key")
    monkeypatch.setattr(perplexity_tools.httpx, "Client", Client)
    perplexity_tools._cache.clear()
    result = perplexity_tools.perplexity_web_search("who founded Jupiter")
    assert "" not in result and "" not in result and "" not in result
    assert "cite" not in result
    assert result.startswith("Meow founded Jupiter in 2021.")


def test_catalog_never_exposes_sandbox():
    catalog = perplexity_tools.perplexity_catalog()
    assert {item["name"] for item in catalog} == {
        "perplexity_web_search",
        "perplexity_fetch_url",
        "perplexity_people_search",
        "perplexity_finance_search",
    }
    assert all(item["risk"] == "read_only" for item in catalog)


def test_perplexity_soft_failure_is_not_cached(monkeypatch):
    class RefusalResponse(_Response):
        def json(self):
            return {
                "output": [{
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": "I can’t retrieve the live Bitcoin price because market-data access is unavailable.",
                    }],
                }]
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return RefusalResponse()

    monkeypatch.setattr(settings, "perplexity_api_key", "test-key")
    monkeypatch.setattr(perplexity_tools.httpx, "Client", Client)
    perplexity_tools._cache.clear()

    try:
        perplexity_tools.perplexity_finance_search("Bitcoin price now")
    except RuntimeError as exc:
        assert "unusable" in str(exc)
    else:
        raise AssertionError("soft failure should be rejected")
    assert perplexity_tools._cache == {}


def test_perplexity_intent_capabilities():
    assert route_capabilities("Summarize https://example.com/report").capabilities == ("url_fetch",)
    assert route_capabilities("Find engineers at Acme").capabilities == ("people_intelligence",)
    assert "finance_data" in route_capabilities("Bitcoin price now").capabilities


def test_perplexity_call_is_refused_and_not_cached_when_budget_exhausted(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            calls.append(1)
            return _Response()

    monkeypatch.setattr(settings, "perplexity_api_key", "test-key")
    monkeypatch.setattr(perplexity_tools.httpx, "Client", Client)
    perplexity_tools._cache.clear()

    token = call_budget.start_budget(max_calls=0, max_cost_usd=100.0)
    try:
        try:
            perplexity_tools.perplexity_finance_search("BTC price now")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "budget" in str(exc).lower()
    finally:
        call_budget.reset_budget(token)
    assert calls == []  # the real HTTP call was never made
    assert perplexity_tools._cache == {}
    assert perplexity_tools._inflight == set()  # inflight tracking was cleaned up on the early raise


def test_perplexity_cache_hit_does_not_charge_the_budget(monkeypatch):
    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(settings, "perplexity_api_key", "test-key")
    monkeypatch.setattr(perplexity_tools.httpx, "Client", Client)
    perplexity_tools._cache.clear()
    perplexity_tools.perplexity_finance_search("BTC price now")  # warms the cache, no budget active

    token = call_budget.start_budget(max_calls=0, max_cost_usd=100.0)
    try:
        # Budget is already exhausted, but this must be a cache hit.
        result = perplexity_tools.perplexity_finance_search("BTC price now")
    finally:
        call_budget.reset_budget(token)
    assert "$78,702.70 USD" in result


def test_reliably_provide_soft_failure_is_rejected():
    from app.provider_router import is_usable_provider_output

    assert not is_usable_provider_output(
        "NVDA latest price",
        "I can’t reliably provide the requested live NVDA research in this turn.",
    )

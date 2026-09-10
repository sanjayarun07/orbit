import json

from app import perplexity_tools
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


def test_reliably_provide_soft_failure_is_rejected():
    from app.provider_router import is_usable_provider_output

    assert not is_usable_provider_output(
        "NVDA latest price",
        "I can’t reliably provide the requested live NVDA research in this turn.",
    )

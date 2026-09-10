from app.capability_router import route_capabilities
from app.provider_registry import get_provider_router
from app.rootdata_provider import RootDataProvider, _cache
from app.settings import settings


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "result": 200,
            "data": [
                {"id": 1, "name": "Paradigm", "active": True},
                {"id": 2, "name": "Pantera Capital", "active": True},
            ],
        }


def test_rootdata_vc_search_uses_documented_id_map_and_server_key(monkeypatch):
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

    _cache.clear()
    monkeypatch.setattr("app.rootdata_provider.httpx.Client", Client)
    monkeypatch.setattr(settings, "rootdata_api_key", "server-secret")
    output = RootDataProvider().search("Find crypto VC Paradigm", "vc")
    assert captured["url"].endswith("/id_map")
    assert captured["headers"]["apikey"] == "server-secret"
    assert captured["json"] == {"type": 2}
    assert "Paradigm" in output
    assert "server-secret" not in output


def test_rootdata_map_is_reused_across_searches(monkeypatch):
    calls = 0

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **_kwargs):
            nonlocal calls
            calls += 1
            return _Response()

    _cache.clear()
    monkeypatch.setattr("app.rootdata_provider.httpx.Client", Client)
    monkeypatch.setattr(settings, "rootdata_api_key", "server-secret")
    provider = RootDataProvider()
    provider.search("Find crypto VC Paradigm", "vc")
    provider.search("Find crypto VC Pantera", "vc")
    assert calls == 1


def test_web3_project_and_vc_requests_route_to_rootdata_capabilities():
    assert route_capabilities("Find crypto project EigenLayer").capabilities == ("project_intelligence",)
    assert route_capabilities("Research crypto VC Paradigm").capabilities == ("vc_intelligence",)


def test_rootdata_tools_are_visible_but_disabled_without_key(monkeypatch):
    monkeypatch.setattr(settings, "rootdata_api_key", None)
    get_provider_router.cache_clear()
    rows = [row for row in get_provider_router().catalog() if row["provider"] == "rootdata"]
    assert {row["name"] for row in rows} == {
        "rootdata_project_search", "rootdata_vc_search", "rootdata_people_search"
    }
    assert all(row["configured"] is False for row in rows)

"""A first ingest on a fresh database used to pull nothing: the registry it
walks was only ever filled by the admin bootstrap endpoint. The standalone
ingester now bootstraps an empty registry itself."""
import asyncio

from app.knowledge import ingest, registry, store as kb_store

LLAMA = [
    {"name": "Aave V3", "slug": "aave-v3", "tvl": 20e9, "chains": ["Ethereum"], "category": "Lending", "url": "https://aave.com", "gecko_id": "aave"},
    {"name": "Lido", "slug": "lido", "tvl": 30e9, "chains": ["Ethereum"], "category": "Liquid Staking", "url": "https://lido.fi", "gecko_id": "lido-dao"},
]


def test_an_empty_registry_is_bootstrapped_before_the_run(monkeypatch):
    memory = kb_store.MemoryStore()

    async def get_memory():
        return memory

    for module in (ingest, registry, kb_store):
        monkeypatch.setattr(module, "get_store", get_memory, raising=False)
    fetched = []
    monkeypatch.setattr(registry, "_fetch_protocols", lambda: fetched.append(1) or LLAMA)

    async def run():
        before = await memory.list_protocols(limit=50)
        count = await ingest.ensure_registry(memory, limit=50)
        again = await ingest.ensure_registry(memory, limit=50)
        return before, count, again

    before, count, again = asyncio.run(run())
    assert before == [] and count == 2 and again == 2
    assert fetched == [1], "a populated registry must not be re-fetched"

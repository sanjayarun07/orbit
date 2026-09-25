"""A registry name that is also an English word ("cap", "drop", "compound")
resolves only when written as a name. Live, 2026-09-24: "What are the specific
reasons behind the 6.32% drop in the total crypto market cap today?" quoted
the Cap protocol's vault-cap scripts and a "Drop exploit" incident."""
from app.knowledge.entities import EntityResolver, is_common_word, spelled_as_name
from app.knowledge.models import Entity

Q = "What are the specific reasons behind the 6.32% drop in the total crypto market cap today?"


def _resolver() -> EntityResolver:
    return EntityResolver([
        Entity(id="protocol:cap", entity_type="protocol", canonical_name="cap", symbol="CAP", aliases=["cap money"], metadata={"defillama_slug": "cap", "tvl_usd": 3e8}),
        Entity(id="incident:drop", entity_type="incident", canonical_name="Drop exploit — 2026-09-22", aliases=["drop", "Drop"], metadata={"date": "2026-09-22"}),
        Entity(id="protocol:compound-v3", entity_type="protocol", canonical_name="Compound V3", aliases=["compound", "Compound"], metadata={"tvl_usd": 2e9}),
        Entity(id="protocol:aave-v3", entity_type="protocol", canonical_name="Aave V3", aliases=["aave", "Aave"], metadata={"tvl_usd": 2e10}),
        Entity(id="protocol:lido", entity_type="protocol", canonical_name="Lido", aliases=["lido"], metadata={"tvl_usd": 3e10}),
    ])


def test_common_words_are_known():
    assert is_common_word("cap") and is_common_word("drop") and is_common_word("compound")
    assert not is_common_word("aave") and not is_common_word("lido")   # a system dictionary called lido a word; names are names however written


def test_market_cap_and_the_drop_are_no_entities():
    res = _resolver()
    assert res.resolve("cap", context=Q) is None
    assert res.resolve("drop", context=Q) is None
    assert res.mentions(Q) == []


def test_the_same_words_written_as_names_still_resolve():
    res = _resolver()
    assert res.resolve("Cap", context="Is the Cap protocol audited?").entity.id == "protocol:cap"
    assert res.resolve("cap", context="Is the Cap protocol audited?").entity.id == "protocol:cap"
    assert res.resolve("$CAP", context="what is $CAP").entity.id == "protocol:cap"
    assert [m.entity.id for m in res.mentions("Is the Cap protocol audited?")] == ["protocol:cap"]
    assert res.resolve("Compound", context="how does Compound lending work").entity.id == "protocol:compound-v3"
    assert res.resolve("compound", context="how does compound interest work") is None


def test_real_names_resolve_however_written():
    res = _resolver()
    assert res.resolve("lido", context="how does lido staking work").entity.id == "protocol:lido"
    assert res.resolve("aave", context="what is aave's health factor").entity.id == "protocol:aave-v3"


def test_spelled_as_name_reads_the_sentence():
    entity = Entity(id="protocol:cap", entity_type="protocol", canonical_name="cap", aliases=[])
    assert spelled_as_name(entity, "cap", "Cap is a lending protocol")
    assert not spelled_as_name(entity, "cap", "the market cap fell")
    assert not spelled_as_name(entity, "cap", "capital markets")


def test_passages_that_never_mention_the_subject_are_no_card():
    from types import SimpleNamespace
    from app.knowledge import tool
    hits = [SimpleNamespace(protocol_name="Hyperliquid Bridge", document_title="What is Hyperliquid?", chunk=SimpleNamespace(heading="", content="Hyperliquid is a Layer 1 blockchain..."))]
    assert not tool._mentions_subject(hits, 'What is the full name and description of the stock labeled "NBIS" on Hyperliquid?')
    assert tool._mentions_subject(hits, "What is Hyperliquid's HLP vault?")
    assert tool._mentions_subject(hits, "how do onchain order books work")      # no subject named: nothing to require


def test_a_ticker_inside_an_address_is_not_a_mention_and_an_address_in_the_ask_must_appear():
    from types import SimpleNamespace
    from app.knowledge import tool
    maple = [SimpleNamespace(protocol_name="Maple", document_title="Mainnet Addresses", chunk=SimpleNamespace(heading="syrupUSDC",
                             content="Pool HrTBpF3LqSxXnjnYdR4htnBLyMHNZ6eNaDZGPundvHbm on Solana; token AvZZF1YaZDziPY2RCK4oJrRVrbN3mTD9NL24hPeaZeUj"))]
    ask = "What is driving the 51.62% price surge in BP today? BPxxfRCXkUVhig4HS1Lh7kZqV6SPJhzfEk4x6fVBjPCy on solana"
    assert not tool._mentions_subject(maple, ask)                                   # "bp" sits inside a pool address: not a mention (live, 2026-09-25)
    assert not tool._mentions_subject(maple, "What is driving the price surge in BP today?")
    backpack = [SimpleNamespace(protocol_name="Backpack", document_title="BP token", chunk=SimpleNamespace(heading="", content="BP (BPxxfRCXkUVhig4HS1Lh7kZqV6SPJhzfEk4x6fVBjPCy) is Backpack's token"))]
    assert tool._mentions_subject(backpack, ask)
    assert tool._mentions_subject(backpack, "What is BP staking for equity?")

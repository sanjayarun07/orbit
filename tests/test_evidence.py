"""The evidence envelope (app/evidence.py): tools record status, sources,
coverage and errors beside their cards; the validator warns when a gap
is not disclosed; a job keeps what its tools recorded."""
import asyncio

import pytest

from app import evidence, jobs, mobula_meme, mobula_wallet
from app.answer_validator import validate_answer
from app.provider_router import NoData

FART = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"
_REAL_TRADES = mobula_meme.token_trades          # captured before the suite's offline patch replaces it


@pytest.fixture
def turn():
    token = evidence.start_turn()
    yield
    evidence.end_turn(token)


def test_recording_without_a_turn_is_a_no_op():
    evidence.complete("t", {"kind": "token", "id": "x"}, {}, [])
    assert evidence.collected() == []


def test_the_holders_tool_records_a_complete_envelope_with_the_top10_definition(monkeypatch, turn):
    rows = [{"walletAddress": f"W{i}" + "A" * 40, "percentageOfTotalSupply": 10 - i, "labels": []} for i in range(12)]
    monkeypatch.setattr(mobula_meme, "_get", lambda path, params: rows)
    mobula_meme.token_holders(f"top holders of {FART} on solana")
    env = evidence.collected()[0]
    assert env.tool == "mobula_token_holders" and env.status == "complete" and env.subject["id"] == FART
    assert env.data["top10_pct_of_supply"] == 55.0 and "pools, exchanges and burn addresses included" in env.data["top10_definition"]
    assert env.sources[0]["provider"] == "mobula" and env.coverage == {"attempted": 1, "successful": 1, "missing": []}


def test_the_holders_tool_records_unavailable_before_raising(monkeypatch, turn):
    monkeypatch.setattr(mobula_meme, "_get", lambda path, params: [])
    with pytest.raises(NoData):
        mobula_meme.token_holders(f"top holders of {FART} on solana")
    assert evidence.collected()[0].status == "unavailable"


def test_the_bundle_check_records_attempted_successful_and_failed_lookups(monkeypatch, turn):
    buyers = [{"address": f"O{i:02d}" + "A" * 30, "firstHoldingDate": f"2026-09-18T12:00:{i * 3:02d}Z", "initialAmount": "1", "currentBalance": "1"} for i in range(8)]
    monkeypatch.setattr(mobula_meme, "_get_v1", lambda path, params: buyers)
    calls = {"n": 0}

    def flaky(path, params):
        calls["n"] += 1
        if calls["n"] <= 3:
            return {"from": "FUNDER" + "F" * 30}
        raise RuntimeError("HTTP 429")
    monkeypatch.setattr(mobula_meme, "_get", flaky)
    mobula_meme.bundle_evidence(mobula_meme._bundle_analysis(FART, "solana"))    # the router entry is offline in the suite
    env = evidence.collected()[0]
    assert env.status == "partial" and env.coverage["attempted"] == 9 and env.coverage["successful"] == 4    # 1 first-buyers + 8 funding, 5 failed
    assert env.errors == [{"operation": "wallet/funding", "reason": "lookup failed (rate limit or outage)", "count": 5}]
    assert env.data["funding_lookups_failed"] == 5 and "5 of 8 early buyers" in env.gap()


def test_the_wallet_card_records_priced_against_unpriced(monkeypatch, turn):
    payload = {"total_wallet_balance": 1e20, "assets": [
        {"asset": {"id": 0, "name": "Mystery Coin", "symbol": "MYST", "blockchains": ["Ethereum"]}, "estimated_balance": 1e20, "price": 1e12, "token_balance": 1},
        {"asset": {"id": 1, "name": "Ethereum", "symbol": "ETH", "blockchains": ["Ethereum"]}, "estimated_balance": 4000.0, "price": 4000.0, "token_balance": 1}]}
    monkeypatch.setattr(mobula_wallet, "_get", lambda path, params: payload)
    mobula_wallet.portfolio("portfolio of 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
    env = evidence.collected()[0]
    assert env.status == "partial" and env.data == {"total_usd": 4000.0, "priced_holdings": 1, "unpriced_holdings": 1, "spam_hidden": 0}
    assert env.coverage == {"attempted": 2, "successful": 1, "missing": ["1 holding(s) not valued: unlisted"]}


def test_the_validator_warns_when_a_gap_is_not_disclosed():
    partial = evidence.Evidence(tool="mobula_token_bundle", status="partial", coverage={"attempted": 26, "successful": 8, "missing": ["funding source of 18 of 25 early buyers"]},
                                errors=[{"operation": "wallet/funding", "reason": "lookup failed", "count": 18}])
    clean = "Bundle evidence: none found in the sample; the launch looks organic. Source: Mobula."
    honest = "Bundle evidence: inconclusive -- 18 of 25 funding lookups failed, so a shared funder cannot be ruled out."
    trajectory = {"tool_name_0": "mobula_token_bundle", "observation_0": "x"}
    warned = validate_answer("is it bundled", clean, trajectory, "research", evidence=[partial])
    ok = validate_answer("is it bundled", honest, trajectory, "research", evidence=[partial])
    assert next(c for c in warned.checks if c.name == "coverage").status == "warn"
    assert "8 of 26 lookups succeeded" in next(c for c in warned.checks if c.name == "coverage").detail
    assert next(c for c in ok.checks if c.name == "coverage").status == "ok"
    none = validate_answer("is it bundled", clean, trajectory, "research")
    assert next(c for c in none.checks if c.name == "coverage").status == "not_applicable"


def test_a_job_keeps_the_envelopes_its_tools_recorded():
    jobs.reset_for_test()

    async def handler(job, ctx):
        evidence.partial("some_tool", {"kind": "token", "id": "x"}, {"n": 1}, [evidence.source("p")], attempted=3, successful=1, missing=["two"])
        return {"answer": "done"}
    jobs.register("ev", handler)
    job = asyncio.run(jobs.create("ev", {}, user_id="u1"))
    out = asyncio.run(jobs.run(job))
    assert out["status"] == "succeeded" and out["evidence"][0]["tool"] == "some_tool" and out["evidence"][0]["coverage"]["successful"] == 1
    assert "data" not in out["evidence"][0]                                   # the public form, never the bulk
    jobs._handlers.pop("ev", None)


def test_a_spam_holding_never_inflates_the_total(monkeypatch, turn):
    """Hidden as spam, an "airdrop" worth a made-up $1e20 still set the
    total: Mobula's own total counted it. The total is the sum of what is shown."""
    payload = {"total_wallet_balance": 1e20, "assets": [
        {"asset": {"id": 0, "name": "Airdrop claim", "symbol": "AIR", "blockchains": ["Ethereum"]}, "estimated_balance": 1e20, "price": 1e12, "token_balance": 1},
        {"asset": {"id": 1, "name": "Ethereum", "symbol": "ETH", "blockchains": ["Ethereum"]}, "estimated_balance": 4000.0, "price": 4000.0, "token_balance": 1}]}
    monkeypatch.setattr(mobula_wallet, "_get", lambda path, params: payload)
    card = mobula_wallet.portfolio("portfolio of 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
    assert "**Total**: $4.00K" in card and "$100,000" not in card
    assert evidence.collected()[0].data["total_usd"] == 4000.0


# ---- anchors (2026-09-23): what each observation rests on ----

def test_the_bundle_envelope_anchors_every_shared_funder_claim_to_its_funding_transaction(monkeypatch, turn):
    buyers = [{"address": f"W{i:02d}" + "A" * 30, "firstHoldingDate": "2026-09-18T12:00:05Z", "initialAmount": "1", "currentBalance": "0"} for i in range(4)]
    buyers += [{"address": f"S{i:02d}" + "A" * 30, "firstHoldingDate": f"2026-09-18T12:00:{10 + i:02d}Z", "initialAmount": "1", "currentBalance": "1"} for i in range(4)]
    monkeypatch.setattr(mobula_meme, "_get_v1", lambda path, params: buyers)
    funding = {b["address"]: {"from": "FUNDER" + "F" * 30, "txHash": f"TX{i}" + "h" * 40, "date": "2026-09-18T11:59:00Z"} for i, b in enumerate(buyers[:4])}
    monkeypatch.setattr(mobula_meme, "_get", lambda path, params: funding.get(params["wallet"]))
    analysis = mobula_meme._bundle_analysis(FART, "solana")
    env = mobula_meme.bundle_evidence(analysis)
    txs = [a for a in env.anchors if a.kind == "tx"]
    assert len(txs) == 4 and txs[0].ref.startswith("TX0") and txs[0].at == "2026-09-18T11:59:00Z" and txs[0].provider == "mobula" and "funded by FUNDER" in txs[0].note
    # a cluster that is not counted (a burn address, an exchange) still anchors its transactions, marked as such
    burn = {b["address"]: {"from": "1" * 32 + "4", "txHash": f"BURN{i}" + "h" * 40, "date": "2026-09-18T11:58:00Z"} for i, b in enumerate(buyers[4:])}
    monkeypatch.setattr(mobula_meme, "_get", lambda path, params: {**funding, **burn}.get(params["wallet"]))
    env2 = mobula_meme.bundle_evidence(mobula_meme._bundle_analysis(FART, "solana"))
    burn_txs = [a for a in env2.anchors if a.kind == "tx" and a.ref.startswith("BURN")]
    assert len(burn_txs) == 4 and all(a.note.endswith(", not counted") for a in burn_txs)
    assert txs[0].link() == f"https://solscan.io/tx/{txs[0].ref}"
    records = [a for a in env.anchors if a.kind == "record"]
    assert len(records) == 4 and all(r.note == "first held in a shared second" for r in records)
    card = mobula_meme._render_bundle(analysis)
    assert "| Funding transactions |" in card and "[TX0h…hhhh](https://solscan.io/tx/TX0h" in card and "+1" in card
    assert env.public()["anchors"][0]["kind"] == "tx" and env.anchored()


def test_the_holders_and_trades_envelopes_anchor_their_rows(monkeypatch, turn):
    holders = [{"walletAddress": f"H{i}" + "A" * 40, "percentageOfTotalSupply": 10 - i, "labels": [], "lastTradeAt": "2026-09-20T00:00:00Z"} for i in range(3)]
    trades = [{"transactionHash": f"T{i}" + "x" * 60, "date": "2026-09-22T10:00:00Z", "type": "buy", "baseTokenAmountUSD": 12.5, "platform": "Raydium",
               "baseToken": {"symbol": "F"}, "quoteToken": {"symbol": "SOL"}, "baseTokenAmount": 1, "quoteTokenAmount": 1, "swapSenderAddress": "S" * 40} for i in range(2)]
    monkeypatch.setattr(mobula_meme, "_get", lambda path, params: holders if "holder" in path else trades)
    mobula_meme.token_holders(f"top holders of {FART} on solana")
    try:
        _REAL_TRADES(f"latest trades {FART} on solana")
    except Exception:
        pass                                                                          # the card's renderer is not under test here
    by_tool = {e.tool: e for e in evidence.collected()}
    assert [a.kind for a in by_tool["mobula_token_holders"].anchors] == ["record"] * 3 and by_tool["mobula_token_holders"].anchors[0].at == "2026-09-20T00:00:00Z"
    assert "mobula_token_trades" in by_tool and [a.kind for a in by_tool["mobula_token_trades"].anchors] == ["tx", "tx"]
    assert by_tool["mobula_token_trades"].anchors[0].note == "buy $12.50 on Raydium"


def test_anchors_are_capped_and_the_public_form_carries_them():
    env = evidence.Evidence(tool="t", status="complete")
    for i in range(80):
        env.add_anchor(evidence.tx(f"h{i}", "p", "solana"))
    assert len(env.anchors) == evidence.MAX_ANCHORS and len(env.public()["anchors"]) == evidence.MAX_ANCHORS
    assert env.public()["interpreter"] == env.interpreter


def test_the_wallet_envelope_anchors_its_record_and_each_shown_holding(monkeypatch, turn):
    payload = {"total_wallet_balance": 5000.0, "assets": [
        {"asset": {"id": 1, "name": "Ethereum", "symbol": "ETH", "blockchains": ["Ethereum"]}, "estimated_balance": 4000.0, "price": 4000.0, "token_balance": 1},
        {"asset": {"id": 2, "name": "USD Coin", "symbol": "USDC", "blockchains": ["Base"]}, "estimated_balance": 1000.0, "price": 1.0, "token_balance": 1000}]}
    monkeypatch.setattr(mobula_wallet, "_get", lambda path, params: payload)
    mobula_wallet.portfolio("portfolio of 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
    env = evidence.collected()[0]
    assert [a.kind for a in env.anchors] == ["record"] * 3 and env.anchors[0].note == "wallet portfolio record"
    assert env.anchors[1].ref.endswith(":ETH") and env.anchors[1].chain == "ethereum" and "priced $4.00K" in env.anchors[1].note


def test_a_pasted_address_beside_forensics_words_is_a_token_not_a_wallet():
    from app.nodes import research
    mint = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"
    assert research._detect_wallet_request(f"bundle check {mint} on solana") is None
    assert research._detect_wallet_request(f"who sniped {mint}") is None
    assert research._detect_wallet_request(f"check {mint}") is not None                       # a bare check on an address is still a wallet
    assert research._detect_wallet_request(f"wallet portfolio of {mint}") is not None         # a wallet word wins outright

"""The evidence envelope (app/evidence.py): tools record status, sources,
coverage and errors beside their cards; the validator warns when a gap
is not disclosed; a job keeps what its tools recorded."""
import asyncio

import pytest

from app import evidence, jobs, mobula_meme, mobula_wallet
from app.answer_validator import validate_answer
from app.provider_router import NoData

FART = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"


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

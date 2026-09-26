"""Every recorded episode passes once in the ordinary suite: the end state, not the prose."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("episode_eval", ROOT / "scripts" / "episode_eval.py")
episode_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(episode_eval)
EPISODES = [e for e in json.load(open(ROOT / "evals" / "episodes" / "cases.json"))["episodes"] if not e.get("live_only")]


@pytest.mark.parametrize("episode", EPISODES, ids=[e["id"] for e in EPISODES])
def test_episode_passes(episode):
    result = episode_eval.run_episode(episode)
    assert result["ok"], result["failures"]


def test_live_goal_does_not_count_a_name_only_in_the_research_trail():
    episode = {"goal": {"pipeline": "research_loop", "must_contain": ["Synthetix"],
                        "live_verified_candidate": "Synthetix"}}
    out = {"pipeline": "research_loop", "answer": "No verified answer. Research trail: Synthetix was a lead.",
           "trajectory": {"research_loop": {"coverage": {"conditions": ["native collateral"],
                                                          "records": [{"candidate": "Synthetix", "requirement": "native collateral",
                                                                       "status": "unknown", "source_url": "", "passage": ""}]}}}}
    assert "lacks complete page-backed condition coverage" in " ".join(episode_eval._judge(episode, out, [], live=True))
    out["trajectory"]["research_loop"]["coverage"]["records"][0].update(
        candidate="Synthetix (historical V2)", status="supported",
        source_url="https://docs.example.org/primary", passage="The native token is locked as collateral.")
    assert episode_eval._judge(episode, out, [], live=True) == []

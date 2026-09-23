"""Every recorded episode passes k=2 times: the end state, not the prose."""
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
def test_episode_passes_twice(episode):
    runs = [episode_eval.run_episode(episode) for _ in range(2)]
    assert all(r["ok"] for r in runs), [r["failures"] for r in runs if not r["ok"]]

"""A turn that detached from a job promised the answer in the conversation:
the browser polls the job and refreshes when it settles (review, 2026-09-22)."""
from tests.test_ui_swap_flow import run_case


def test_the_browser_watches_a_detached_job_and_refreshes_on_settle():
    out = run_case("detached_job_refreshes_the_conversation")
    assert out["loadError"] is None
    assert any("/jobs/job-1" in c for c in out["calls"]), out["calls"]
    assert any("/chat/history" in c for c in out["calls"]), out["calls"]     # hydrateHistory ran after the settle


def test_a_finished_message_with_a_job_id_starts_the_watch():
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text()
    assert "function finishAgentMessage(el, data) {\n  if(data.job_id)watchJob(data.job_id);" in script

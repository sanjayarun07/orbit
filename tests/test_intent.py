from app.intent import current_information_intent
from app.web_search import append_web_sources


def test_current_information_routes_to_research_for_any_topic():
    assert current_information_intent("What is the latest news in robotics?") == "research"
    assert current_information_intent("Give me today's Formula 1 news") == "research"
    assert current_information_intent("Who is the current CEO of Acme?") == "research"
    assert current_information_intent("bitcoin price now") == "research"


def test_static_questions_do_not_force_live_research():
    assert current_information_intent("Explain proof of history") is None


def test_wallet_and_trade_requests_keep_specialized_routes():
    assert current_information_intent("Show my current portfolio") is None
    assert current_information_intent("Swap SOL at the current price") is None


def test_web_sources_survive_agent_synthesis():
    trajectory = {
        "tool_name_0": "web_search",
        "observation_0": "Verified by [Python](https://python.org/downloads/).",
        "tool_name_1": "finish",
    }
    answer = append_web_sources("Python 3.14 is current.", trajectory)
    assert "[Python](https://python.org/downloads/)" in answer


def test_existing_source_is_not_duplicated():
    answer = "See [Python](https://python.org/downloads/)."
    trajectory = {"tool_name_0": "web_search", "observation_0": answer}
    assert append_web_sources(answer, trajectory) == answer


def test_bare_provider_url_is_appended_as_a_clickable_source():
    trajectory = {
        "tool_name_0": "perplexity_web_search",
        "observation_0": "Primary filing: https://www.sec.gov/example.htm",
    }
    answer = append_web_sources("Grounded summary.", trajectory)
    assert "[www.sec.gov](https://www.sec.gov/example.htm)" in answer

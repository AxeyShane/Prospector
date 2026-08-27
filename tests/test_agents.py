"""Routing decides what the user pays for and how good the answers are, so it
is worth testing directly rather than trusting it to work out."""

import pytest

from prospector import agents


@pytest.fixture
def both(monkeypatch):
    monkeypatch.setenv("LOCAL_BASE_URL", "http://127.0.0.1:8779/v1")
    monkeypatch.setenv("LOCAL_MODEL", "qwen2.5-3b")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    for name in agents.AGENTS:
        monkeypatch.delenv(f"ROUTE_{name.upper()}", raising=False)


def test_every_agent_in_the_order_exists_and_is_described():
    for name in ("planner",) + agents.ORDER:
        agent = agents.get(name)
        assert agent.friendly and agent.desc
        assert agent.tier in (agents.BULK, agents.REASONING)
        assert agent.workers >= 1
        if agent.uses_ai:
            assert agent.role, f"{name} calls the AI but has no role"


def test_judgement_goes_to_the_cloud_and_bulk_stays_local(both):
    assert agents.route("planner") == "cloud"
    assert agents.route("qualify") == "cloud"
    assert agents.route("classify") == "local"
    assert agents.route("people") == "local"
    assert agents.route("discover") == "local"


def test_no_cloud_key_falls_everything_back_to_this_pc(monkeypatch, both):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert agents.route("qualify") == "local"
    assert agents.route("classify") == "local"


def test_no_local_engine_sends_everything_to_the_cloud(monkeypatch, both):
    monkeypatch.delenv("LOCAL_MODEL")
    assert agents.route("classify") == "cloud"
    assert agents.route("qualify") == "cloud"


def test_a_user_override_wins(monkeypatch, both):
    agents.set_override("qualify", "local")
    assert agents.route("qualify") == "local"
    agents.set_override("classify", "cloud")
    assert agents.route("classify") == "cloud"
    agents.set_override("qualify", "")
    assert agents.route("qualify") == "cloud"


def test_an_override_pointing_at_a_missing_engine_is_ignored(monkeypatch, both):
    # Pinning an agent to the cloud must not break the run when the key is gone.
    monkeypatch.delenv("OPENROUTER_API_KEY")
    agents.set_override("classify", "cloud")
    assert agents.route("classify") == "local"
    agents.set_override("classify", "")


def test_the_routing_table_covers_every_agent(both):
    rows = agents.routing_table()
    assert len(rows) == len(agents.ORDER) + 1     # + planner
    assert {r["name"] for r in rows} == set(agents.ORDER) | {"planner"}
    # Stages that never call a model are always shown as running here.
    assert all(r["where"] == "this PC" for r in rows if not r["uses_ai"])


def test_search_heavy_agents_are_throttled():
    # These issue several web searches per company; running them wide open gets
    # the free search endpoint to block the whole run.
    assert agents.get("qualify").workers <= 3
    assert agents.get("qualify").uses_search is True
    assert agents.get("classify").workers > agents.get("qualify").workers


def test_the_table_admits_when_nothing_is_set_up(monkeypatch):
    # route() still has to return something for internal callers, but telling
    # the user an agent runs "cloud" when there is no key is simply false.
    for var in ("LOCAL_BASE_URL", "LOCAL_MODEL", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    rows = agents.routing_table()
    ai_rows = [r for r in rows if r["uses_ai"]]
    assert ai_rows and all(r["where"] == "not set up" for r in ai_rows)
    # The ones that never call a model are honest either way.
    assert all(r["where"] == "this PC" for r in rows if not r["uses_ai"])

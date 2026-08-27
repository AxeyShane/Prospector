import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    """Point every test at a throwaway project, never the user's real one."""
    from prospector import config

    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "SHARED_DIR", tmp_path / "shared")
    monkeypatch.setenv("PROSPECTOR_PROJECT", "pytest")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    config.refresh_paths()
    config.ensure_dirs()

    from prospector import database
    database.close_connection()
    database.init_db()
    yield
    database.close_connection()


@pytest.fixture
def sample_plan():
    from prospector.planner import normalise_plan

    return normalise_plan({
        "title": "Mining suppliers",
        "objective": "Indian crushing equipment makers already selling into Australia.",
        "target_profile": "A manufacturer with a named Australian distributor.",
        "target_leads": 12,
        "regions": ["Australia", "USA"],
        "discovery_queries": ["top crusher manufacturers india",
                              "crushing equipment suppliers india directory"],
        "relevant_categories": ["Crushing and screening"],
        "excluded_categories": ["Consultancy"],
        "qualification_criteria": [
            {"name": "Sells into Australia", "description": "Named AU distributor or customer",
             "weight": 5},
            {"name": "Mining capable", "description": "Equipment used in mining", "weight": 3},
        ],
        "priority_roles": ["Managing Director", "Export Director"],
        "people_per_company": 3,
    }, "Indian crusher makers selling into Australia")

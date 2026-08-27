"""Every CLI command must at least run.

`cli.py` was silently left importing functions that no longer existed, and the
test suite passed anyway because nothing imported it. These are cheap and would
have caught that immediately.
"""

import pytest
from typer.testing import CliRunner

from prospector.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "prospector" in result.stdout


def test_help_lists_every_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("serve", "plan", "load", "run", "status", "export",
                    "ai", "doctor", "projects"):
        assert command in result.stdout, f"{command} missing from help"


def test_doctor_runs_and_reports_both_engines():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Local AI" in result.stdout
    assert "Cloud AI" in result.stdout


def test_status_with_no_plan_says_what_to_do_next():
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "No plan yet" in result.stdout


def test_status_with_a_plan_lists_the_agents(sample_plan):
    from prospector import config

    config.save_plan(sample_plan)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Scout" in result.stdout
    assert "Judge" in result.stdout


def test_ai_shows_the_routing_table():
    result = runner.invoke(app, ["ai"])
    assert result.exit_code == 0, result.output
    assert "This computer" in result.stdout
    assert "Judge" in result.stdout


def test_ai_route_pins_an_agent():
    result = runner.invoke(app, ["ai", "--route", "qualify=local"])
    assert result.exit_code == 0, result.output
    assert "qualify now runs: local" in result.stdout

    from prospector import agents
    agents.set_override("qualify", "")


def test_ai_route_rejects_an_unknown_agent():
    result = runner.invoke(app, ["ai", "--route", "nonsense=local"])
    assert result.exit_code == 1
    assert "Unknown agent" in result.stdout


def test_run_rejects_an_unknown_agent():
    result = runner.invoke(app, ["run", "nonsense"])
    assert result.exit_code == 1
    assert "Unknown agent" in result.stdout


def test_run_dry_run_needs_no_ai(sample_plan):
    from prospector import config

    config.save_plan(sample_plan)
    result = runner.invoke(app, ["run", "--dry-run"])
    assert result.exit_code == 0, result.output


def test_export_builds_a_workbook(tmp_path, sample_plan):
    from prospector import config

    config.save_plan(sample_plan)
    out = tmp_path / "out.xlsx"
    result = runner.invoke(app, ["export", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_load_reads_a_text_list(tmp_path):
    listing = tmp_path / "companies.txt"
    listing.write_text("Acme Crushers Ltd\nBharat Wire Ropes Limited\n", encoding="utf-8")

    result = runner.invoke(app, ["load", str(listing)])
    assert result.exit_code == 0, result.output

    from prospector.database import get_stats
    assert get_stats()["total"] == 2


def test_projects_lists_the_active_one():
    result = runner.invoke(app, ["projects"])
    assert result.exit_code == 0, result.output
    assert "pytest" in result.stdout

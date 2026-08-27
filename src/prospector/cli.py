"""Prospector CLI.

Running `prospector` with no arguments opens the app -- that is the front door
for anyone who is not comfortable in a terminal. The subcommands exist for
scripting and for debugging one agent in isolation.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from prospector import __version__

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s - %(levelname)s - %(message)s",
                    datefmt="%H:%M:%S")

app = typer.Typer(
    name="prospector",
    help="Describe the leads you want; Prospector finds, researches and qualifies them.",
    no_args_is_help=False,
    invoke_without_command=True,
)
console = Console()


def _bootstrap() -> None:
    from prospector.config import ensure_dirs, load_env
    from prospector.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]prospector[/bold] {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit.",
                                 callback=_version_callback, is_eager=True),
    project: Optional[str] = typer.Option(None, "--project", "-p",
                                          help="Work on a specific project."),
) -> None:
    """Prospector - prompt-driven lead generation."""
    if project:
        os.environ["PROSPECTOR_PROJECT"] = project
        from prospector import config
        config.refresh_paths()
    if ctx.invoked_subcommand is None:
        serve()


@app.command()
def serve(
    port: int = typer.Option(8740, "--port", help="Port to serve the app on."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser."),
) -> None:
    """Open the Prospector app in your browser (the easy way to use this)."""
    _bootstrap()
    from prospector.webui import serve_app

    serve_app(port=port, open_browser=not no_browser)


@app.command()
def plan(prompt: str = typer.Argument(..., help="Describe the leads you want.")) -> None:
    """Turn a prompt into a research plan."""
    _bootstrap()
    from prospector import config
    from prospector.database import init_db
    from prospector.planner import make_plan

    result = make_plan(prompt)
    slug = config.create_project(result["title"])
    config.set_active_project(slug)
    os.environ["PROSPECTOR_PROJECT"] = slug
    config.refresh_paths()
    config.ensure_dirs()
    init_db()
    config.save_plan(result)

    console.print()
    console.print(Panel.fit(f"[bold]{result['title']}[/bold]", border_style="blue"))
    console.print(f"  {result['objective']}\n")
    console.print(f"  Target:   {result['target_leads']} companies")
    console.print(f"  Regions:  {', '.join(result['regions']) or 'not restricted'}")
    console.print(f"  Project:  {slug}\n")
    console.print("  [bold]Searches[/bold]")
    for query in result["discovery_queries"]:
        console.print(f"    - {query}")
    console.print("\n  [bold]Qualifies on[/bold]")
    for crit in result["qualification_criteria"]:
        console.print(f"    - {crit['name']} (weight {crit['weight']}): {crit['description']}")
    console.print(f"\n  Edit it at: {config.PLAN_PATH}")
    console.print("  Then run:   [bold]prospector run[/bold]")


@app.command()
def load(list_file: Path = typer.Argument(..., help=".json, .csv, .tsv or .txt")) -> None:
    """Load a company list you already have."""
    _bootstrap()
    from prospector.stages.seed import run_seed

    run_seed(list_file)


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None, help="Agents to run, or 'all'. Order is fixed however you list them."),
    workers: Optional[int] = typer.Option(None, "--workers", "-w",
                                          help="Override every agent's worker count."),
    limit: Optional[int] = typer.Option(None, "--limit", "-l",
                                        help="At most N companies per agent (a test run)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would run."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Where to write the .xlsx"),
) -> None:
    """Run the agents."""
    _bootstrap()
    from prospector.pipeline import run_pipeline

    try:
        result = run_pipeline(stages=list(stages) if stages else None, workers=workers,
                              limit=limit, export_path=out, dry_run=dry_run)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if result.get("errors"):
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show progress so far."""
    _bootstrap()
    from prospector.config import load_plan
    from prospector.database import get_stats
    from prospector.pipeline import pending_summary

    stats = get_stats()
    current = load_plan()

    if not current:
        console.print(Panel.fit(
            "No plan yet.\n\n"
            'Make one with:  [bold]prospector plan "the leads you want"[/bold]\n'
            "Or just run:    [bold]prospector[/bold]  to use the app.",
            border_style="yellow"))
        return

    console.print()
    console.print(f"  [bold]{current.get('title')}[/bold] - {stats['total']} leads")

    table = Table(header_style="bold")
    table.add_column("Agent", style="bold")
    table.add_column("Done", justify="right")
    table.add_column("Waiting", justify="right")

    pending = pending_summary()
    for label, done, name in (
        ("Scout (found)", stats["total"], "discover"),
        ("Locator (websites)", stats["resolved"], "resolve"),
        ("Reader (pages read)", stats["crawled"], "crawl"),
        ("Sorter (classified)", stats["classified"], "classify"),
        ("Judge (qualified)", stats["qualified"], "qualify"),
        ("Analyst (profiled)", stats["profiled"], "profile"),
        ("Connector (contacts)", stats["with_people"], "people"),
    ):
        table.add_row(label, str(done), str(pending.get(name, 0)))
    console.print(table)

    if stats["by_qualification"]:
        console.print("\n  Qualification:")
        for level, count in stats["by_qualification"]:
            console.print(f"    {level:<28s} {count}")


@app.command()
def export(out: Optional[Path] = typer.Option(None, "--out", "-o")) -> None:
    """Build the spreadsheet from whatever has been researched so far."""
    _bootstrap()
    from prospector.exporter import build_workbook

    console.print(f"  [green]Workbook written:[/green] {build_workbook(out)}")


@app.command()
def ai(
    setup_local: bool = typer.Option(False, "--local", help="Set up local AI on this PC."),
    cloud_key: Optional[str] = typer.Option(None, "--key", help="Save a cloud API key."),
    model: Optional[str] = typer.Option(None, "--model", help="Model to use."),
    route: Optional[str] = typer.Option(None, "--route",
                                        help="Pin an agent, e.g. --route qualify=local"),
) -> None:
    """Check or set up where the AI runs."""
    _bootstrap()
    from prospector import agents, config, engine, hardware
    from prospector.llm import describe

    if route:
        name, _, where = route.partition("=")
        name, where = name.strip(), where.strip()
        if name not in agents.AGENTS:
            console.print(f"  [red]Unknown agent:[/red] {name}. "
                          f"One of: {', '.join(agents.AGENTS)}")
            raise typer.Exit(1)
        agents.set_override(name, where)
        config.write_env({f"ROUTE_{name.upper()}": where})
        console.print(f"  {name} now runs: {where or 'automatically'}")
        return

    if cloud_key:
        config.write_env({"OPENROUTER_API_KEY": cloud_key,
                          "OPENROUTER_MODEL": model or "google/gemini-2.5-flash"})
        console.print(f"  [green]Cloud key saved.[/green] Using {describe()}")
        return

    report = hardware.report()
    console.print()
    console.print(Panel.fit("[bold]This computer[/bold]", border_style="blue"))
    console.print(f"  {report['summary']}")
    console.print(f"  {report['recommendation']['reason']}\n")
    console.print(f"  Local AI already running here: "
                  f"{'yes' if engine.detect_running() else 'no'}")
    console.print(f"  Currently using: {describe()}\n")

    table = Table(header_style="bold")
    table.add_column("Agent", style="bold")
    table.add_column("Job")
    table.add_column("Runs")
    for row in agents.routing_table():
        table.add_row(row["friendly"], row["desc"],
                      row["where"] + (" (pinned)" if row["overridden"] else ""))
    console.print(table)

    if setup_local:
        def show(event):
            if event.get("message"):
                console.print(f"  {event['message']}")

        settings = engine.setup(show, model_id=model or "")
        config.write_env(settings)
        console.print("  [green]Local AI ready.[/green]")


@app.command()
def doctor() -> None:
    """Check the setup and say plainly what is missing."""
    _bootstrap()
    from prospector import config
    from prospector.database import get_stats
    from prospector.llm import cloud_ready, describe, local_ready

    ok, bad = "[green]OK[/green]", "[red]MISSING[/red]"
    console.print()
    console.print(Panel.fit(f"[bold]Prospector {__version__}[/bold]", border_style="blue"))
    console.print(f"  Project:     {config.get_active_project()}")
    console.print(f"  Data folder: {config.APP_DIR}")
    console.print(f"  Plan:        {ok if config.PLAN_PATH.exists() else bad}")
    console.print(f"  Local AI:    {ok if local_ready() else '[yellow]not set up[/yellow]'}")
    console.print(f"  Cloud AI:    {ok if cloud_ready() else '[yellow]not set up[/yellow]'}")
    console.print(f"  In use:      {describe()}")

    for module in ("httpx", "bs4", "openpyxl", "flask", "dotenv"):
        try:
            __import__(module)
            console.print(f"  {module:<12s} {ok}")
        except ImportError:
            console.print(f"  {module:<12s} {bad}  - reinstall Prospector")

    console.print(f"\n  Leads loaded: {get_stats()['total']}")
    if not (local_ready() or cloud_ready()):
        console.print("\n  [yellow]Next step:[/yellow] run [bold]prospector[/bold] and "
                      "set up the AI on the AI screen.")


@app.command()
def projects(
    use: Optional[str] = typer.Option(None, "--use", "-u", help="Switch to this project."),
) -> None:
    """List projects, or switch between them. Each has its own database."""
    from prospector.config import get_active_project, list_projects, set_active_project

    if use:
        if use not in list_projects():
            console.print(f"  [red]No such project:[/red] {use}")
            raise typer.Exit(1)
        set_active_project(use)
        console.print(f"  Switched to: [bold]{use}[/bold]")
        return

    active = get_active_project()
    console.print()
    for name in list_projects():
        console.print(f"  {'[green]*[/green]' if name == active else ' '} {name}")


if __name__ == "__main__":
    app()

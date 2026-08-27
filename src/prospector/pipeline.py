"""Pipeline orchestrator -- runs the agents in order.

    discover -> resolve -> crawl -> classify -> qualify -> profile -> people -> exporter

Each agent drains the work the previous one created. The database is the queue,
so an interrupted run -- closed laptop, dropped network, Stop pressed -- resumes
exactly where it stopped. Nothing is ever redone.

Concurrency comes from the agent registry rather than a single global setting,
because the right number of workers is a property of the agent: `qualify`
issues six web searches per company and gets blocked at eight, `classify` is
pure text and is happy there.

`on_event` lets the app watch progress without parsing console output.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from prospector import agents
from prospector.config import ensure_dirs, load_env, load_plan
from prospector.llm import (
    BudgetExceeded, check_budget, reset_spend, spend_so_far,
)
from prospector.websearch import reset_health, search_health
from prospector.database import get_connection, get_stats, init_db

log = logging.getLogger(__name__)
console = Console()

ORDER = agents.ORDER


def meta(name: str) -> dict:
    agent = agents.get(name)
    return {"desc": agent.desc, "friendly": agent.friendly}


_cancel = threading.Event()


def request_cancel() -> None:
    _cancel.set()


def clear_cancel() -> None:
    _cancel.clear()


def is_cancelled() -> bool:
    return _cancel.is_set()


# ---------------------------------------------------------------------------
# Pending work
# ---------------------------------------------------------------------------

def _pending_sql(name: str) -> str | None:
    from prospector.stages import (
        classify, crawl, outreach, people, profile, qualify, resolve,
    )

    return {
        "resolve": resolve.PENDING_SQL,
        "crawl": crawl.PENDING_SQL,
        "classify": classify.PENDING_SQL,
        "qualify": qualify.pending_sql(),
        "profile": profile.pending_sql(),
        "people": people.pending_sql(),
        "outreach": outreach.pending_sql(),
    }.get(name)


def count_pending(name: str) -> int:
    if name == "discover":
        plan = load_plan()
        target = int(plan.get("target_leads", 0) or 0)
        have = get_connection().execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        return max(target - have, 0)

    sql = _pending_sql(name)
    if sql is None:
        return 0
    counting = sql.replace("SELECT * FROM", "SELECT COUNT(*) FROM", 1)
    if " ORDER BY " in counting:
        counting = counting.split(" ORDER BY ")[0]
    try:
        return get_connection().execute(counting).fetchone()[0]
    except Exception:  # noqa: BLE001 - a count must never break a run
        log.exception("pending count failed for %s", name)
        return 0


def pending_summary() -> dict[str, int]:
    return {name: count_pending(name) for name in ORDER if name != "exporter"}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _run_agent(name: str, limit: int | None, export_path=None) -> dict:
    from prospector.exporter import run_export
    from prospector.stages import (
        classify, crawl, discover, outreach, people, profile, qualify, resolve,
    )

    workers = agents.get(name).workers

    if name == "discover":
        return discover.run_discover(load_plan())
    if name == "resolve":
        return resolve.run_resolve(workers=workers, limit=limit)
    if name == "crawl":
        return crawl.run_crawl(workers=workers, limit=limit)
    if name == "classify":
        return classify.run_classify(workers=workers, limit=limit)
    if name == "qualify":
        return qualify.run_qualify(workers=workers, limit=limit)
    if name == "profile":
        return profile.run_profile(workers=workers, limit=limit)
    if name == "people":
        return people.run_people(workers=workers, limit=limit)
    if name == "outreach":
        return outreach.run_outreach(workers=workers, limit=limit)
    if name == "exporter":
        return run_export(export_path)
    raise ValueError(f"Unknown agent: {name}")


def resolve_stages(names: list[str] | None) -> list[str]:
    """Expand 'all' / None and put the requested agents in execution order."""
    if not names or "all" in names:
        return list(ORDER)

    # "export" is the old name for the exporter agent; accept it rather than
    # failing a command someone has in a script.
    names = ["exporter" if n == "export" else n for n in names]
    unknown = [n for n in names if n not in ORDER]
    if unknown:
        raise ValueError(
            f"Unknown agent(s): {', '.join(unknown)}. "
            f"Available: {', '.join(ORDER)}, all"
        )
    return [n for n in ORDER if n in names]


SENDER_PROFILE_MISSING = (
    "Fill in \"Who you are and what you offer\" on the plan. Without it every "
    "first message comes out as generic boilerplate, so none get written at all."
)


def preflight(stages: list[str]) -> list[str]:
    """Problems that would make this run fail, in words a person can act on."""
    from prospector.llm import cloud_ready, local_ready

    problems: list[str] = []
    plan = load_plan()

    if not plan:
        problems.append(
            "No research plan yet. Describe the leads you want, and press "
            "Build the plan."
        )
    elif "discover" in stages and not plan.get("discovery_queries"):
        have = get_connection().execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        if have == 0:
            problems.append(
                "The plan has no search queries and no company list has been "
                "uploaded, so there is nothing to research yet."
            )

    needs_ai = any(agents.get(s).uses_ai for s in stages if s in agents.AGENTS)
    if needs_ai and not (local_ready() or cloud_ready()):
        problems.append(
            "No AI set up yet. Press the AI button at the top and either set up "
            "AI on this PC, or paste a cloud key."
        )

    # Blocking only when the user asked for the drafts specifically. Asking for
    # the whole pipeline and getting refused outright because of one empty field
    # would be worse than the bug it fixes -- the research is still worth having.
    # `run_pipeline` drops the stage with a warning in that case instead.
    if (plan and stages == ["outreach"]
            and not (plan.get("sender_profile") or "").strip()):
        problems.append(SENDER_PROFILE_MISSING)

    return problems


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(
    stages: list[str] | None = None,
    workers: int | None = None,
    limit: int | None = None,
    export_path=None,
    dry_run: bool = False,
    on_event: Callable[[dict], None] | None = None,
    quiet: bool = False,
) -> dict:
    """Run agents in order. `workers` overrides every agent's own setting."""
    load_env()
    ensure_dirs()
    init_db()
    clear_cancel()

    ordered = resolve_stages(stages)
    plan = load_plan()
    title = plan.get("title", "Lead research")

    if workers:
        for name in ordered:
            if name in agents.AGENTS:
                agents.AGENTS[name].workers = max(1, min(workers, 8))

    def emit(event: dict) -> None:
        if on_event:
            try:
                on_event(event)
            except Exception:  # noqa: BLE001 - a broken listener must not stop work
                log.exception("on_event listener failed")

    if not quiet:
        console.print()
        console.print(Panel.fit(f"[bold]Prospector[/bold] - {title}", border_style="blue"))
        for name in ordered:
            agent = agents.get(name)
            where = "this PC" if not agent.uses_ai else (
                "this PC" if agents.route(name) == "local" else "cloud")
            console.print(f"  {agent.friendly:<11s} {agent.desc:<44s} [dim]{where}[/dim]")

    problems = preflight(ordered)
    if problems:
        for p in problems:
            if not quiet:
                console.print(f"  [red]Cannot start:[/red] {p}")
        emit({"type": "error", "problems": problems})
        return {"stages": [], "errors": {"preflight": "; ".join(problems)}, "elapsed": 0.0}

    if dry_run:
        pending = pending_summary()
        if not quiet:
            console.print("\n  [yellow]DRY RUN[/yellow] - work waiting:")
            for name in ordered:
                if name == "exporter":
                    console.print(f"    {name:<10s} (always runs)")
                else:
                    console.print(f"    {name:<10s} {pending.get(name, 0)} companies")
        return {"stages": [], "errors": {}, "elapsed": 0.0, "pending": pending}

    # Without a sender profile the Drafter refuses for every single lead. Left
    # alone, a user waited forty minutes, saw "Research complete", and found the
    # First Contact tab empty with nothing on screen explaining why. The research
    # is still worth doing, so the stage is dropped and said out loud instead.
    if "outreach" in ordered and not (load_plan().get("sender_profile") or "").strip():
        ordered = [n for n in ordered if n != "outreach"]
        emit({"type": "warning", "stage": "outreach",
              "message": SENDER_PROFILE_MISSING})
        if not quiet:
            console.print(f"\n  [yellow]Skipping first messages:[/yellow] "
                          f"{SENDER_PROFILE_MISSING}")

    results: list[dict] = []
    errors: dict[str, str] = {}
    started = time.time()
    reset_spend()
    reset_health()
    spend_limit = float(load_plan().get("spend_limit_usd") or 0)
    emit({"type": "run_start", "stages": ordered, "spend_limit": spend_limit})

    for name in ordered:
        if is_cancelled():
            emit({"type": "cancelled", "stage": name})
            if not quiet:
                console.print("\n  [yellow]Stopped[/yellow] - progress is saved, "
                              "run again to continue.")
            break

        agent = agents.get(name)
        pending = count_pending(name) if name != "exporter" else 0
        where = "local" if not agent.uses_ai else agents.route(name)
        emit({"type": "stage_start", "stage": name, "friendly": agent.friendly,
              "pending": pending, "where": where})

        if not quiet:
            console.print(f"\n{'=' * 68}")
            console.print(f"  [bold]{agent.friendly.upper()}[/bold] - {agent.desc}")
            if name != "exporter":
                console.print(f"  {pending} to process, {agent.workers} at a time")
            console.print(f"{'=' * 68}")

        if name != "exporter" and pending == 0:
            results.append({"stage": name, "status": "nothing to do", "elapsed": 0.0})
            emit({"type": "stage_done", "stage": name, "status": "nothing to do"})
            continue

        t0 = time.time()
        try:
            result = _run_agent(name, limit, export_path)
            status = result.get("status", "ok")
            # Reported in words rather than as a bare count. "outreach: ok (60
            # failed)" is not something a non-technical user can act on, and it
            # was the only trace left when the whole stage failed for one
            # fixable reason.
            if result.get("failed"):
                reason = result.get("common_error") or ""
                status = f"{result['failed']} of {result.get('processed', 0)} could not be done"
                if reason:
                    status += f" - {reason}"
            if result.get("no_match"):
                status = (f"{result.get('ok', 0)} found, "
                          f"{result['no_match']} with no confident match")
        except Exception as exc:  # noqa: BLE001 - report it, do not crash the run
            status = f"error: {exc}"
            errors[name] = str(exc)
            result = {}
            log.exception("Agent '%s' crashed", name)
            if not quiet:
                console.print(f"  [red]Failed:[/red] {exc}")

        elapsed = time.time() - t0
        results.append({"stage": name, "status": status, "elapsed": elapsed,
                        **{k: v for k, v in result.items() if k != "status"}})
        emit({"type": "stage_done", "stage": name, "status": status,
              "elapsed": elapsed, "result": result, "spend": spend_so_far()})

        # A soft ceiling on cloud spend. It pauses the run rather than ending
        # it: everything found is on disk, and pressing Continue after raising
        # the limit picks up exactly where this left off.
        try:
            check_budget(spend_limit)
        except BudgetExceeded as exc:
            errors["budget"] = str(exc)
            emit({"type": "error", "stage": name, "message": str(exc),
                  "recoverable": True, "spend": spend_so_far()})
            if not quiet:
                console.print(f"\n  [yellow]Paused:[/yellow] {exc}")
            break

        # Stop the moment web search stops answering, rather than spending the
        # next hour making blocked requests and finishing with an empty
        # spreadsheet and no explanation. Everything found so far is on disk.
        if agent.uses_search:
            health = search_health()
            if health["blocked"]:
                message = (
                    "Web search has stopped responding - the free search "
                    "service has most likely blocked us for searching too "
                    "quickly, or this computer has lost its connection. "
                    "Everything found so far is saved. Wait about ten minutes "
                    "and press Continue.")
                errors["search"] = message
                emit({"type": "error", "stage": name, "message": message,
                      "recoverable": True})
                if not quiet:
                    console.print(f"\n  [red]Stopped:[/red] {message}")
                break

    total_elapsed = time.time() - started

    if not quiet:
        table = Table(title="Run summary", show_header=True, header_style="bold")
        table.add_column("Agent", style="bold")
        table.add_column("Result")
        table.add_column("Time", justify="right")
        for r in results:
            style = "red" if str(r["status"]).startswith("error") else "green"
            table.add_row(agents.get(r["stage"]).friendly,
                          f"[{style}]{str(r['status'])[:40]}[/{style}]",
                          f"{r['elapsed']:.1f}s")
        console.print()
        console.print(table)

        stats = get_stats()
        console.print(f"\n  Leads: {stats['total']}   websites: {stats['resolved']}   "
                      f"classified: {stats['classified']}   qualified: {stats['qualified']}")

    emit({"type": "run_done", "elapsed": total_elapsed, "errors": errors})
    return {"stages": results, "errors": errors, "elapsed": total_elapsed}

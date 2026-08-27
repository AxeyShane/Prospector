"""Shared batch runner for pipeline stages.

Every stage does the same three things: select the rows that still need work,
process them (optionally across threads), and record success or a counted
failure. Putting that here means each stage file contains only its actual
logic and its prompt, and means a bug in the retry/error handling gets fixed
once rather than six times.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from rich.console import Console
from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TextColumn, TimeElapsedColumn,
)

from prospector.database import MAX_ATTEMPTS, bump_attempt, get_connection

log = logging.getLogger(__name__)
console = Console()

# Each worker thread needs its own SQLite connection; get_connection() is
# already thread-local, so this just guarantees it is called on the worker.
_thread_local = threading.local()


def worker_conn() -> sqlite3.Connection:
    return get_connection()


def select_pending(sql: str, params: tuple = (), limit: int | None = None) -> list[sqlite3.Row]:
    """Fetch rows needing work, excluding ones that already failed too often."""
    conn = get_connection()
    if limit:
        sql = f"{sql} LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def run_batch(
    stage: str,
    rows: list[sqlite3.Row],
    handler: Callable[[sqlite3.Row], str],
    workers: int = 1,
    label: str = "",
) -> dict:
    """Run `handler` over `rows`, counting outcomes and recording failures.

    The handler returns a short status string for the progress line, or raises
    to signal failure. A raised exception is counted against the row's attempt
    budget so a company with a permanently broken website stops being retried
    on every subsequent run instead of blocking the pipeline forever.
    """
    if not rows:
        return {"processed": 0, "ok": 0, "failed": 0}

    label = label or stage
    ok = failed = 0
    lock = threading.Lock()

    from prospector.pipeline import is_cancelled

    skipped = 0

    def process(row: sqlite3.Row) -> tuple[str, bool, str]:
        nonlocal skipped
        key = row["company_key"]
        name = row["company_name"]

        # Cancellation used to be checked only *between* stages. Qualify runs
        # for the better part of an hour, so pressing Stop showed "finishing the
        # current step first" and then did nothing visible for that whole hour.
        # A queued row that has not started yet can simply be dropped.
        if is_cancelled():
            skipped += 1
            return name, True, "stopped"
        try:
            status = handler(row) or "ok"
            return name, True, status
        except Exception as exc:  # noqa: BLE001 - failure is data, not a crash
            log.debug("%s failed for %s: %s", stage, name, exc, exc_info=True)
            try:
                bump_attempt(key, stage, str(exc), conn=worker_conn())
            except Exception:  # noqa: BLE001
                log.exception("could not record %s failure for %s", stage, name)
            return name, False, str(exc)[:80]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task(f"  {label}", total=len(rows))

        if workers <= 1:
            for row in rows:
                name, success, status = process(row)
                ok, failed = (ok + 1, failed) if success else (ok, failed + 1)
                progress.update(task, advance=1,
                                description=f"  {label} [dim]{name[:40]}[/dim]")
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(process, row): row for row in rows}
                for future in as_completed(futures):
                    name, success, status = future.result()
                    with lock:
                        ok, failed = (ok + 1, failed) if success else (ok, failed + 1)
                    progress.update(task, advance=1,
                                    description=f"  {label} [dim]{name[:40]}[/dim]")

    if failed:
        console.print(f"  [yellow]{failed} failed[/yellow], {ok} succeeded "
                      f"(failures retried next run, up to {MAX_ATTEMPTS} attempts)")
    return {"processed": len(rows), "ok": max(ok - skipped, 0),
            "failed": failed, "skipped": skipped}


def relevance_filter(min_relevance: str) -> str:
    """SQL fragment restricting a stage to companies worth the LLM spend.

    Returned as a literal IN-list rather than bound parameters so it can be
    embedded in the pending-count queries the pipeline uses for streaming
    mode, which take no parameters of their own.
    """
    ladder = ["High", "Medium", "Low", "Not relevant"]
    if min_relevance not in ladder:
        min_relevance = "Medium"
    keep = ladder[: ladder.index(min_relevance) + 1]
    quoted = ", ".join(f"'{r}'" for r in keep)
    return f"relevance IN ({quoted})"

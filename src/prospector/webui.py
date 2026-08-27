"""Prospector web app -- the front door.

Prompt in, spreadsheet out. Four screens:

    1. Describe the leads you want
    2. Review the plan the AI wrote (editable -- you see exactly what it decided
       to search for before anything runs)
    3. Choose where the AI runs. Nothing here names a product: the user is
       shown "on this PC" and "in the cloud", never the engine behind either.
    4. Watch the agents work, then download the spreadsheet

Served on 127.0.0.1 only. Nothing here is exposed to a network.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, request, send_file

from prospector import __version__, agents, config, engine, hardware
from prospector.database import (
    MAX_ATTEMPTS, get_connection, get_stats, init_db, upsert_lead,
)
from prospector.llm import spend_so_far

log = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

app = Flask(__name__)

_lock = threading.Lock()
_STATE: dict = {
    "running": False, "stage": "", "friendly": "", "where": "", "error": "",
    "problems": [], "warnings": [], "cancelled": False, "started_at": 0.0,
    "engine_busy": False, "engine_percent": 0, "engine_message": "",
    "engine_error": "", "planning": False,
}
_LOG: deque[str] = deque(maxlen=300)

# Described by what they do for the user, not by their model id. The id is the
# option's value and never appears on screen: a picker labelled "Model" listing
# "meta-llama/llama-3.3-70b-instruct" is meaningless to someone who does not
# know what any of this is, and asks them to make a choice they cannot make.
CLOUD_MODELS = [
    ("google/gemini-2.5-flash", "Balanced - fast, accurate and cheap (recommended)"),
    ("google/gemini-2.0-flash-001", "Cheapest - lowest cost per company"),
    ("anthropic/claude-3.5-haiku", "Most careful - strictest about evidence"),
    ("openai/gpt-4o-mini", "Alternative - a different provider"),
    ("meta-llama/llama-3.3-70b-instruct", "Open - runs on open-source models"),
]

# Roughly what one hundred companies costs on each, for the picker. Deliberately
# rounded up: a number that undershoots is worse than no number.
CLOUD_COST_HINT = {
    "google/gemini-2.5-flash": "about 30-60 cents per hundred companies",
    "google/gemini-2.0-flash-001": "about 15-30 cents per hundred companies",
    "anthropic/claude-3.5-haiku": "about $1-2 per hundred companies",
    "openai/gpt-4o-mini": "about 20-40 cents per hundred companies",
    "meta-llama/llama-3.3-70b-instruct": "about 15-30 cents per hundred companies",
}


def friendly_error(exc: object, context: str = "") -> str:
    """Turn anything that went wrong into a sentence with a next step.

    Every user-visible error used to be `str(exc)`, so the screen showed things
    like `HTTPStatusError: Client error '404 Not Found' for url
    'https://huggingface.co/Qwen/...q4_k_m.gguf?download=true'` -- four separate
    pieces of jargon and no indication of what to do. The full text still goes
    to the log file, where it is useful; what reaches the screen is this.
    """
    text = str(exc).strip()
    low = text.lower()
    kind = type(exc).__name__.lower()

    # Anything with a technical fingerprint is translated, never passed through.
    # An earlier version asked the opposite question -- "does this look plain?"
    # -- and let `getaddrinfo failed` reach the screen unchanged.
    technical = ("traceback", "errno", "exception", "0x", "getaddrinfo", "ssl",
                 "socket", "gguf", "http://", "https://", ".exe", "winerror",
                 "\\", "none type", "nonetype", "attribute")
    looks_written = (
        text[:1].isupper() and text.endswith((".", "!", "?"))
        and not any(k in low for k in technical)
    )
    if looks_written:
        return text

    if "permission denied" in low and (".xlsx" in low or "export" in low):
        return ("The spreadsheet is open in Excel. Close it and press Build "
                "spreadsheet again.")
    if "no space left" in low or "disk full" in low:
        return ("This computer has run out of disk space. Free some up and try "
                "again - nothing found so far is lost.")
    if "download" in low or "gguf" in low or "huggingface" in low:
        return ("The AI model could not be downloaded. Check the internet "
                "connection and press Set up again, or use the cloud option "
                "instead.")
    if ("connect" in kind or "network" in kind
            or any(k in low for k in ("connectionerror", "connecterror",
                                      "getaddrinfo", "name or service not known",
                                      "temporary failure", "unreachable"))):
        return ("This computer cannot reach the internet at the moment. Check "
                "the connection and press Continue.")
    if "timeout" in low or "timed out" in low:
        return ("Something took too long to answer. Press Continue - everything "
                "found so far is saved.")
    if "404" in low and "model" in low:
        return ("That AI option is not available right now. Pick a different "
                "one on the AI screen.")
    if "401" in low or "403" in low:
        return ("The cloud service would not accept the key. Open the AI screen "
                "and paste it again.")
    if "402" in low or "insufficient" in low:
        return ("The cloud account is out of credit. Add credit, choose a "
                "cheaper option, or move the work to this PC on the AI screen.")

    where = f" while {context}" if context else ""
    return (f"Something went wrong{where}. Nothing found so far is lost - press "
            f"Continue to try again.")

EXAMPLE_PROMPTS = [
    "Indian manufacturers of crushing and screening equipment that already "
    "supply into Australia or other major mining markets, and who to meet at each.",
    "Commercial cleaning contractors in the UK with more than 50 staff who "
    "service healthcare or education sites.",
    "European suppliers of industrial conveyor belts that hold food-grade "
    "certification and sell into North America.",
]


def emit_log(message: str) -> None:
    if message:
        _LOG.append(f"{time.strftime('%H:%M:%S')}  {message}")


def _progress() -> dict:
    from prospector.pipeline import count_pending

    # The whole page polls this endpoint. A database that is not ready yet used
    # to 500 the request, which the browser reads as "lost contact" -- so a
    # first-run hiccup looked like the app had crashed.
    try:
        init_db()
    except Exception:  # noqa: BLE001
        log.debug("could not ensure the database", exc_info=True)

    stats = get_stats()
    plan = config.load_plan()
    target = int(plan.get("target_leads", 0) or 0)

    done_map = {
        "discover": stats["total"],
        "resolve": stats["resolved"] + stats["resolve_failed"],
        "crawl": stats["crawled"],
        "classify": stats["classified"],
        "qualify": stats["qualified"],
        "profile": stats["profiled"],
        # `people_searched`, not `with_people`. The denominator counts rows the
        # stage has still to visit, so pairing it with a numerator that counts
        # rows where a contact was *found* read 12/112 at the halfway point and
        # shrank the total as the run went on.
        "people": stats.get("people_searched", 0),
        "outreach": stats["drafted"],
    }

    rows, done_sum, total_sum = [], 0, 0
    for name in agents.ORDER:
        if name == "exporter":
            continue
        agent = agents.get(name)
        done = done_map.get(name, 0)
        total = target if name == "discover" else done + count_pending(name)
        if name in ("resolve", "classify"):
            total = max(total, stats["total"])
        capped = min(done, total) if total else done
        rows.append({"stage": name, "friendly": agent.friendly, "desc": agent.desc,
                     "done": capped, "total": total,
                     "where": "this PC" if not agent.uses_ai else
                              ("this PC" if agents.route(name) == "local" else "cloud")})
        done_sum += capped
        total_sum += max(total, 0)

    # Companies that hit the retry limit or fell below the relevance floor are
    # excluded from every stage's pending query, so they used to vanish from
    # both the numerator and the denominator: the bar read 100% and said
    # "Research complete" while a third of the list had been dropped, with
    # nothing anywhere saying so.
    skipped = get_connection().execute(
        "SELECT COUNT(*) FROM leads WHERE COALESCE(resolve_attempts, 0) >= ? "
        "OR COALESCE(classify_attempts, 0) >= ? OR COALESCE(qualify_attempts, 0) >= ?",
        (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS)).fetchone()[0]

    percent = int(round(100 * done_sum / total_sum)) if total_sum else 0

    started = _STATE.get("started_at") or 0
    elapsed = int(time.time() - started) if started and _STATE.get("running") else 0
    remaining = 0
    if elapsed > 30 and 0 < percent < 100:
        remaining = int(elapsed * (100 - percent) / percent)

    return {"rows": rows,
            "percent": percent,
            "leads": stats["total"], "qualified": stats["qualified"],
            "strong": _count_best(plan),
            "skipped": skipped,
            "elapsed_seconds": elapsed,
            "remaining_seconds": remaining,
            "spend": spend_so_far()}


def _count_best(plan: dict) -> int:
    labels = [lv["label"] for lv in plan.get("qualification_levels", [])]
    if not labels:
        return 0
    return get_connection().execute(
        "SELECT COUNT(*) FROM leads WHERE qualification_level = ?", (labels[0],)
    ).fetchone()[0]


def _latest_export() -> str:
    try:
        files = sorted(Path(config.EXPORT_DIR).glob("*.xlsx"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        return str(files[0]) if files else ""
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

def _on_event(event: dict) -> None:
    kind = event.get("type")
    with _lock:
        if kind == "stage_start":
            _STATE["stage"] = event["stage"]
            _STATE["friendly"] = event.get("friendly", event["stage"])
            _STATE["where"] = event.get("where", "")
            where = "on this PC" if event.get("where") == "local" else "in the cloud"
            emit_log(f"{event.get('friendly')} started ({where}) - "
                     f"{event.get('pending', 0)} to go")
        elif kind == "stage_done":
            # The agent's friendly name, never the internal stage id. A log line
            # reading "outreach: ok (60 failed)" is not something a
            # non-technical user can act on -- and it was the only trace left
            # when a whole stage failed for one fixable reason.
            friendly = _friendly_stage(event.get("stage", ""))
            emit_log(f"{friendly}: {event.get('status', 'done')}")
        elif kind == "warning":
            message = event.get("message", "")
            _STATE["warnings"] = [w for w in _STATE.get("warnings", []) if w != message]
            _STATE["warnings"].append(message)
            emit_log(message)
        elif kind == "error":
            _STATE["problems"] = event.get("problems", [])
            message = event.get("message", "")
            if message:
                _STATE["error"] = message
                emit_log(message)
            for p in _STATE["problems"]:
                emit_log(f"Cannot start: {p}")
        elif kind == "cancelled":
            _STATE["cancelled"] = True
            emit_log("Stopped. Progress is saved - press Continue to resume.")
        elif kind == "run_done":
            emit_log(f"Run finished in {event.get('elapsed', 0):.0f}s")


def _friendly_stage(name: str) -> str:
    from prospector import agents
    agent = agents.AGENTS.get(name)
    return agent.friendly if agent else (name or "Research")


def _run_worker(workers: int | None, limit: int | None) -> None:
    from prospector.pipeline import run_pipeline

    try:
        result = run_pipeline(workers=workers, limit=limit,
                              on_event=_on_event, quiet=True)
        with _lock:
            errors = result.get("errors", {})
            # Errors carry their own plain wording where the pipeline wrote one;
            # anything else is translated. The internal stage key is not shown.
            _STATE["error"] = " ".join(friendly_error(v) for v in errors.values())
    except Exception as exc:  # noqa: BLE001 - surface it, do not die silently
        log.exception("pipeline crashed")
        friendly = friendly_error(exc, "researching")
        with _lock:
            _STATE["error"] = friendly
        emit_log(friendly)
    finally:
        with _lock:
            _STATE.update({"running": False, "stage": "", "friendly": "", "where": ""})


def _engine_progress(event: dict) -> None:
    with _lock:
        _STATE["engine_percent"] = int(event.get("percent", 0) or 0)
        _STATE["engine_message"] = event.get("message", "")
    if event.get("status") in ("done", "starting"):
        emit_log(event.get("message", ""))


def _engine_worker(model_id: str, url_override: str) -> None:
    try:
        settings = engine.setup(_engine_progress, model_id=model_id,
                                url_override=url_override)
        config.write_env(settings)
        from prospector.llm import reset_clients
        reset_clients()
        with _lock:
            _STATE["engine_error"] = ""
            _STATE["engine_message"] = "Local AI is ready."
        emit_log("Local AI is ready.")
    except Exception as exc:  # noqa: BLE001
        log.exception("local AI setup failed")
        friendly = friendly_error(exc, "setting up AI on this PC")
        with _lock:
            _STATE["engine_error"] = friendly
        emit_log(friendly)
    finally:
        with _lock:
            _STATE["engine_busy"] = False


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/state")
def api_state():
    from prospector.llm import cloud_ready, describe, local_ready

    with _lock:
        state = dict(_STATE)
    plan = config.load_plan()
    return jsonify({
        "version": __version__,
        "project": config.get_active_project(),
        "plan": plan,
        "has_plan": bool(plan),
        "local_ready": local_ready(),
        "cloud_ready": cloud_ready(),
        "ai_ready": local_ready() or cloud_ready(),
        "engine_label": describe(),
        "cloud_models": [{"id": m, "label": lb} for m, lb in CLOUD_MODELS],
        "cloud_model": os.environ.get("OPENROUTER_MODEL", ""),
        "routing": agents.routing_table(),
        "progress": _progress(),
        "state": state,
        "warnings": state.get("warnings", []),
        "engine_detail": engine.describe_engine(),
        "log": list(_LOG)[-120:],
        "export_ready": bool(_latest_export()),
        "examples": EXAMPLE_PROMPTS,
        "data_dir": str(config.APP_DIR),
    })


@app.get("/api/engine/detect")
def api_engine_detect():
    """What this PC can do, and whether anything is already running here."""
    report = hardware.report()
    running = engine.detect_running()
    report["already_running"] = bool(running)
    report["models"] = [{"id": m["id"], "label": m["label"],
                         "size_gb": m["size_gb"], "ram_gb": m["ram_gb"],
                         "quality": m["quality"]} for m in engine.MODELS]
    report["installed"] = bool(engine.find_server())
    return jsonify(report)


@app.post("/api/engine/local")
def api_engine_local():
    with _lock:
        if _STATE["engine_busy"]:
            return jsonify({"ok": False, "message": "Already setting up."})
        _STATE.update({"engine_busy": True, "engine_error": "", "engine_percent": 0,
                       "engine_message": "Checking this computer..."})

    data = request.get_json(force=True, silent=True) or {}
    threading.Thread(target=_engine_worker,
                     args=(data.get("model", ""), (data.get("model_url") or "").strip()),
                     daemon=True, name="prospector-engine").start()
    return jsonify({"ok": True})


@app.post("/api/engine/cloud")
def api_engine_cloud():
    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("api_key") or "").strip()
    model = (data.get("model") or "").strip() or "google/gemini-2.5-flash"

    updates = {"OPENROUTER_MODEL": model}
    if key:
        updates["OPENROUTER_API_KEY"] = key
    config.write_env(updates)

    from prospector.llm import reset_clients
    reset_clients()
    # The plain label, not the model id.
    label = next((lbl for mid, lbl in CLOUD_MODELS if mid == model), "").split(" - ")[0]
    emit_log(f"Cloud AI ready{f' ({label.lower()})' if label else ''}.")
    return jsonify({"ok": True, "cost_hint": CLOUD_COST_HINT.get(model, "")})


@app.post("/api/engine/test")
def api_engine_test():
    """One real call per configured engine, so "ready" means ready."""
    from prospector.llm import LLMClient, LLMError, _endpoint, cloud_ready, local_ready

    data = request.get_json(force=True, silent=True) or {}
    if data.get("api_key"):
        config.write_env({"OPENROUTER_API_KEY": data["api_key"].strip(),
                          "OPENROUTER_MODEL": data.get("model")
                          or "google/gemini-2.5-flash"})
        from prospector.llm import reset_clients
        reset_clients()

    results = []
    for where, ready in (("local", local_ready()), ("cloud", cloud_ready())):
        if not ready:
            continue
        label = "This PC" if where == "local" else "Cloud"
        try:
            base, model, key, timeout = _endpoint(where)
            client = LLMClient(base, model, key, min(timeout, 300), where=where)
            reply = client.chat([{"role": "user", "content": "Reply with the word OK."}],
                                max_tokens=5)
            client.close()
            results.append(f"{label}: working" if reply.strip()
                           else f"{label}: answered with nothing")
        except LLMError as exc:
            # LLMError messages are written for the user already.
            results.append(f"{label}: {exc}")
        except Exception as exc:  # noqa: BLE001
            results.append(f"{label}: {friendly_error(exc, 'testing the connection')}")

    if not results:
        return jsonify({"ok": False,
                        "message": "Nothing is set up yet. Set up local AI, or paste "
                                   "a cloud key."})
    ok = all("working" in r for r in results)
    return jsonify({"ok": ok, "message": " | ".join(results)})


@app.post("/api/engine/route")
def api_engine_route():
    """Pin one agent to this PC or the cloud."""
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("agent") or "").strip()
    where = (data.get("where") or "").strip()
    if name not in agents.AGENTS:
        return jsonify({"ok": False, "message": f"Unknown agent: {name}"})

    agents.set_override(name, where)
    config.write_env({f"ROUTE_{name.upper()}": where or ""})
    from prospector.llm import reset_clients
    reset_clients()
    return jsonify({"ok": True, "routing": agents.routing_table()})


@app.post("/api/plan/build")
def api_plan_build():
    from prospector.llm import LLMError
    from prospector.planner import make_plan

    prompt = ((request.get_json(force=True, silent=True) or {}).get("prompt") or "").strip()

    # Building a plan creates and switches to a new project, which repoints
    # config.DB_PATH -- and `get_connection` resolves that at call time, so
    # worker threads would quietly start writing into the new, empty database.
    # Every `update()` after that matches zero rows and the results of however
    # long the run had been going are discarded without a word.
    with _lock:
        if _STATE["running"]:
            return jsonify({"ok": False, "message": (
                "Research is running. Press Stop first - your progress is "
                "saved - then start a new search.")})
        _STATE["planning"] = True
    try:
        plan = make_plan(prompt)
    except (ValueError, LLMError) as exc:
        return jsonify({"ok": False, "message": str(exc)})
    finally:
        with _lock:
            _STATE["planning"] = False

    # Anything uploaded before the plan was built lives in the project we are
    # about to leave. The upload control sits inside the prompt card, which is
    # exactly where a user does it first -- so "40 new companies added" was
    # followed by a run that found zero and re-discovered everything from
    # scratch. Carry the names across.
    carried: list[str] = []
    try:
        carried = [r[0] for r in get_connection().execute(
            "SELECT company_name FROM leads WHERE source_list NOT IN "
            "('discovered', '') OR source_list IS NULL").fetchall()]
    except Exception:  # noqa: BLE001
        log.debug("could not read uploaded leads before switching project",
                  exc_info=True)

    # A plan means a project. Move into its own folder so a second brief does
    # not land in the same database as the first.
    slug = config.create_project(plan.get("title") or prompt[:40])
    config.set_active_project(slug)
    os.environ["PROSPECTOR_PROJECT"] = slug
    config.refresh_paths()
    config.ensure_dirs()
    init_db()
    config.save_plan(plan)

    if carried:
        conn = get_connection()
        moved = sum(1 for name in carried
                    if upsert_lead(name, source_list="uploaded", conn=conn))
        if moved:
            emit_log(f"Kept the {moved} companies you uploaded.")

    emit_log(f"Plan ready: {plan['title']} - {len(plan['discovery_queries'])} searches, "
             f"target {plan['target_leads']} companies")
    return jsonify({"ok": True, "plan": plan, "project": slug})


@app.post("/api/plan/save")
def api_plan_save():
    from prospector.planner import normalise_plan

    data = (request.get_json(force=True, silent=True) or {}).get("plan") or {}
    plan = normalise_plan(data, data.get("prompt", ""))
    config.save_plan(plan)
    return jsonify({"ok": True, "plan": plan})


@app.post("/api/upload")
def api_upload():
    """Optional: start from a list you already have."""
    from prospector.stages.seed import load_names

    init_db()
    label = "pasted list"

    uploaded = request.files.get("file")
    if uploaded and uploaded.filename:
        suffix = Path(uploaded.filename).suffix.lower() or ".txt"
        tmp = Path(config.APP_DIR) / f"_upload{suffix}"
        uploaded.save(tmp)
        label = uploaded.filename
        try:
            names = load_names(tmp)
        except Exception as exc:  # noqa: BLE001
            log.info("upload failed", exc_info=True)
            return jsonify({"ok": False,
                            "message": f"Could not read {uploaded.filename}. It should "
                                       f"be a .csv, .txt, .tsv or .json file with one "
                                       f"company name per row."})
        finally:
            tmp.unlink(missing_ok=True)
    else:
        text = (request.form.get("names") or "").strip()
        if not text:
            return jsonify({"ok": False, "message": "Choose a file, or paste names."})
        names = [line.strip() for line in text.splitlines() if line.strip()]

    if not names:
        return jsonify({"ok": False, "message": "That file had no company names in it."})

    conn = get_connection()
    created = sum(1 for n in names if upsert_lead(n, source_list=label, conn=conn))
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    emit_log(f"Loaded {created} companies from {label}")
    return jsonify({"ok": True, "added": created, "total": total,
                    "message": f"{created} new companies added. {total} in total."})


@app.post("/api/start")
def api_start():
    from prospector.pipeline import clear_cancel, preflight, resolve_stages

    problems = preflight(resolve_stages(None))
    if problems:
        with _lock:
            _STATE["problems"] = problems
        return jsonify({"ok": False, "message": problems[0], "problems": problems})

    data = request.get_json(force=True, silent=True) or {}
    limit = int(data["limit"]) if data.get("limit") else None

    clear_cancel()
    # Checked and set in a single critical section. They were two separate lock
    # blocks with preflight between them, so two clicks a few hundred
    # milliseconds apart both passed the check and started two pipelines over
    # one SQLite file -- duplicated work, doubled cloud spend.
    with _lock:
        if _STATE["running"]:
            return jsonify({"ok": False, "message": "Already running."})
        _STATE.update({"running": True, "started_at": time.time(), "error": "",
                       "problems": [], "warnings": [], "cancelled": False})
    emit_log("Starting research" + (f", limited to {limit} per step" if limit else ""))

    threading.Thread(target=_run_worker, args=(None, limit), daemon=True,
                     name="prospector-run").start()
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    from prospector.pipeline import request_cancel

    request_cancel()
    emit_log("Stop requested - finishing the current step first.")
    return jsonify({"ok": True})


@app.post("/api/export")
def api_export():
    from prospector.exporter import build_workbook

    try:
        path = build_workbook()
    except Exception as exc:  # noqa: BLE001
        log.exception("export failed")
        return jsonify({"ok": False,
                        "message": friendly_error(exc, "building the spreadsheet")})
    emit_log(f"Spreadsheet built: {Path(path).name}")
    return jsonify({"ok": True, "name": Path(path).name})


@app.get("/api/download")
def api_download():
    path = _latest_export()
    if not path or not Path(path).exists():
        return jsonify({"ok": False, "message": "No spreadsheet yet."}), 404
    return send_file(path, as_attachment=True, download_name=Path(path).name)


@app.get("/api/results")
def api_results():
    from prospector.exporter import rank_score

    plan = config.load_plan()
    labels = [lv["label"] for lv in plan.get("qualification_levels", [])]
    rows = get_connection().execute(
        "SELECT * FROM leads WHERE qualification_level IS NOT NULL").fetchall()
    rows.sort(key=lambda r: -rank_score(r, labels))

    out = []
    for row in rows[:25]:
        try:
            people = json.loads(row["people_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            people = []
        out.append({
            "company": row["company_name"], "website": row["website"] or "",
            "category": row["category"] or "",
            "level": row["qualification_level"] or "",
            "rank": labels.index(row["qualification_level"])
                    if row["qualification_level"] in labels else 9,
            "score": row["qualification_score"] or 0,
            "evidence": (row["qualification_evidence"] or "").split("\n")[0][:200],
            "person": f"{people[0]['name']} ({people[0]['title']})" if people else "",
        })
    return jsonify({"rows": out, "labels": labels})


@app.get("/api/open-folder")
def api_open_folder():
    # The exports folder, not APP_DIR. The button is labelled "Open my files
    # folder" and used to open the internals -- the database, the .env, the page
    # cache, the logs -- rather than the spreadsheets the user was looking for.
    config.ensure_dirs()
    folder = str(config.EXPORT_DIR)
    try:
        if os.name == "nt":
            os.startfile(folder)  # noqa: S606 - local desktop convenience
        else:
            import subprocess
            subprocess.Popen(["xdg-open", folder])  # noqa: S607
    except Exception:  # noqa: BLE001
        # The caller shows the path so there is still a way forward.
        return jsonify({"ok": False, "folder": folder,
                        "message": f"Could not open the folder. It is here:\n{folder}"})
    return jsonify({"ok": True, "folder": folder})


@app.get("/")
def index():
    return PAGE_HTML.replace("__VERSION__", __version__)


def _first_free_port(start: int, tries: int = 12) -> int:
    """A port nothing else is on. 0 when they are all taken."""
    import socket

    for candidate in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    return 0


def serve_app(port: int = 8740, open_browser: bool = True) -> None:
    config.refresh_paths()
    config.ensure_dirs()
    init_db()
    emit_log(f"Prospector {__version__} ready")

    # A busy port used to print "run: prospector serve --port 8741" -- a command
    # line instruction to someone who by definition cannot use one -- and the
    # browser opened anyway, onto a page that could not load.
    chosen = _first_free_port(port)
    if not chosen:
        print()
        print("  Prospector could not start: this computer is unusually busy on")
        print("  the addresses it uses. Restarting the computer will clear it.")
        print()
        return

    url = f"http://127.0.0.1:{chosen}/"
    print()
    print("  Prospector is running.")
    print(f"  Open this in your browser if it did not open by itself:  {url}")
    print("  Close this window when you are finished.")
    print()

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        app.run(host="127.0.0.1", port=chosen, debug=False, threaded=True)
    except OSError as exc:
        print(f"\n  Prospector could not start: {exc}")
        print("  It may already be open - check your browser tabs.")
    finally:
        engine.stop_server()


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
# Raw string on purpose: with a normal string Python eats the escape sequences
# in the JavaScript, and the page renders but silently stops responding.

PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prospector</title>
<style>
  :root { --bg:#0b1220; --panel:#151f33; --panel2:#1e2a42; --line:#2c3a56;
          --text:#e6edf7; --dim:#93a4bf; --accent:#5eb0ff; --ok:#4ade80;
          --warn:#fbbf24; --bad:#f87171; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.6 -apple-system,Segoe UI,Roboto,Arial,sans-serif; }
  header { padding:16px 26px; border-bottom:1px solid var(--line); display:flex;
           align-items:center; gap:12px; position:sticky; top:0; background:var(--bg); z-index:5; }
  header h1 { margin:0; font-size:19px; letter-spacing:.3px; }
  header h1 span { color:var(--accent); }
  header .meta { color:var(--dim); font-size:13.5px; }
  header .spacer { flex:1; }
  main { max-width:1080px; margin:0 auto; padding:24px 20px 80px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px;
          padding:22px 24px; margin-bottom:18px; }
  .card h2 { margin:0 0 4px; font-size:17px; }
  .card p.sub { margin:0 0 16px; color:var(--dim); font-size:14px; }
  label { display:block; font-size:13px; color:var(--dim); margin:12px 0 5px; }
  input[type=text], input[type=password], input[type=number], select, textarea {
    width:100%; background:#0a111f; color:var(--text); border:1px solid var(--line);
    border-radius:8px; padding:10px 12px; font-size:14px; font-family:inherit; }
  textarea { resize:vertical; }
  textarea.big { min-height:120px; font-size:15px; }
  button { background:var(--accent); color:#052034; border:0; border-radius:8px;
           padding:10px 18px; font-size:14px; font-weight:650; cursor:pointer; }
  button.ghost { background:var(--panel2); color:var(--text); border:1px solid var(--line); }
  button.big { font-size:17px; padding:15px 30px; width:100%; }
  button.stop { background:var(--bad); color:#3b0a0a; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:12px; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  .msg { margin-top:10px; font-size:13.5px; padding:9px 12px; border-radius:8px; display:none; }
  .msg.ok { display:block; background:rgba(74,222,128,.12); color:var(--ok);
            border:1px solid rgba(74,222,128,.3); }
  .msg.bad { display:block; background:rgba(248,113,113,.12); color:var(--bad);
             border:1px solid rgba(248,113,113,.3); }
  .msg.info { display:block; background:rgba(94,176,255,.1); color:var(--accent);
              border:1px solid rgba(94,176,255,.28); }
  .bar { height:9px; background:#0a111f; border-radius:99px; overflow:hidden;
         border:1px solid var(--line); }
  .bar > i { display:block; height:100%; width:0;
             background:linear-gradient(90deg,#5eb0ff,#4ade80); transition:width .4s; }
  .srow { display:grid; grid-template-columns:150px 1fr 86px 74px; gap:10px;
          align-items:center; padding:6px 0; font-size:13.5px; }
  .srow .nm { color:var(--dim); }
  .srow.active .nm { color:var(--accent); font-weight:650; }
  .srow .ct { text-align:right; color:var(--dim); font-variant-numeric:tabular-nums; }
  .srow .wh { text-align:right; color:var(--dim); font-size:11.5px; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
           gap:12px; margin:6px 0 4px; }
  .tile { background:var(--panel2); border-radius:10px; padding:13px 15px; }
  .tile b { display:block; font-size:23px; font-weight:700; }
  .tile span { color:var(--dim); font-size:12.5px; }
  .opt { border:1px solid var(--line); border-radius:10px; padding:16px; background:var(--panel2); }
  .opt h3 { margin:0 0 4px; font-size:15px; }
  .opt p { margin:0 0 8px; color:var(--dim); font-size:13px; }
  .opt.on { border-color:var(--ok); box-shadow:0 0 0 1px rgba(74,222,128,.35) inset; }
  .chip { display:inline-block; background:var(--panel2); border:1px solid var(--line);
          border-radius:99px; padding:4px 11px; font-size:12.5px; margin:3px 4px 0 0;
          color:var(--dim); cursor:pointer; }
  .chip:hover { border-color:var(--accent); color:var(--text); }
  pre.log { background:#060c16; border:1px solid var(--line); border-radius:8px;
            padding:11px 13px; max-height:200px; overflow:auto; font-size:12.5px;
            color:#9fb0c9; margin:14px 0 0; white-space:pre-wrap; }
  table { width:100%; border-collapse:collapse; margin-top:10px; font-size:13.5px; }
  th { text-align:left; color:var(--dim); font-weight:600; padding:8px 9px;
       border-bottom:1px solid var(--line); }
  td { padding:9px; border-bottom:1px solid #1f2c44; vertical-align:top; }
  td select { padding:4px 8px; font-size:12.5px; }
  .pill { display:inline-block; padding:2px 9px; border-radius:99px; font-size:11.5px;
          font-weight:650; white-space:nowrap; }
  .pill.l0 { background:rgba(74,222,128,.16); color:var(--ok); }
  .pill.l1 { background:rgba(251,191,36,.16); color:var(--warn); }
  .pill.l2 { background:rgba(148,163,184,.16); color:var(--dim); }
  .pill.l3, .pill.l9 { background:rgba(148,163,184,.1); color:var(--dim); }
  .crit { display:grid; grid-template-columns:1fr 2fr 70px; gap:8px; margin-top:8px; }
  a { color:var(--accent); }
  .hide { display:none !important; }
  .hint { color:var(--dim); font-size:12.5px; margin-top:6px; }
  .hint.warn { color:#b45309; }
  .req { color:#b45309; font-weight:500; }
  button.unsaved { border-color:#b45309; color:#b45309; }
  #offline { border-color:#b91c1c; }
  details summary { cursor:pointer; color:var(--dim); font-size:13.5px; margin-top:6px; }
</style>
</head>
<body>
<header>
  <h1>Prospect<span>or</span></h1>
  <span class="meta" id="hdrPlan"></span>
  <span class="spacer"></span>
  <span class="meta" id="hdrEngine"></span>
  <button class="ghost" id="btnEngineToggle">AI</button>
</header>

<main>

  <div class="card bad hide" id="offline">
    <h2>Lost contact with Prospector</h2>
    <p class="sub">The Prospector window may have been closed, or this computer
      went to sleep. Nothing is lost - everything found so far is saved. Open
      Prospector again from the Start menu and this page will reconnect.</p>
  </div>

  <div class="card" id="cardPrompt">
    <h2>What leads do you want?</h2>
    <p class="sub">Describe them the way you would to a new researcher. Say who they
      are, where, and what would make one worth contacting.</p>
    <textarea id="prompt" class="big" placeholder="Indian manufacturers of crushing and screening equipment that already supply into Australia or other major mining markets, and who to meet at each."></textarea>
    <div id="examples"></div>
    <div class="row">
      <button id="btnPlan">Build the plan</button>
      <span class="hint">Takes about 20 seconds.</span>
    </div>
    <div class="msg" id="planMsg"></div>
    <details>
      <summary>I already have a list of companies</summary>
      <div style="margin-top:10px">
        <input type="file" id="file" accept=".json,.csv,.tsv,.txt">
        <label for="names">Or paste company names, one per line</label>
        <textarea id="names"></textarea>
        <div class="row"><button class="ghost" id="btnUpload">Add these companies</button></div>
        <div class="msg" id="uploadMsg"></div>
      </div>
    </details>
  </div>

  <div class="card hide" id="cardEngine">
    <h2>Where the AI runs</h2>
    <p class="sub" id="engineSub">Checking this computer...</p>
    <div class="grid2">
      <div class="opt" id="optLocal">
        <h3>On this PC</h3>
        <p id="localWhy">Free and private. Nothing leaves this computer.</p>
        <div class="bar hide" id="engineBar"><i></i></div>
        <div class="row"><button id="btnLocal">Set up local AI</button></div>
        <div class="hint" id="localHint"></div>
      </div>
      <div class="opt" id="optCloud">
        <h3>In the cloud</h3>
        <p>Better at the judgement calls. Needs a free key from
          <a href="https://openrouter.ai/keys" target="_blank" rel="noreferrer">openrouter.ai/keys</a>.</p>
        <input type="password" id="apiKey" placeholder="sk-or-v1-...">
        <label for="cloudModel">Model</label>
        <select id="cloudModel"></select>
        <div class="row">
          <button class="ghost" id="btnCloud">Save cloud key</button>
          <button class="ghost" id="btnTest">Test both</button>
        </div>
      </div>
    </div>
    <div class="msg" id="engineMsg"></div>

    <p class="hint" style="margin-top:18px">Set up both and Prospector splits the work:
      the repetitive reading runs here for free, and the two decisions that matter go to
      the cloud. Either one on its own also works.</p>
    <table id="routeTable"><thead><tr><th>Agent</th><th>Job</th><th>Runs</th></tr></thead>
      <tbody></tbody></table>

    <details>
      <summary>Advanced</summary>
      <label for="modelUrl">Direct link to a model file, if the automatic download fails</label>
      <input type="text" id="modelUrl" placeholder="https://...">
      <pre class="log" id="hwDump" style="max-height:130px"></pre>
    </details>
  </div>

  <div class="card hide" id="cardPlan">
    <h2>The plan</h2>
    <p class="sub">Exactly what will be searched for and how leads get scored.
      Change anything that looks wrong, then start.</p>
    <div class="grid2">
      <div><label for="pTitle">Project name</label><input type="text" id="pTitle"></div>
      <div><label for="pTarget">How many companies to find</label>
        <input type="number" id="pTarget" min="5" max="1000"></div>
    </div>
    <label for="pObjective">What this is looking for</label>
    <textarea id="pObjective" style="min-height:60px"></textarea>
    <label for="pRegions">Regions (comma separated)</label>
    <input type="text" id="pRegions">

    <label for="pSender">Who you are and what you offer
      <span class="req">&mdash; needed for the first messages</span></label>
    <p class="hint" id="senderHint">Without this, no opening messages get written: a
      message that could have been sent to anybody is why cold emails go unanswered.
      The research still runs.</p>
    <textarea id="pSender" style="min-height:80px" placeholder="I represent Indian manufacturers of crushing and screening equipment looking for distribution in Australia. I handle introductions, not sales. Recent example: placed a screen-media supplier with two Queensland quarry groups."></textarea>
    <label for="pChannel">First contact by</label>
    <select id="pChannel">
      <option value="email">Email</option>
      <option value="linkedin">LinkedIn note</option>
      <option value="call">Phone opener</option>
    </select>
    <label for="pQueries">Web searches it will run (one per line)</label>
    <textarea id="pQueries" style="min-height:130px"></textarea>
    <label>What makes a lead qualify</label>
    <div id="pCriteria"></div>
    <div class="hint">Weight 1-5. Higher weight moves a matching company further up the call list.</div>
    <div class="grid2" style="margin-top:12px">
      <div><label for="pRoles">Who to find at each company (comma separated)</label>
        <textarea id="pRoles" style="min-height:70px"></textarea></div>
      <div><label for="pExcluded">Not interested in (comma separated)</label>
        <textarea id="pExcluded" style="min-height:70px"></textarea></div>
    </div>
    <div class="row">
      <button class="ghost" id="btnSavePlan">Save changes</button>
      <button class="ghost" id="btnRePrompt">Start over with a new prompt</button>
    </div>
    <div class="msg" id="savePlanMsg"></div>
  </div>

  <div class="card hide" id="cardRun">
    <h2 id="runTitle">Ready</h2>
    <p class="sub" id="runSub"></p>
    <div class="bar"><i id="barFill"></i></div>
    <div class="tiles">
      <div class="tile"><b id="tLeads">0</b><span>companies found</span></div>
      <div class="tile"><b id="tQual">0</b><span>qualified</span></div>
      <div class="tile"><b id="tStrong">0</b><span>strong matches</span></div>
    </div>
    <p class="sub" id="runFacts"></p>
    <div id="stages"></div>
    <div class="row" style="margin-top:16px"><button class="big" id="btnStart">Start research</button></div>
    <div class="row">
      <button class="stop hide" id="btnStop">Stop</button>
      <button class="ghost" id="btnBuild">Build spreadsheet now</button>
      <button class="ghost" id="btnDownload">Download spreadsheet</button>
      <button class="ghost" id="btnFolder">Open my files folder</button>
    </div>
    <div class="msg" id="runMsg"></div>
    <pre class="log" id="log"></pre>
  </div>

  <div class="card hide" id="cardResults">
    <h2>Best leads so far</h2>
    <p class="sub">Ranked by how well each matches your criteria.</p>
    <table id="tbl"><thead><tr><th>Company</th><th>What they do</th><th>Rating</th>
      <th>Why</th><th>Contact</th></tr></thead><tbody></tbody></table>
  </div>

</main>

<script>
const $ = id => document.getElementById(id);
let LAST = null, PLAN = null, engineOpen = false, engineTouched = false;

function show(el, on) { el.classList.toggle('hide', !on); }
function msg(el, text, kind) {
  el.className = 'msg ' + (kind || 'info');
  el.textContent = text || '';
  if (!text) el.className = 'msg';
}
function esc(s) {
  return (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
async function post(url, body, isForm) {
  const opts = { method:'POST' };
  if (isForm) opts.body = body;
  else { opts.headers = {'Content-Type':'application/json'}; opts.body = JSON.stringify(body||{}); }
  return (await fetch(url, opts)).json();
}

// The plan form is filled from the server exactly once per plan, and never
// again while the user is working in it.
//
// It used to be re-filled on every two-second poll, guarded only by a focus
// flag registered on eight of the fields. Choosing "LinkedIn note" snapped back
// to Email within two seconds; typing a criterion vanished mid-keystroke; and
// filling in the sender profile and clicking away reverted it to empty, so
// pressing Start then saved the empty field the user thought they had filled --
// silently destroying the one field the whole Drafter stage depends on.
let planFilledFor = null;
let planDirty = false;
let newSearch = false;

function fillPlan(p, force) {
  const stamp = JSON.stringify([p.title, p.objective, (p.discovery_queries||[]).length]);
  if (!force && (planDirty || planFilledFor === stamp)) { PLAN = p; return; }
  planFilledFor = stamp;
  planDirty = false;
  PLAN = p;
  $('pTitle').value = p.title || '';
  $('pTarget').value = p.target_leads || 60;
  $('pObjective').value = p.objective || '';
  $('pRegions').value = (p.regions||[]).join(', ');
  $('pSender').value = p.sender_profile || '';
  $('pChannel').value = p.outreach_channel || 'email';
  $('pQueries').value = (p.discovery_queries||[]).join('\n');
  $('pRoles').value = (p.priority_roles||[]).join(', ');
  $('pExcluded').value = (p.excluded_categories||[]).join(', ');
  const box = $('pCriteria'); box.innerHTML = '';
  (p.qualification_criteria||[]).forEach((c,i) => {
    const div = document.createElement('div');
    div.className = 'crit';
    div.innerHTML =
      '<input type="text" data-k="name" data-i="'+i+'" value="'+esc(c.name)+'">' +
      '<input type="text" data-k="description" data-i="'+i+'" value="'+esc(c.description)+'">' +
      '<input type="number" data-k="weight" data-i="'+i+'" min="1" max="5" value="'+(c.weight||3)+'">';
    box.appendChild(div);
  });
}

function mins(sec) {
  if (!sec) return '';
  if (sec < 90) return Math.round(sec) + ' seconds';
  const m = Math.round(sec / 60);
  if (m < 90) return m + ' minute' + (m === 1 ? '' : 's');
  const h = Math.floor(m / 60);
  return h + 'h ' + (m % 60) + 'm';
}

// A run can take an hour. "42%" on its own does not tell anyone whether to wait
// or go and do something else, and the start time was recorded and never shown.
function runFacts(pr, st) {
  const bits = [];
  if (st.running && pr.elapsed_seconds) {
    bits.push('Running for ' + mins(pr.elapsed_seconds));
    if (pr.remaining_seconds) bits.push('about ' + mins(pr.remaining_seconds) + ' left');
  }
  if (pr.spend && pr.spend.usd > 0.005) {
    bits.push('cloud cost so far $' + pr.spend.usd.toFixed(2));
  }
  if (pr.skipped) {
    bits.push(pr.skipped + ' compan' + (pr.skipped === 1 ? 'y' : 'ies') +
              ' set aside after repeated failures');
  }
  return bits.join(' \u00b7 ');
}

function readPlan() {
  const p = Object.assign({}, PLAN || {});
  p.title = $('pTitle').value.trim();
  p.target_leads = parseInt($('pTarget').value || '60', 10);
  p.objective = $('pObjective').value.trim();
  p.regions = $('pRegions').value.split(',').map(s=>s.trim()).filter(Boolean);
  p.sender_profile = $('pSender').value.trim();
  p.outreach_channel = $('pChannel').value;
  p.discovery_queries = $('pQueries').value.split('\n').map(s=>s.trim()).filter(Boolean);
  p.priority_roles = $('pRoles').value.split(',').map(s=>s.trim()).filter(Boolean);
  p.excluded_categories = $('pExcluded').value.split(',').map(s=>s.trim()).filter(Boolean);
  const crit = [];
  $('pCriteria').querySelectorAll('input').forEach(inp => {
    const i = +inp.dataset.i;
    crit[i] = crit[i] || {name:'',description:'',weight:3};
    crit[i][inp.dataset.k] = inp.dataset.k === 'weight' ? +inp.value : inp.value;
  });
  p.qualification_criteria = crit.filter(c => c && c.name);
  return p;
}

// Every control inside the plan card, including the ones added later for the
// criteria rows -- a hand-maintained list of ids is exactly how pChannel and
// the criteria inputs were left out of the old guard.
$('cardPlan').addEventListener('input', () => { planDirty = true; markUnsaved(); });
$('cardPlan').addEventListener('change', () => { planDirty = true; markUnsaved(); });

function markUnsaved() {
  const b = $('btnSavePlan');
  if (b) { b.textContent = 'Save changes'; b.classList.add('unsaved'); }
}

function markSaved() {
  planDirty = false;
  const b = $('btnSavePlan');
  if (b) { b.textContent = 'Saved'; b.classList.remove('unsaved'); }
}

function renderRouting(rows) {
  const tb = $('routeTable').querySelector('tbody');
  tb.innerHTML = '';
  rows.forEach(r => {
    const tr = document.createElement('tr');
    let cell;
    if (!r.uses_ai) {
      cell = '<span style="color:#93a4bf">this PC</span>';
    } else {
      cell = '<select data-agent="'+r.name+'">' +
        '<option value=""'   + (r.overridden ? '' : ' selected') + '>automatic (' + r.where + ')</option>' +
        '<option value="local"' + (r.overridden && r.where === 'this PC' ? ' selected' : '') + '>this PC</option>' +
        '<option value="cloud"' + (r.overridden && r.where === 'cloud' ? ' selected' : '') + '>cloud</option>' +
        '</select>';
    }
    tr.innerHTML = '<td><b>'+esc(r.friendly)+'</b></td><td>'+esc(r.desc)+'</td><td>'+cell+'</td>';
    tb.appendChild(tr);
  });
  tb.querySelectorAll('select').forEach(sel => {
    sel.onchange = async () => {
      await post('/api/engine/route', {agent: sel.dataset.agent, where: sel.value});
      tick();
    };
  });
}

function render(d) {
  LAST = d;
  const st = d.state, pr = d.progress;

  $('hdrPlan').textContent = d.has_plan ? d.plan.title : '';
  $('hdrEngine').textContent = d.ai_ready ? d.engine_label : 'AI not set up';

  if (!$('cloudModel').options.length) {
    d.cloud_models.forEach(m => {
      const o = document.createElement('option');
      o.value = m.id; o.textContent = m.label; $('cloudModel').appendChild(o);
    });
    if (d.cloud_model) $('cloudModel').value = d.cloud_model;
    $('examples').innerHTML = d.examples.map(e =>
      '<span class="chip">' + esc(e.slice(0,72)) + '...</span>').join('');
    $('examples').querySelectorAll('.chip').forEach((chip,i) => {
      chip.onclick = () => { $('prompt').value = d.examples[i]; };
    });
  }
  if (d.cloud_ready && !$('apiKey').placeholder.startsWith('saved')) {
    $('apiKey').placeholder = 'saved - paste again only to change it';
  }

  show($('cardPrompt'), !d.has_plan || newSearch);
  show($('cardPlan'), d.has_plan);
  show($('cardRun'), d.has_plan);
  // Never auto-hide a card the user is working in. Saving a cloud key flipped
  // ai_ready to true, and two seconds later the whole card evaporated -- taking
  // the "Cloud key saved" confirmation, the Test button and the local-AI
  // progress bar with it, mid-action.
  show($('cardEngine'), engineOpen || !d.ai_ready || engineTouched);
  $('optLocal').classList.toggle('on', d.local_ready);
  $('optCloud').classList.toggle('on', d.cloud_ready);
  renderRouting(d.routing || []);

  if (d.has_plan) fillPlan(d.plan);   // no-op once the user is typing
  const senderEmpty = d.has_plan && !$('pSender').value.trim();
  $('senderHint').classList.toggle('warn', senderEmpty);
  (d.warnings || []).forEach(w => msg($('savePlanMsg'), w, 'bad'));

  $('barFill').style.width = pr.percent + '%';
  $('runFacts').textContent = runFacts(pr, st);
  $('tLeads').textContent = pr.leads;
  $('tQual').textContent = pr.qualified;
  $('tStrong').textContent = pr.strong;

  const box = $('stages'); box.innerHTML = '';
  pr.rows.forEach(r => {
    const div = document.createElement('div');
    div.className = 'srow' + (st.stage === r.stage ? ' active' : '');
    const pct = r.total ? Math.round(100 * r.done / r.total) : 0;
    div.innerHTML = '<div class="nm">'+esc(r.friendly)+'</div>' +
      '<div class="bar"><i style="width:'+pct+'%"></i></div>' +
      '<div class="ct">'+r.done+' / '+r.total+'</div>' +
      '<div class="wh">'+esc(r.where)+'</div>';
    box.appendChild(div);
  });

  const ready = d.ai_ready && d.has_plan;
  if (st.running) {
    $('runTitle').textContent = (st.friendly || 'Working') + ' running...';
    $('runSub').textContent = 'This keeps going on its own. You can close this tab and '
      + 'come back - reopening shows where it got to.';
    $('btnStart').disabled = true;
    $('btnStart').textContent = 'Researching... ' + pr.percent + '%';
    show($('btnStop'), true);
  } else {
    $('btnStart').disabled = !ready;
    $('btnStart').textContent = pr.percent > 0 ? 'Continue research' : 'Start research';
    show($('btnStop'), false);
    if (!d.ai_ready) { $('runTitle').textContent = 'Set up the AI first'; $('runSub').textContent = ''; }
    else if (st.error) { $('runTitle').textContent = 'Stopped with a problem'; msg($('runMsg'), st.error, 'bad'); }
    else if (st.cancelled) { $('runTitle').textContent = 'Stopped';
                             $('runSub').textContent = 'Progress is saved. Press Continue.'; }
    else if (pr.percent >= 100 && pr.leads) { $('runTitle').textContent = 'Research complete';
                             $('runSub').textContent = 'Download the spreadsheet below.'; }
    else { $('runTitle').textContent = 'Ready';
           $('runSub').textContent = 'Press Start. You can stop and resume at any time.'; }
  }
  if (st.problems && st.problems.length) msg($('runMsg'), st.problems[0], 'bad');

  if (st.engine_busy) {
    show($('engineBar'), true);
    $('engineBar').querySelector('i').style.width = (st.engine_percent||0) + '%';
    $('btnLocal').disabled = true;
    msg($('engineMsg'), st.engine_message || 'Setting up...', 'info');
  } else {
    show($('engineBar'), false);
    $('btnLocal').disabled = false;
    if (st.engine_error) msg($('engineMsg'), st.engine_error, 'bad');
  }

  $('btnDownload').disabled = !d.export_ready;
  $('log').textContent = (d.log||[]).join('\n');
  $('log').scrollTop = $('log').scrollHeight;
}

// A frozen progress bar that still says "Researching... 42%" is worse than an
// error. The empty catch here meant a closed console window, a crashed process
// or a sleeping machine left the page polling into the void forever, insisting
// it was still working.
let missedPolls = 0;

async function tick() {
  try {
    const d = await (await fetch('/api/state')).json();
    missedPolls = 0;
    show($('offline'), false);
    render(d);
    if (d.progress.qualified > 0) loadResults();
  } catch (e) {
    missedPolls += 1;
    if (missedPolls >= 3) {
      show($('offline'), true);
      $('btnStart').disabled = true;
      $('btnStop').disabled = true;
    }
  }
}

let resultsAt = 0;
async function loadResults() {
  if (Date.now() - resultsAt < 15000) return;
  resultsAt = Date.now();
  const d = await (await fetch('/api/results')).json();
  if (!d.rows || !d.rows.length) return;
  show($('cardResults'), true);
  const tb = $('tbl').querySelector('tbody'); tb.innerHTML = '';
  d.rows.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td><b>'+esc(r.company)+'</b>' + (r.website ? '<div style="font-size:12px;color:#93a4bf">'+esc(r.website)+'</div>' : '') + '</td>' +
      '<td>'+esc(r.category)+'</td>' +
      '<td><span class="pill l'+r.rank+'">'+esc(r.level)+'</span>' +
      '<div style="font-size:12px;color:#93a4bf">score '+r.score+'</div></td>' +
      '<td>'+esc(r.evidence)+'</td><td>'+esc(r.person)+'</td>';
    tb.appendChild(tr);
  });
}

async function loadEngine() {
  const d = await (await fetch('/api/engine/detect')).json();
  const rec = d.recommendation;
  $('engineSub').textContent = d.summary;
  $('hwDump').textContent = JSON.stringify(d.hardware, null, 2);
  $('localWhy').textContent = rec.reason;
  if (d.already_running) {
    $('btnLocal').textContent = 'Use the AI already running here';
    $('localHint').textContent = 'Something is already serving AI on this PC - '
      + 'Prospector can use it and download nothing.';
    $('btnLocal').disabled = false;
  } else if (rec.can_run_local) {
    $('btnLocal').textContent = 'Set up local AI';
    $('localHint').textContent = 'One-off download of about '
      + (30/1000 + rec.size_gb).toFixed(1) + ' GB. ' + rec.quality + '.';
    $('btnLocal').disabled = false;
  } else {
    $('btnLocal').textContent = 'Not possible on this PC';
    $('btnLocal').disabled = true;
    $('localHint').textContent = 'Use the cloud option instead.';
  }
}

$('btnEngineToggle').onclick = () => {
  engineOpen = !engineOpen;
  if (!engineOpen) engineTouched = false;
  render(LAST);
};
$('cardEngine').addEventListener('click', () => { engineTouched = true; });

$('btnPlan').onclick = async () => {
  msg($('planMsg'), 'Working out how to research this...', 'info');
  $('btnPlan').disabled = true;
  const r = await post('/api/plan/build', { prompt: $('prompt').value });
  $('btnPlan').disabled = false;
  if (r.ok) { newSearch = false; planFilledFor = null; planDirty = false; }
  msg($('planMsg'), r.ok ? '' : r.message, r.ok ? 'ok' : 'bad');
  tick();
};

$('btnSavePlan').onclick = async () => {
  const r = await post('/api/plan/save', { plan: readPlan() });
  msg($('savePlanMsg'), r.ok ? 'Saved.' : 'Could not save.', r.ok ? 'ok' : 'bad');
  if (r.ok) { PLAN = r.plan; markSaved(); }
  tick();
};

// "Start over" used to un-hide the prompt card and the next poll re-hid it two
// seconds later, so the box flashed up and vanished and there was no way to
// start a second search except from the command line.
$('btnRePrompt').onclick = () => {
  newSearch = true;
  show($('cardPrompt'), true);
  $('prompt').value = '';
  $('prompt').focus();
};

$('btnLocal').onclick = async () => {
  msg($('engineMsg'), 'Setting up local AI. The first download takes a while.', 'info');
  await post('/api/engine/local', { model_url: $('modelUrl').value.trim() });
  tick();
};

$('btnCloud').onclick = async () => {
  const r = await post('/api/engine/cloud',
    { api_key: $('apiKey').value.trim(), model: $('cloudModel').value });
  $('apiKey').value = '';
  msg($('engineMsg'), r.ok ? 'Cloud key saved.' : 'Could not save.', r.ok ? 'ok' : 'bad');
  tick();
};

$('btnTest').onclick = async () => {
  msg($('engineMsg'), 'Testing...', 'info');
  const r = await post('/api/engine/test',
    { api_key: $('apiKey').value.trim(), model: $('cloudModel').value });
  msg($('engineMsg'), r.message, r.ok ? 'ok' : 'bad');
  tick();
};

$('file').onchange = async e => {
  if (!e.target.files.length) return;
  const fd = new FormData(); fd.append('file', e.target.files[0]);
  const r = await post('/api/upload', fd, true);
  msg($('uploadMsg'), r.message, r.ok ? 'ok' : 'bad');
  tick();
};

$('btnUpload').onclick = async () => {
  const fd = new FormData(); fd.append('names', $('names').value);
  const r = await post('/api/upload', fd, true);
  msg($('uploadMsg'), r.message, r.ok ? 'ok' : 'bad');
  if (r.ok) $('names').value = '';
  tick();
};

let starting = false;
$('btnStart').onclick = async () => {
  // Disabled here, not on the next two-second render.
  if (starting || $('btnStart').disabled) return;
  starting = true;
  $('btnStart').disabled = true;
  try {
    msg($('runMsg'), '', '');
    const saved = await post('/api/plan/save', { plan: readPlan() });
    if (saved.ok) markSaved();
    const r = await post('/api/start', {});
    if (!r.ok) msg($('runMsg'), r.message, 'bad');
  } finally {
    starting = false;
  }
  tick();
};

$('btnStop').onclick = async () => { await post('/api/stop', {}); tick(); };

$('btnBuild').onclick = async () => {
  msg($('runMsg'), 'Building the spreadsheet...', 'info');
  const r = await post('/api/export', {});
  msg($('runMsg'), r.ok ? 'Ready: ' + r.name : r.message, r.ok ? 'ok' : 'bad');
  tick();
};

$('btnDownload').onclick = () => { window.location = '/api/download'; };
$('btnFolder').onclick = () => fetch('/api/open-folder');

tick();
loadEngine();
setInterval(tick, 2000);
</script>
</body>
</html>
"""

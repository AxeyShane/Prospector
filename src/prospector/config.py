"""Prospector configuration: paths, projects, environment, and the research plan.

The unit of work is a *project* -- one research brief, one database, one
spreadsheet. "Mining suppliers already selling into Australia" and "UK facilities
managers for a cleaning contract" are two projects that never touch each other.

    ~/.prospector/
        active_project.txt
        projects/
            mining-suppliers-australia/
                prospector.db      the conveyor belt (WAL SQLite)
                .env               engine choice, API key, model
                plan.json          the AI-generated research plan
                exports/           the spreadsheets
                page_cache/
                logs/
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Project selection
# ---------------------------------------------------------------------------

_USER_ROOT = Path.home() / ".prospector"
PROJECTS_ROOT = _USER_ROOT / "projects"
_ACTIVE_MARKER = _USER_ROOT / "active_project.txt"
DEFAULT_PROJECT = "default"


def slugify(text: str, limit: int = 48) -> str:
    """Folder-safe slug from a human title."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug[:limit].strip("-")) or DEFAULT_PROJECT


def get_active_project() -> str:
    env = os.environ.get("PROSPECTOR_PROJECT", "").strip()
    if env:
        return env
    if _ACTIVE_MARKER.exists():
        val = _ACTIVE_MARKER.read_text(encoding="utf-8").strip()
        if val:
            return val
    return DEFAULT_PROJECT


def list_projects() -> list[str]:
    if not PROJECTS_ROOT.exists():
        return [DEFAULT_PROJECT]
    names = sorted(p.name for p in PROJECTS_ROOT.iterdir() if p.is_dir())
    return names or [DEFAULT_PROJECT]


def create_project(name: str) -> str:
    slug = slugify(name)
    (PROJECTS_ROOT / slug).mkdir(parents=True, exist_ok=True)
    return slug


def set_active_project(name: str) -> None:
    _USER_ROOT.mkdir(parents=True, exist_ok=True)
    _ACTIVE_MARKER.write_text(name.strip(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

APP_DIR = PROJECTS_ROOT / get_active_project()
DB_PATH = APP_DIR / "prospector.db"
ENV_PATH = APP_DIR / ".env"
PLAN_PATH = APP_DIR / "plan.json"
EXPORT_DIR = APP_DIR / "exports"
CACHE_DIR = APP_DIR / "page_cache"
LOG_DIR = APP_DIR / "logs"

PACKAGE_DIR = Path(__file__).parent
SHARED_DIR = _USER_ROOT / "shared"          # models, downloads: shared across projects


def refresh_paths() -> None:
    """Recompute path globals after the active project changes.

    The module-level paths resolve once at import, which is right for the CLI
    but wrong for the app, where the user creates a project while the process
    is already running.
    """
    global APP_DIR, DB_PATH, ENV_PATH, PLAN_PATH, EXPORT_DIR, CACHE_DIR, LOG_DIR

    APP_DIR = PROJECTS_ROOT / get_active_project()
    DB_PATH = APP_DIR / "prospector.db"
    ENV_PATH = APP_DIR / ".env"
    PLAN_PATH = APP_DIR / "plan.json"
    EXPORT_DIR = APP_DIR / "exports"
    CACHE_DIR = APP_DIR / "page_cache"
    LOG_DIR = APP_DIR / "logs"


def ensure_dirs() -> None:
    for d in (APP_DIR, EXPORT_DIR, CACHE_DIR, LOG_DIR, SHARED_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

DEFAULTS = {
    # Both engines can be configured at once -- agents.py decides which agent
    # uses which. Neither is a global mode.
    "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    "OPENROUTER_MODEL": "google/gemini-2.5-flash",
    "LOCAL_BASE_URL": "",
    "LOCAL_MODEL": "",
    "REQUEST_TIMEOUT": "30",
    "SEARCH_DELAY_MS": "1500",
    "USER_AGENT": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


# Settings that belong to the person, not to one piece of research: the cloud
# key, the local engine's address, the model choice, agent routing.
#
# These used to live in the active project's .env, and the app switches projects
# the moment a plan is built. A key saved on the AI screen before building the
# first plan was written into `projects/default/`, left behind by the switch,
# and gone at the next launch -- so the app asked for it again, for every new
# project, forever, while the downloaded model sat on disk unused.
SHARED_KEYS = (
    "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL",
    "LOCAL_BASE_URL", "LOCAL_MODEL",
)


def shared_env_path() -> Path:
    return SHARED_DIR / "settings.env"


def _read_env_file(path: Path) -> dict:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    except OSError:
        pass
    return values


def load_env() -> None:
    """Load shared settings, then this project's, then the defaults.

    Project settings win over shared ones -- except for the account-level keys,
    where the shared file wins if it has a value at all. Without that exception
    a key left in an older project's `.env`, from before these were separated,
    shadows the shared copy permanently: the user pastes a new key, the app says
    it saved, and the next launch silently goes back to the old one with no way
    to tell why.
    """
    shared = _read_env_file(shared_env_path())
    for key, val in shared.items():
        if val:
            os.environ.setdefault(key, val)

    for key, val in _read_env_file(ENV_PATH).items():
        if not val:
            continue
        if key in SHARED_KEYS and shared.get(key):
            continue
        os.environ[key] = val

    for key, val in DEFAULTS.items():
        if val and not os.environ.get(key):
            os.environ[key] = val


def _write_env_file(path: Path, updates: dict, heading: str) -> None:
    existing = _read_env_file(path)
    existing.update({k: str(v) for k, v in updates.items() if v is not None})
    body = [f"# {heading}", "# Written by the app -- safe to edit by hand.", ""]
    body += [f"{k}={v}" for k, v in existing.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")


def write_env(updates: dict) -> None:
    """Merge keys into settings, preserving what is already there.

    Account-level settings (the cloud key, the local engine) go to the shared
    file so they survive switching or creating a project; everything else stays
    with the project it belongs to.
    """
    ensure_dirs()
    shared = {k: v for k, v in updates.items() if k in SHARED_KEYS}
    project = {k: v for k, v in updates.items() if k not in SHARED_KEYS}

    if shared:
        _write_env_file(shared_env_path(), shared, "Prospector settings for this PC.")
    if project:
        _write_env_file(ENV_PATH, project, "Prospector settings for this project.")

    for k, v in updates.items():
        if v is not None:
            os.environ[k] = str(v)


def env_int(key: str, fallback: int) -> int:
    try:
        return int(os.environ.get(key, "") or fallback)
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# The research plan
# ---------------------------------------------------------------------------

def load_plan() -> dict:
    """Load this project's plan, or {} when the user has not made one yet."""
    if not PLAN_PATH.exists():
        return {}
    try:
        return json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_plan(plan: dict) -> None:
    ensure_dirs()
    PLAN_PATH.write_text(json.dumps(plan, indent=2), encoding="utf-8")


DIRECTORY_DOMAINS = {
    # Business directories, marketplaces, data brokers and social sites. The
    # resolve stage must never accept one of these as a company's own website:
    # without this list, small manufacturers resolve to an IndiaMART listing and
    # every stage after it reads marketplace boilerplate instead of the
    # company's own words.
    "indiamart.com", "m.indiamart.com", "tradeindia.com", "exportersindia.com",
    "justdial.com", "sulekha.com", "zaubacorp.com", "thecompanycheck.com",
    "tofler.in", "dnb.com", "zoominfo.com", "rocketreach.co", "leadiq.com",
    "signalhire.com", "datanyze.com", "growjo.com", "crunchbase.com",
    "tracxn.com", "pitchbook.com", "bloomberg.com", "linkedin.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "tiktok.com", "pinterest.com", "reddit.com", "quora.com",
    "wikipedia.org", "grokipedia.com", "alibaba.com", "made-in-china.com",
    "volza.com", "importgenius.com", "eximpedia.app", "seair.co.in",
    "zauba.com", "tendata.com", "screener.in", "moneycontrol.com",
    "economictimes.indiatimes.com", "yahoo.com", "investing.com",
    "marketscreener.com", "tradingview.com", "scribd.com", "slideshare.net",
    "amazon.com", "flipkart.com", "ebay.com", "etsy.com",
    "mascus.com", "mascus.co.uk", "machinio.com", "metoree.com",
    "environmental-expert.com", "theorg.com", "ampliz.com", "bitscale.ai",
    "eindiabusiness.com", "10times.com", "vendelux.com", "yelp.com",
    "yellowpages.com", "bbb.org", "glassdoor.com", "indeed.com",
    "manta.com", "kompass.com", "europages.co.uk", "europages.com",
    "thomasnet.com", "globalspec.com", "medium.com", "blogspot.com",
    "wordpress.com", "wixsite.com", "substack.com", "github.com",
}


def load_directory_domains() -> set[str]:
    """Directory domains, plus any the user added in `extra_directory_domains`."""
    extra = load_plan().get("extra_directory_domains") or []
    return DIRECTORY_DOMAINS | {str(d).lower().strip() for d in extra if d}

"""Prospector database layer: schema, migrations, stats, connections.

Single source of truth for the `leads` table. Every column from every
pipeline stage is created up front so any stage can run independently without
migration ordering issues -- the same approach ApplyPilot takes with `jobs`.

The database is the conveyor belt. Each stage reads rows where its own output
column is NULL, does its work, and writes the result back. That is what makes
the pipeline resumable: kill it at any point, run it again, and it picks up
exactly the rows that were not finished.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from urllib.parse import urlparse
import threading
from datetime import datetime, timezone
from pathlib import Path

from prospector import config

log = logging.getLogger(__name__)

# Thread-local connection storage -- each thread gets its own connection
# (required for SQLite thread safety with parallel workers).
_local = threading.local()

MAX_ATTEMPTS = 3


def utc_now() -> str:
    """ISO-8601 UTC timestamp, used for every *_at column."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled."""
    path = str(db_path or config.DB_PATH)

    if not hasattr(_local, "connections"):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or config.DB_PATH)
    if hasattr(_local, "connections"):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Complete column registry: column_name -> SQL type with optional default.
# Adding a column here is all that's needed for it to appear in both new
# databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # -- Discovery / seed ---------------------------------------------------
    "company_key": "TEXT PRIMARY KEY",   # normalised name, the stable id
    "company_name": "TEXT",              # name as it was first seen
    "source_list": "TEXT",               # uploaded file, or "discovered"
    "discovered_via": "TEXT",            # the search query that surfaced it
    "discovery_source_url": "TEXT",      # the page it was named on
    "seeded_at": "TEXT",
    # -- Resolve (find the official website) --------------------------------
    "website": "TEXT",
    "website_confidence": "REAL",
    "website_candidates": "TEXT",        # JSON list of rejected candidates
    "resolved_at": "TEXT",
    "resolve_error": "TEXT",
    "resolve_attempts": "INTEGER DEFAULT 0",
    # -- Crawl (fetch the pages that carry the evidence) --------------------
    "pages_json": "TEXT",                # JSON {url: text}
    "crawl_chars": "INTEGER",
    "crawled_at": "TEXT",
    "crawl_error": "TEXT",
    "crawl_attempts": "INTEGER DEFAULT 0",
    # -- Classify (category + mining relevance) -----------------------------
    "country": "TEXT",
    "origin_country": "TEXT",
    "entity_type": "TEXT",
    "category": "TEXT",
    "products": "TEXT",
    "relevance": "TEXT",                 # High | Medium | Low | Not relevant
    "from_name_only": "INTEGER",         # 1 = judged with no website to read
    "classify_reasoning": "TEXT",
    "classified_at": "TEXT",
    "classify_error": "TEXT",
    "classify_attempts": "INTEGER DEFAULT 0",
    # -- Qualify (the important one) ----------------------------------------
    "qualification_level": "TEXT",       # one of the plan's four labels
    "qualification_evidence": "TEXT",
    "qualification_matched": "TEXT",     # criteria names that were met
    "qualification_sources": "TEXT",     # JSON list of source URLs
    "qualification_score": "INTEGER",    # 0-100, weighted by criteria
    "qualified_at": "TEXT",
    "qualify_error": "TEXT",
    "qualify_attempts": "INTEGER DEFAULT 0",
    # -- Profile (size and shape of the company) ----------------------------
    "revenue": "TEXT",
    "employees": "TEXT",
    "founded": "TEXT",
    "ownership": "TEXT",                 # Public | Private | Subsidiary ...
    "parent": "TEXT",
    "plants": "TEXT",
    "export_countries": "TEXT",
    "profiled_at": "TEXT",
    "profile_error": "TEXT",
    "profile_attempts": "INTEGER DEFAULT 0",
    # -- People -------------------------------------------------------------
    "people_json": "TEXT",               # JSON list of {name,title,linkedin,note}
    "people_dropped": "INTEGER",         # contacts discarded as unverifiable
    "people_at": "TEXT",
    "people_error": "TEXT",
    "people_attempts": "INTEGER DEFAULT 0",
    # -- Outreach -----------------------------------------------------------
    "outreach_angle": "TEXT",            # the specific fact the opener uses
    "outreach_subject": "TEXT",
    "outreach_body": "TEXT",
    "outreach_channel": "TEXT",          # email | linkedin | call
    "outreach_at": "TEXT",
    "outreach_error": "TEXT",
    "outreach_attempts": "INTEGER DEFAULT 0",
    # -- Output / manual ----------------------------------------------------
    "meet_rank": "INTEGER",
    "notes": "TEXT",
    "exported_at": "TEXT",
}


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the leads table with every column. Idempotent."""
    conn = get_connection(db_path or config.DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            company_key   TEXT PRIMARY KEY,
            company_name  TEXT,
            source_list   TEXT,
            seeded_at     TEXT
        )
        """
    )
    conn.commit()
    ensure_columns(conn)

    # Indexes on the columns every stage's pending-work query filters on.
    for col in ("relevance", "qualification_level", "resolved_at", "classified_at"):
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_leads_{col} ON leads({col})"
        )
    conn.commit()
    return conn


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns (forward-only migration). Returns names added."""
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    added: list[str] = []

    for col, dtype in _ALL_COLUMNS.items():
        if col in existing:
            continue
        if "PRIMARY KEY" in dtype:
            # Created with the table itself; ALTER TABLE cannot add one.
            continue
        conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {dtype}")
        added.append(col)

    if added:
        conn.commit()
    return added


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def normalise_key(name: str) -> str:
    """Collapse a company name to a stable primary key.

    "M/S ASKA EQUIPMENTS PRIVATE LIMITED" and "Aska Equipments Pvt Ltd" are
    the same lead as far as the pipeline is concerned. Suffix stripping
    happens before lowercasing so the legal-form words are matched whatever
    case the show list used.

    The bias here is deliberately towards *under*-merging. Two rows for one
    company is a wasted phone call; one row for two companies destroys data
    and the user never finds out. Near-misses that survive this function are
    caught later by the domain merge in `merge_duplicate_websites`, which has
    real evidence -- the same website -- to work from.
    """
    import re

    raw = name or ""
    s = " " + raw.upper().strip() + " "
    s = s.replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s)

    # Phase 1: legal-form and courtesy words. Repeated, because "PVT LTD"
    # arrives as two separate tokens.
    #
    # Country words are NOT in here. "INDIA" used to be, and it merged Indian
    # Oil Corporation with Oil India Limited -- two of the country's largest
    # companies, collapsed into the single key "oil". Phase 2 handles the case
    # this was meant for.
    noise = (
        "PRIVATE", "PVT", "LIMITED", "LTD", "LLP", "LLC", "INC", "INCORPORATED",
        "CORPORATION", "CORP", "COMPANY", "CO", "GMBH", "AG", "SA", "BV", "PTY",
    )
    # Only these may be stripped from the *front*. An entity suffix at the start
    # of a name is part of the name: stripping it collapsed "AG Industries",
    # "MS Industries" and "CO Industries" into the single key "industrie", which
    # is exactly the merging of unrelated companies this function claims to
    # guard against.
    #
    # "M S" and not "MS": the courtesy prefix is written "M/s" or "M/S", and the
    # punctuation strip above turns that into two separate tokens. A bare "MS"
    # arrives that way only when it is genuinely part of the name, and stripping
    # it turned "MS Industries" into plain "industries".
    prefixes = ("M S", "MESSRS", "THE")

    stripped_suffix = False
    changed = True
    while changed:
        changed = False
        for token in noise:
            pad = f" {token} "
            if s.endswith(pad):
                s = s[: -len(pad)] + " "
                s = re.sub(r"\s+", " ", s)
                changed = stripped_suffix = True
        for token in prefixes:
            pad = f" {token} "
            if s.startswith(pad) and len(s.strip()) > len(token):
                s = " " + s[len(pad):]
                s = re.sub(r"\s+", " ", s)
                changed = True

    # Phase 2: a trailing country word, but only on a name that carried a legal
    # form after it -- "Acme India Pvt Ltd" is Acme; "Oil India Limited" is not
    # Oil, but stripping it there still leaves a different key from "Indian Oil
    # Corporation", which is what matters. A *leading* country word is never
    # stripped, so "Indian Oil" and "India Cements" keep their identity.
    if stripped_suffix:
        for token in ("INDIA", "INDIAN"):
            pad = f" {token} "
            if s.endswith(pad):
                s = s[: -len(pad)] + " "
                s = re.sub(r"\s+", " ", s)
                break

    # "J.K. Cement" arrives as "J K CEMENT" and "JK Cement" as "JK CEMENT".
    # Runs of single letters are one initialism.
    s = re.sub(r"\b(?:[A-Z0-9] )+[A-Z0-9]\b",
               lambda m: m.group(0).replace(" ", ""), s)

    # Conservative singularisation, so "Equipments" and "Equipment" agree.
    # Length-gated to leave SONS, WORKS and GAS alone, and -SS/-US/-IS words
    # (GLASS, PLUS, ANALYSIS) are never touched.
    def _singular(tok: str) -> str:
        if len(tok) >= 7 and tok.endswith("S") and not tok.endswith(("SS", "US", "IS")):
            return tok[:-1]
        return tok

    key = " ".join(_singular(t) for t in s.split()).strip().lower()

    # A name that is nothing but legal forms and a country ("India Limited")
    # normalises to nothing. Dropping the row silently is how leads disappear
    # without a trace, so fall back to a plain slug of what we were given.
    if not key:
        key = re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()
    return key


def merge_duplicate_websites(conn: sqlite3.Connection | None = None) -> int:
    """Merge rows that resolved to the same website. Returns rows removed.

    `normalise_key` only ever sees a name, and it is deliberately cautious --
    "Acme Ltd" and "Acme Industries" stay separate because collapsing two real
    companies is worse than listing one twice. That leaves genuine duplicates
    behind, and a salesperson phoning the same company twice from one list
    notices immediately.

    Once both rows have resolved, there is real evidence to work from: the same
    domain. The higher-confidence row is kept, the other name is recorded in its
    notes, and any research already done on the loser is carried across rather
    than thrown away.
    """
    if conn is None:
        conn = get_connection()

    groups: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT * FROM leads WHERE website IS NOT NULL AND website != ''"
    ).fetchall():
        try:
            host = urlparse(row["website"]).netloc.lower().removeprefix("www.")
        except ValueError:
            continue
        if host:
            groups.setdefault(host, []).append(row)

    removed = 0
    for host, rows in groups.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: (-(r["website_confidence"] or 0.0), r["company_key"]))
        keeper, losers = rows[0], rows[1:]

        # Carry across anything the keeper is missing but a loser has.
        #
        # Grouped, not column by column. A stage's columns only make sense
        # together: carrying `qualification_level` without its score, sources
        # and timestamp produced a top-rated lead scoring zero, sorted to the
        # bottom of the call list, and permanently excluded from re-qualifying
        # because the pending query keys on the level being NULL.
        groups = (
            ("crawled_at", "pages_json", "crawl_chars"),
            ("classified_at", "category", "products", "relevance", "entity_type",
             "country", "origin_country", "from_name_only", "classify_reasoning"),
            ("qualified_at", "qualification_level", "qualification_evidence",
             "qualification_matched", "qualification_sources",
             "qualification_score"),
            ("profiled_at", "revenue", "employees", "founded", "ownership",
             "parent", "plants", "export_countries"),
            ("people_at", "people_json", "people_dropped"),
        )
        carry = {}
        for group in groups:
            marker = group[0]
            if keeper[marker]:
                continue          # the keeper already did this stage
            donor = next((l for l in losers if l[marker]), None)
            if donor is None:
                continue
            for column in group:
                carry[column] = donor[column]

        names = " | ".join(
            f"Also listed as: {l['company_name']}" for l in losers
            if (l["company_name"] or "").strip().lower()
            != (keeper["company_name"] or "").strip().lower()
        )
        if names:
            existing = keeper["notes"] or ""
            carry["notes"] = f"{existing} | {names}".strip(" |") if existing else names

        if carry:
            update(keeper["company_key"], conn=conn, **carry)
        for loser in losers:
            conn.execute("DELETE FROM leads WHERE company_key = ?", (loser["company_key"],))
            removed += 1

    conn.commit()
    if removed:
        log.info("merged %d duplicate rows by website", removed)
    return removed


def upsert_lead(
    company_name: str,
    source_list: str = "",
    discovered_via: str = "",
    source_url: str = "",
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Insert an lead if new. Returns True when a row was created."""
    if conn is None:
        conn = get_connection()

    key = normalise_key(company_name)
    if not key:
        return False

    cur = conn.execute(
        "INSERT OR IGNORE INTO leads "
        "(company_key, company_name, source_list, discovered_via, "
        " discovery_source_url, seeded_at) VALUES (?, ?, ?, ?, ?, ?)",
        (key, company_name.strip(), source_list, discovered_via, source_url, utc_now()),
    )
    created = cur.rowcount > 0

    if not created:
        # Two *different* names can normalise to the same key -- "Eastman
        # Exports Inc." and "Eastman Exports Private Limited" are a real example
        # from a real list, the US arm and the India entity. Merging them is
        # usually right, but never silently: record the alternate name so the
        # user can split the row if it matters.
        existing_name = conn.execute(
            "SELECT company_name FROM leads WHERE company_key = ?", (key,)
        ).fetchone()[0]
        if existing_name and existing_name.strip().lower() != company_name.strip().lower():
            note = f"Also listed as: {company_name.strip()}"
            conn.execute(
                "UPDATE leads SET notes = "
                "CASE WHEN notes IS NULL OR notes = '' THEN ? "
                "     WHEN notes LIKE ? THEN notes "
                "     ELSE notes || ' | ' || ? END "
                "WHERE company_key = ?",
                (note, f"%{note}%", note, key),
            )

    conn.commit()
    return created


def update(company_key: str, conn: sqlite3.Connection | None = None, **fields) -> None:
    """Write named columns for one lead. Unknown columns are rejected."""
    if not fields:
        return
    if conn is None:
        conn = get_connection()

    unknown = set(fields) - set(_ALL_COLUMNS)
    if unknown:
        raise ValueError(f"Unknown column(s): {', '.join(sorted(unknown))}")

    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE leads SET {assignments} WHERE company_key = ?",
        (*fields.values(), company_key),
    )
    conn.commit()


def bump_attempt(company_key: str, stage: str, error: str = "",
                 conn: sqlite3.Connection | None = None) -> None:
    """Record a failed attempt so a permanently broken row stops being retried."""
    if conn is None:
        conn = get_connection()
    attempts_col = f"{stage}_attempts"
    error_col = f"{stage}_error"
    if attempts_col not in _ALL_COLUMNS:
        raise ValueError(f"No attempts column for stage '{stage}'")
    conn.execute(
        f"UPDATE leads SET {attempts_col} = COALESCE({attempts_col}, 0) + 1, "
        f"{error_col} = ? WHERE company_key = ?",
        (error[:500] or None, company_key),
    )
    conn.commit()


def get_pages(row: sqlite3.Row | dict) -> dict[str, str]:
    """Decode the crawled pages blob, tolerating NULL and malformed JSON."""
    raw = row["pages_json"] if "pages_json" in row.keys() else None  # noqa: SIM118
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return lead counts by pipeline stage, for `status` and the export."""
    if conn is None:
        conn = get_connection()

    def one(sql: str, *params) -> int:
        return conn.execute(sql, params).fetchone()[0]

    stats: dict = {}
    stats["total"] = one("SELECT COUNT(*) FROM leads")
    stats["resolved"] = one("SELECT COUNT(*) FROM leads WHERE website IS NOT NULL")
    stats["resolve_failed"] = one(
        "SELECT COUNT(*) FROM leads WHERE website IS NULL AND resolve_attempts > 0"
    )
    stats["crawled"] = one("SELECT COUNT(*) FROM leads WHERE crawled_at IS NOT NULL")
    stats["classified"] = one("SELECT COUNT(*) FROM leads WHERE relevance IS NOT NULL")
    stats["qualified"] = one("SELECT COUNT(*) FROM leads WHERE qualification_level IS NOT NULL")
    stats["profiled"] = one("SELECT COUNT(*) FROM leads WHERE profiled_at IS NOT NULL")
    # Counting `people_at` counts companies the stage *ran on*, not companies a
    # contact was actually found for -- the stage stamps the timestamp even when
    # it returns an empty list. A rate-limited run used to report "200 with named
    # contacts" over a Contacts tab holding twelve rows.
    stats["with_people"] = one(
        "SELECT COUNT(*) FROM leads WHERE people_json IS NOT NULL "
        "AND people_json NOT IN ('', '[]')"
    )
    stats["people_searched"] = one("SELECT COUNT(*) FROM leads WHERE people_at IS NOT NULL")
    stats["drafted"] = one("SELECT COUNT(*) FROM leads WHERE outreach_at IS NOT NULL")

    stats["by_relevance"] = [
        (r[0] or "unclassified", r[1])
        for r in conn.execute(
            "SELECT relevance, COUNT(*) FROM leads GROUP BY relevance "
            "ORDER BY COUNT(*) DESC"
        ).fetchall()
    ]
    stats["by_qualification"] = [
        (r[0] or "not researched", r[1])
        for r in conn.execute(
            "SELECT qualification_level, COUNT(*) FROM leads GROUP BY qualification_level "
            "ORDER BY COUNT(*) DESC"
        ).fetchall()
    ]
    stats["by_category"] = [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT category, COUNT(*) FROM leads WHERE category IS NOT NULL "
            "GROUP BY category ORDER BY COUNT(*) DESC LIMIT 15"
        ).fetchall()
    ]
    return stats

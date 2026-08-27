"""Stage 6 -- profile: revenue, headcount, age, ownership, plants, exports.

This is a "collect whatever is public" stage, not an estimation exercise: where a figure is not published, the answer is the
literal string "Not publicly available" rather than a guess. That instruction
is enforced here in code as well as in the prompt, because a fabricated
revenue number is the kind of error that survives into a client meeting.
"""

from __future__ import annotations

import logging

from prospector.config import load_plan
from prospector.database import (
    MAX_ATTEMPTS, get_connection, get_pages, update, utc_now,
)
from prospector.llm import get_client
from prospector.stages._runner import relevance_filter, run_batch, select_pending
from prospector.websearch import search_many

log = logging.getLogger(__name__)

NOT_PUBLIC = "Not publicly available"

# Superseded by this agent's `role` in agents.py, which is what actually
# gets sent. Kept here only so the prompt below reads in context.
_SYSTEM_REFERENCE = (
    "You are a company-research analyst. You extract only facts that appear in "
    "the supplied text. Where a fact is not present you write exactly "
    f'"{NOT_PUBLIC}". You never estimate. You answer only with JSON.'
)

PROMPT = """Extract basic company information for a sales briefing.

COMPANY: {name}
WEBSITE: {website}

=== TEXT FROM THEIR WEBSITE ===
{site_text}

=== WEB SEARCH RESULTS ===
{search_text}

Return JSON with exactly these keys. For any field not supported by the text
above, use exactly "{not_public}" -- do not estimate, do not infer from company
size, do not use prior knowledge.
{{
  "revenue": "approximate annual revenue with currency and year if stated",
  "employees": "number or range of employees",
  "founded": "year established",
  "ownership": "Public (name the exchange if stated) or Private or Subsidiary",
  "parent": "parent or group company, or 'Independent' if clearly standalone",
  "plants": "main sites, offices or plants, comma separated",
  "export_countries": "countries or regions they operate in or sell to, comma separated"
}}"""


def _text(row, max_chars: int = 7000) -> str:
    pages = get_pages(row)
    if not pages:
        return "(no website content available)"
    parts, budget = [], max_chars
    for url, text in pages.items():
        if budget <= 0:
            break
        chunk = text[: min(len(text), budget, 2500)]
        parts.append(f"[{url}]\n{chunk}")
        budget -= len(chunk)
    return "\n\n".join(parts)


def _clean(value, limit: int = 300) -> str:
    """Normalise empties, nulls and hedge words to the agreed literal."""
    if value is None:
        return NOT_PUBLIC
    text = str(value).strip()
    if not text or text.lower() in {
        "n/a", "na", "none", "null", "unknown", "not available",
        "not specified", "not stated", "not disclosed", "-",
    }:
        return NOT_PUBLIC
    return text[:limit]


def profile_one(row) -> dict:
    results = search_many(
        [
            f'"{row["company_name"]}" revenue employees "year established"',
            f'"{row["company_name"]}" company profile turnover headquarters',
        ],
        max_results=5,
    )
    search_text = "\n\n".join(f"{r.title}\n{r.url}\n{r.snippet}" for r in results[:8])

    prompt = PROMPT.format(
        name=row["company_name"],
        website=row["website"] or "(not found)",
        site_text=_text(row),
        search_text=search_text or "(no search results)",
        not_public=NOT_PUBLIC,
    )

    data = get_client("profile").ask_json(prompt)
    return {
        "revenue": _clean(data.get("revenue")),
        "employees": _clean(data.get("employees"), 100),
        "founded": _clean(data.get("founded"), 60),
        "ownership": _clean(data.get("ownership"), 120),
        "parent": _clean(data.get("parent"), 160),
        "plants": _clean(data.get("plants"), 400),
        "export_countries": _clean(data.get("export_countries"), 400),
    }


def pending_sql(plan: dict | None = None) -> str:
    plan = plan or load_plan()
    min_rel = plan.get("min_relevance_for_research", "Medium")
    return (
        "SELECT * FROM leads WHERE profiled_at IS NULL "
        "AND qualification_level IS NOT NULL "
        f"AND {relevance_filter(min_rel)} "
        f"AND COALESCE(profile_attempts, 0) < {MAX_ATTEMPTS} "
        "ORDER BY company_key"
    )


def run_profile(workers: int = 3, limit: int | None = None) -> dict:
    rows = select_pending(pending_sql(), limit=limit)

    def handler(row):
        fields = profile_one(row)
        update(row["company_key"], conn=get_connection(),
               profiled_at=utc_now(), profile_error=None, **fields)
        # An all-"Not publicly available" row is a real answer, but reporting it
        # as a success alongside a fully populated one is how the Company
        # Profiles tab fills with blank rows nobody can account for.
        if all(str(v).startswith(NOT_PUBLIC) for v in fields.values()):
            return "nothing public found"
        return fields["employees"]

    return run_batch("profile", rows, handler, workers=min(workers, 4),
                     label="Collecting company facts")

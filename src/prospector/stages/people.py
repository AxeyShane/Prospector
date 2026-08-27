"""Stage 7 -- people: who to try to meet.

Name and position are what matter, with a LinkedIn URL when one happens to be
easy to find. Contacts are ranked against the plan's `priority_roles`, so
whoever the user said they want to reach outranks everyone else when the list
has to be cut to `people_per_company`.

The model is told never to invent a person, and then not believed: every name
it returns must appear in the text it was shown, or the contact is discarded
and counted. The same goes for LinkedIn URLs, which must appear verbatim rather
than merely look like LinkedIn URLs. A made-up contact is the fastest way to
lose a user's trust in the whole list, and this agent runs on the small local
model, which is exactly the tier that produces fluent fictional people.
"""

from __future__ import annotations

import json
import re
import logging

from prospector.config import load_plan
from prospector.database import (
    MAX_ATTEMPTS, get_connection, get_pages, update, utc_now,
)
from prospector.llm import get_client
from prospector.stages._runner import relevance_filter, run_batch, select_pending
from prospector.websearch import search_many

log = logging.getLogger(__name__)

# Superseded by this agent's `role` in agents.py, which is what actually
# gets sent. Kept here only so the prompt below reads in context.
_SYSTEM_REFERENCE = (
    "You extract named people and their job titles from supplied text. You only "
    "return people who are explicitly named in the text. You never invent a "
    "person, a title or a LinkedIn URL. You answer only with JSON."
)

PROMPT = """Identify the people worth meeting at this company.

COMPANY: {name}

=== TEXT FROM THEIR WEBSITE (leadership, about and contact pages) ===
{site_text}

=== WEB SEARCH RESULTS ===
{search_text}

Prioritise these roles, most important first:
{roles}

RULES:
- Only include people explicitly named in the text above.
- Only include a "linkedin" URL if that exact URL appears in the text.
- If no named people appear anywhere in the text, return an empty list.
- Return at most {limit} people.

Return JSON:
{{
  "people": [
    {{"name": "Full Name", "title": "Exact job title as stated",
      "linkedin": "URL or empty string",
      "note": "one short reason this person is worth meeting, or empty string"}}
  ]
}}"""


def _rank(title: str, roles: list[str]) -> int:
    """Lower is better. Unlisted roles sort after every listed one."""
    low = (title or "").lower()
    for i, role in enumerate(roles):
        if role.lower() in low:
            return i
    return len(roles) + 1


def _site_text(row, max_chars: int = 7000) -> str:
    pages = get_pages(row)
    if not pages:
        return "(no website content available)"

    # Leadership/team/contact pages first -- that is where names live, and the
    # budget is small enough that homepage boilerplate would crowd them out.
    priority = ("leader", "management", "team", "board", "director", "about",
                "people", "contact", "company")
    ordered = sorted(
        pages.items(),
        key=lambda kv: 0 if any(p in kv[0].lower() for p in priority) else 1,
    )
    parts, budget = [], max_chars
    for url, text in ordered:
        if budget <= 0:
            break
        chunk = text[: min(len(text), budget, 3000)]
        parts.append(f"[{url}]\n{chunk}")
        budget -= len(chunk)
    return "\n\n".join(parts)


def _name_appears(name: str, haystack: str) -> bool:
    """Is this person actually named in the text the model was shown?

    A surname somewhere in seven thousand characters is not evidence. Indian
    company pages are full of surnames used as other things -- "Patel Nagar" in
    an address, "Shah Alloys" in a customer list -- so the loose version of this
    check passed exactly the fabrications it exists to catch: an invented
    "Rajesh Patel, Managing Director" sailed through on a street name.

    So the name must appear as a name: either the whole thing contiguously, or
    the surname within a hundred characters of another part of the name, which
    is what "Mr. R. Sharma" and "Sharma, Ravi" both look like on a real page.
    """
    parts = [t for t in re.findall(r"[A-Za-z]{3,}", name)]
    if not parts:
        return False
    low = haystack.lower()

    # The whole name, contiguously, allowing initials and punctuation between.
    # Tried in reverse too, because a model handed "Ravi Sharma" on the page
    # quite often returns "Sharma, Ravi" -- the same person, written the way a
    # staff list writes it.
    for ordering in (parts, list(reversed(parts))):
        contiguous = r"[\s.,]+".join(re.escape(p.lower()) for p in ordering)
        if re.search(r"\b" + contiguous + r"\b", low):
            return True

    surname = parts[-1].lower()
    others = [p.lower() for p in parts[:-1]]
    if not others:
        return False        # the contiguous test above was the only chance

    # An initial standing in for the given name, but *attached* to the surname:
    # "Mr. R. Sharma". A loose search for a lone "R" anywhere near the surname
    # matched a phone-number line and let "Rajesh Kumar" through on a customer
    # list entry reading "Kumar Steels ... Contact Mr. R. Sharma".
    for other in others:
        initial = re.escape(other[0])
        if re.search(r"\b" + initial + r"\.?\s+" + re.escape(surname) + r"\b", low):
            return True
        if re.search(r"\b" + re.escape(surname) + r"\s*,\s*" + initial, low):
            return True
    return False


def people_one(row, plan: dict) -> tuple[list[dict], int]:
    """Return (verified people, number discarded as unverifiable)."""
    roles = plan.get("priority_roles", [])
    limit = int(plan.get("people_per_company", 4))

    results = search_many(
        [
            f'"{row["company_name"]}" managing director OR CEO OR founder',
            f'"{row["company_name"]}" export sales OR international business head linkedin',
        ],
        max_results=6,
    )
    search_text = "\n\n".join(f"{r.title}\n{r.url}\n{r.snippet}" for r in results[:10])
    site_text = _site_text(row)

    # Everything the model is about to see. A name that is not in here was not
    # read anywhere -- it was invented.
    shown = f"{site_text}\n{search_text}"

    prompt = PROMPT.format(
        name=row["company_name"],
        site_text=site_text,
        search_text=search_text or "(no search results)",
        roles="\n".join(f"{i + 1}. {r}" for i, r in enumerate(roles)),
        limit=limit,
    )

    data = get_client("people").ask_json(prompt)
    raw = data.get("people") or data.get("items") or []
    if isinstance(raw, dict):
        raw = [raw]

    people: list[dict] = []
    seen: set[str] = set()
    dropped = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        title = str(item.get("title", "")).strip()
        if not name or name.lower() in seen:
            continue

        # The one check that matters. This stage runs on the small local model
        # by default, which is exactly the tier that produces a fluent, entirely
        # fictional "Rajesh Kumar, Managing Director" -- and the person holding
        # the spreadsheet finds out by asking a switchboard for someone who does
        # not work there.
        if not _name_appears(name, shown):
            log.info("dropped unverifiable contact %r for %s", name, row["company_name"])
            dropped += 1
            continue

        linkedin = str(item.get("linkedin", "")).strip()
        # A URL is kept only if it was actually in the text. "linkedin.com is in
        # the string" passes trivially for an invented profile slug.
        if linkedin and (
            "linkedin.com" not in linkedin.lower() or linkedin not in shown
        ):
            linkedin = ""

        seen.add(name.lower())
        people.append({
            "name": name[:120],
            "title": title[:160],
            "linkedin": linkedin[:250],
            "note": str(item.get("note", "")).strip()[:250],
        })

    people.sort(key=lambda p: _rank(p["title"], roles))
    return people[:limit], dropped


def pending_sql(plan: dict | None = None) -> str:
    plan = plan or load_plan()
    min_rel = plan.get("min_relevance_for_research", "Medium")
    return (
        "SELECT * FROM leads WHERE people_at IS NULL "
        "AND qualification_level IS NOT NULL "
        f"AND {relevance_filter(min_rel)} "
        f"AND COALESCE(people_attempts, 0) < {MAX_ATTEMPTS} "
        "ORDER BY company_key"
    )


def run_people(workers: int = 3, limit: int | None = None) -> dict:
    plan = load_plan()
    rows = select_pending(pending_sql(plan), limit=limit)

    def handler(row):
        people, dropped = people_one(row, plan)
        update(row["company_key"], conn=get_connection(),
               people_json=json.dumps(people), people_dropped=dropped,
               people_at=utc_now(), people_error=None)
        if not people:
            return "no contact found"
        return f"{len(people)} contact{'' if len(people) == 1 else 's'}"

    return run_batch("people", rows, handler, workers=min(workers, 4),
                     label="Finding who to contact")

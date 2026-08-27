"""Stage 4 -- classify: what does this company do, and does it fit the brief?

Runs on every lead, so it is the stage to point a cheap model at. It works
from the crawled site text when there is any, and from the company name alone
when the site could not be resolved -- a name like "TWIN ROCK TOOLS" already
tells you most of what you need, and marking such a row unclassified would
silently drop it from every stage after this one.

Categories come from the plan, so "relevant" means whatever the user's prompt
said it means.
"""

from __future__ import annotations

import logging

from prospector.config import load_plan
from prospector.database import (
    MAX_ATTEMPTS, get_connection, get_pages, update, utc_now,
)
from prospector.llm import get_client
from prospector.stages._runner import run_batch, select_pending

log = logging.getLogger(__name__)

PENDING_SQL = (
    "SELECT * FROM leads WHERE relevance IS NULL "
    f"AND COALESCE(classify_attempts, 0) < {MAX_ATTEMPTS} "
    "ORDER BY company_key"
)

RELEVANCE_LEVELS = ("High", "Medium", "Low", "Not relevant")

ENTITY_TYPES = (
    "Manufacturer",
    "Manufacturer (Listed)",
    "Multinational subsidiary",
    "Trader/Distributor",
    "Dealer",
    "Service provider",
    "Contractor",
    "Retailer",
    "Consultancy",
    "Association / Government",
    "Other",
)

# Superseded by this agent's `role` in agents.py, which is what actually
# gets sent. Kept here only so the prompt below reads in context.
_SYSTEM_REFERENCE = (
    "You are a lead-research analyst. You classify companies against a "
    "salesperson's brief. You answer only with JSON. You never invent facts: "
    "when the evidence does not support a field, you say so."
)

PROMPT = """Classify this company against the brief.

COMPANY NAME: {name}
WEBSITE: {website}

EVIDENCE FROM THEIR OWN WEBSITE (may be empty):
---
{evidence}
---

WHAT THE SALESPERSON IS LOOKING FOR: {objective}
AN IDEAL LEAD LOOKS LIKE: {profile}

Choose ONE product category. Prefer one of these where it fits:
{categories}

These categories are NOT relevant for this brief -- anything that belongs here
must get relevance "Not relevant":
{excluded}

Choose ONE entity type from exactly this list:
{entity_types}
Use "Multinational subsidiary" for the local arm of a global group. Use
"Trader/Distributor" for resellers and agencies that do not make anything.

Rate relevance to the brief as exactly one of: High, Medium, Low, Not relevant.
  High         = squarely what the salesperson is looking for.
  Medium       = plausibly relevant, adjacent to the brief.
  Low          = same broad industry, but not what was asked for.
  Not relevant = nothing to do with the brief, or in the excluded list above.

If the evidence section is empty, classify from the company name alone and set
"from_name_only": true.

Return JSON with exactly these keys:
{{
  "category": "one short category name",
  "products": "one sentence naming what they actually make or do",
  "entity_type": "one of the entity types above",
  "country": "country of the exhibiting entity, best guess",
  "origin_country": "country of the parent/group, or same as country",
  "relevance": "High | Medium | Low | Not relevant",
  "reasoning": "one short sentence justifying the relevance rating",
  "from_name_only": true or false
}}"""


def _evidence(row, max_chars: int = 2600) -> str:
    """Homepage first, then the highest-value pages, trimmed to a token budget.

    The budget used to be 9,000 characters. This agent runs on the small local
    model, on the CPU, on every single company -- and a CPU spends far longer
    *reading* a prompt than writing an answer, so those 9,000 characters were
    roughly three quarters of the entire run's wall clock. Deciding what a
    company makes does not need nine thousand characters; it needs the top of
    the homepage and the top of the products page. Cutting to 2,600 takes the
    per-company cost from about a minute to about twenty seconds, and small
    models are measurably *more* accurate on a short prompt than a long one.
    """
    pages = get_pages(row)
    if not pages:
        return ""
    parts: list[str] = []
    budget = max_chars
    for url, text in pages.items():
        if budget <= 0:
            break
        chunk = text[: min(len(text), budget, 1200)]
        parts.append(f"[{url}]\n{chunk}")
        budget -= len(chunk)
    return "\n\n".join(parts)


def classify_one(row, plan: dict) -> dict:
    # An excluded name can still arrive from an uploaded list, which never
    # passes through the discovery filter.
    from prospector.stages.discover import _excluded
    if _excluded(row["company_name"], plan):
        return {
            "category": "Excluded", "products": "",
            "entity_type": "Manufacturer", "country": "", "origin_country": "",
            "relevance": "Not relevant", "from_name_only": 1,
            "classify_reasoning": "On the plan's excluded-companies list.",
        }

    client = get_client("classify")
    prompt = PROMPT.format(
        name=row["company_name"],
        website=row["website"] or "(not found)",
        evidence=_evidence(row) or "(no website content available)",
        objective=plan.get("objective", ""),
        profile=plan.get("target_profile", ""),
        categories="\n".join(f"- {c}" for c in plan.get("relevant_categories", []))
                   or "(no list given -- judge from the objective above)",
        excluded="\n".join(f"- {c}" for c in plan.get("excluded_categories", []))
                 or "(none given)",
        entity_types="\n".join(f"- {t}" for t in ENTITY_TYPES),
    )
    data = client.ask_json(prompt)
    had_site = bool(get_pages(row))

    relevance = str(data.get("relevance", "")).strip()
    if relevance not in RELEVANCE_LEVELS:
        # Models occasionally answer "high" or "Very High". Normalise rather
        # than fail -- a rejected call means a company nobody ever looks at.
        #
        # Tested weakest-first, and negatives before positives. Walking the
        # ladder from High downwards made every hedge resolve *upward*:
        # "Medium-High" became High, and "Low, certainly not High" became High
        # too, landing a hedged answer at the top of the call list. An uncertain
        # answer should cost the user a scroll, never a wasted phone call.
        lowered = relevance.lower()
        if any(k in lowered for k in ("not relevant", "irrelevant", "no ", "none")):
            relevance = "Not relevant"
        else:
            relevance = next(
                (lvl for lvl in reversed(RELEVANCE_LEVELS) if lvl.lower() in lowered),
                "Low",
            )

    entity_type = str(data.get("entity_type", "")).strip()
    if entity_type not in ENTITY_TYPES:
        # An unrecognised type used to be kept verbatim, which defeated the
        # whitelist entirely -- "Mining company" went into the spreadsheet as a
        # category of its own and the Type filter grew a long tail of one-offs.
        entity_type = next(
            (t for t in ENTITY_TYPES if t.lower() == entity_type.lower()),
            ENTITY_TYPES[-1] if entity_type else "Manufacturer",
        )

    return {
        "category": str(data.get("category", ""))[:120],
        "products": str(data.get("products", ""))[:400],
        "entity_type": entity_type,
        "country": str(data.get("country", ""))[:60],
        "origin_country": str(data.get("origin_country", ""))[:60],
        "relevance": relevance,
        # Was this judged from the company's own pages, or guessed from its
        # name? The prompt has always asked; the answer used to be thrown away,
        # so a name-only guess was indistinguishable in the spreadsheet from a
        # company whose website had actually been read -- and the Judge was then
        # handed that guess as though it were fact.
        "from_name_only": 0 if had_site else 1,
        "classify_reasoning": str(data.get("reasoning", ""))[:400],
    }


def run_classify(workers: int = 4, limit: int | None = None) -> dict:
    plan = load_plan()
    rows = select_pending(PENDING_SQL, limit=limit)

    def handler(row):
        fields = classify_one(row, plan)
        update(row["company_key"], conn=get_connection(),
               classified_at=utc_now(), classify_error=None, **fields)
        return f"{fields['relevance']} / {fields['category']}"

    return run_batch("classify", rows, handler, workers=workers,
                     label="Sorting the relevant ones")

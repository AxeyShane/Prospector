"""Prompt to research plan.

The user types what they want in their own words. This turns that into the
structured plan every stage reads: which searches to run to find companies,
what makes a company relevant, what "qualified" means, and who to meet.

The plan is deliberately a visible, editable artefact rather than a hidden
prompt. The user sees exactly what the AI decided to look for and can correct
it before anything runs -- which is both faster than re-prompting and the only
honest way to show why a company ended up on the list.

`normalise_plan` is the important half. A small local model will return a plan
with a missing key, a string where a list belongs, or five qualification levels
instead of four, and the pipeline has to survive all of it.
"""

from __future__ import annotations

import logging
import re

from prospector.llm import get_client

log = logging.getLogger(__name__)

RELEVANCE_LEVELS = ("High", "Medium", "Low", "Not relevant")

# The four qualification buckets. Wording is generated per project so it talks
# about the user's actual criteria, but the shape is fixed: the spreadsheet,
# the ranking and the filters all depend on there being exactly four, ordered
# best to worst.
DEFAULT_LEVELS = [
    {"label": "Strong match",
     "definition": "Hard, specific, verifiable evidence that this company meets the "
                   "qualification criteria -- named entities, named customers, named places."},
    {"label": "Partial match",
     "definition": "Credible but incomplete evidence. Points the right way without "
                   "naming a specific customer, partner or location."},
    {"label": "Unclear",
     "definition": "Claims that fit the criteria in general terms but name nothing "
                   "specific anywhere in public sources. Absence of evidence."},
    {"label": "Does not match",
     "definition": "Public sources document this company clearly enough to say it "
                   "does not meet the criteria."},
]

DEFAULT_ROLES = [
    "CEO", "Managing Director", "Founder", "Owner", "President",
    "Head of Sales", "Sales Director", "Business Development Director",
    "Head of International Sales", "Export Director", "Country Manager",
]

# Superseded by this agent's `role` in agents.py, which is what actually
# gets sent. Kept here only so the prompt below reads in context.
_SYSTEM_REFERENCE = (
    "You design lead-generation research plans. You turn a salesperson's "
    "description of who they want to find into a concrete, searchable plan. "
    "You are specific and literal: vague search queries return nothing useful. "
    "You answer only with JSON."
)

PROMPT = """A salesperson described the leads they want. Build the research plan.

WHAT THEY SAID:
\"\"\"
{prompt}
\"\"\"

Produce a plan that a research pipeline can execute. The pipeline will:
  1. run your `discovery_queries` through a web search to harvest company names
  2. find and read each company's website
  3. rate how relevant each company is
  4. judge each company against your `qualification_criteria`
  5. collect company facts and the people worth contacting

RULES FOR discovery_queries:
- Write 8 to 14 queries that a person would actually type to surface LISTS of
  companies -- "top X manufacturers in Y", "X suppliers directory Z",
  "members X association", "X exhibitor list", "leading X companies".
- Vary the wording and the angle. Include regional variations if the user
  named a region. Do NOT write questions; write search queries.
- Never include a specific company name in a discovery query.

RULES FOR qualification_criteria:
- These are what actually decides whether a lead is good. Turn the user's
  requirements into 2 to 5 named, checkable criteria.
- Each needs a `name` (a few words), a `description` saying what evidence
  would prove it, and a `weight` from 1 to 5 for how much it matters.

RULES FOR qualification_levels:
- Exactly four, best to worst. Keep the same shape as the examples but rewrite
  the definitions so they talk about THIS project's criteria.

Return JSON with exactly these keys:
{{
  "title": "short project name, 2-5 words",
  "objective": "one or two sentences restating what they want, in plain language",
  "target_profile": "one sentence describing what an ideal lead looks like",
  "target_leads": 60,
  "regions": ["countries or regions the leads should be based in"],
  "discovery_queries": ["search query", "..."],
  "relevant_categories": ["kinds of company that count as relevant"],
  "excluded_categories": ["kinds of company that look similar but do not count"],
  "qualification_criteria": [
    {{"name": "...", "description": "what evidence proves this", "weight": 5}}
  ],
  "qualification_levels": [
    {{"label": "Strong match", "definition": "..."}},
    {{"label": "Partial match", "definition": "..."}},
    {{"label": "Unclear", "definition": "..."}},
    {{"label": "Does not match", "definition": "..."}}
  ],
  "evidence_types": ["kinds of evidence worth looking for"],
  "priority_roles": ["job titles worth meeting, most important first"],
  "people_per_company": 3
}}"""


def _as_list(value, fallback: list) -> list:
    """Coerce whatever the model returned into a list of non-empty strings."""
    if value is None:
        return list(fallback)
    if isinstance(value, str):
        # Models sometimes return a comma- or newline-separated string.
        parts = [p.strip(" -*\t") for p in re.split(r"[\n;]|,(?![^(]*\))", value)]
        parts = [p for p in parts if p]
        return parts or list(fallback)
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                name = item.get("name") or item.get("label") or item.get("query")
                if name:
                    out.append(str(name).strip())
        return out or list(fallback)
    return list(fallback)


def _as_int(value, fallback: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return fallback


def normalise_plan(raw: dict, user_prompt: str = "") -> dict:
    """Make any model output into a plan the pipeline can actually execute."""
    raw = raw if isinstance(raw, dict) else {}

    criteria = []
    for item in (raw.get("qualification_criteria") or []):
        if isinstance(item, dict) and (item.get("name") or item.get("description")):
            criteria.append({
                "name": str(item.get("name") or "Criterion")[:80],
                "description": str(item.get("description") or "")[:400],
                "weight": _as_int(item.get("weight"), 3, 1, 5),
            })
        elif isinstance(item, str) and item.strip():
            criteria.append({"name": item.strip()[:80], "description": "", "weight": 3})
    if not criteria:
        criteria = [{"name": "Matches the brief",
                     "description": user_prompt[:400] or "Meets the stated requirements.",
                     "weight": 5}]

    levels = []
    for item in (raw.get("qualification_levels") or []):
        if isinstance(item, dict) and item.get("label"):
            levels.append({"label": str(item["label"])[:60],
                           "definition": str(item.get("definition") or "")[:400]})
    # The rest of the system indexes these by position, so anything other than
    # exactly four is replaced wholesale rather than patched.
    if len(levels) != 4:
        levels = [dict(lv) for lv in DEFAULT_LEVELS]

    queries = _as_list(raw.get("discovery_queries"), [])
    queries = [q for q in queries if len(q) > 6][:20]

    return {
        "prompt": user_prompt,
        "title": str(raw.get("title") or "Lead research")[:80],
        "objective": str(raw.get("objective") or user_prompt)[:1000],
        "target_profile": str(raw.get("target_profile") or "")[:600],
        "target_leads": _as_int(raw.get("target_leads"), 60, 5, 1000),
        "regions": _as_list(raw.get("regions"), []),
        "discovery_queries": queries,
        "relevant_categories": _as_list(raw.get("relevant_categories"), []),
        "excluded_categories": _as_list(raw.get("excluded_categories"), []),
        # Names to keep out of the list entirely -- the user's own company, and
        # their competitors. A competitor matches the target profile better than
        # anyone else on earth, so without this it lands at High relevance near
        # the top of the call list and the user finds themselves in their own
        # lead sheet.
        "excluded_companies": _as_list(raw.get("excluded_companies"), [])[:40],
        "qualification_criteria": criteria[:6],
        "qualification_levels": levels,
        "evidence_types": _as_list(raw.get("evidence_types"), [
            "Local subsidiary or owned entity", "Distributor or dealer",
            "Named customer", "Named project", "Partnership or joint venture",
            "Case study or sales reference",
        ]),
        "priority_roles": _as_list(raw.get("priority_roles"), DEFAULT_ROLES)[:14],
        "people_per_company": _as_int(raw.get("people_per_company"), 3, 1, 8),
        "min_relevance_for_research": (
            raw.get("min_relevance_for_research")
            if raw.get("min_relevance_for_research") in RELEVANCE_LEVELS else "Medium"
        ),
        "extra_directory_domains": _as_list(raw.get("extra_directory_domains"), []),
        # A ceiling on cloud spend for one run. Retries used to multiply against
        # the per-stage attempt counter with nothing watching, so a model that
        # reliably returned malformed answers could turn a thirty-cent run into
        # a thirty-dollar one without a number appearing anywhere.
        "spend_limit_usd": float(raw.get("spend_limit_usd") or 5.0),
        # Who is writing, and what they are offering. The Drafter cannot write
        # anything but boilerplate without this, so it is carried on the plan
        # rather than buried in settings -- the user sees it and edits it.
        "sender_profile": str(raw.get("sender_profile") or "")[:1200],
        "outreach_channel": (raw.get("outreach_channel")
                             if raw.get("outreach_channel") in
                             ("email", "linkedin", "call") else "email"),
    }


def make_plan(user_prompt: str) -> dict:
    """Ask the AI to turn a free-text prompt into a plan."""
    user_prompt = (user_prompt or "").strip()
    if len(user_prompt) < 15:
        raise ValueError(
            "Tell me a bit more about the leads you want - who they are, what "
            "they make or do, and what would make one worth contacting."
        )

    # Role, token budget and temperature all come from the agent registry.
    raw = get_client("planner").ask_json(PROMPT.format(prompt=user_prompt))
    plan = normalise_plan(raw, user_prompt)

    if not plan["discovery_queries"]:
        # Without queries the discover stage has nothing to do, so fall back to
        # something derived from the prompt rather than shipping an empty plan.
        plan["discovery_queries"] = fallback_queries(user_prompt, plan)
    return plan


def fallback_queries(user_prompt: str, plan: dict) -> list[str]:
    """Queries built from the prompt when the model gave none usable."""
    regions = plan.get("regions") or [""]
    cats = plan.get("relevant_categories") or [user_prompt[:60]]
    out: list[str] = []
    for cat in cats[:4]:
        for region in regions[:3]:
            suffix = f" in {region}" if region else ""
            out.append(f"top {cat} manufacturers{suffix}")
            out.append(f"{cat} suppliers directory{suffix}")
    return out[:12]


EXPAND_PROMPT = """These web searches have found {found} companies so far, but the
target is {target}.

THE GOAL: {objective}

SEARCHES ALREADY TRIED:
{tried}

COMPANIES ALREADY FOUND (do not search for these by name):
{found_names}

Write {count} NEW search queries that would surface DIFFERENT companies matching
the goal. Use angles not tried yet: trade associations, regional directories,
trade-show exhibitor lists, industry award lists, "suppliers to <big customer>",
sub-categories of the product, and neighbouring regions.

Return JSON: {{"queries": ["...", "..."]}}"""


def expand_queries(plan: dict, tried: list[str], found_names: list[str],
                   count: int = 6) -> list[str]:
    """More queries when discovery has not hit the target yet."""
    try:
        raw = get_client("planner").ask_json(
            EXPAND_PROMPT.format(
                found=len(found_names), target=plan.get("target_leads", 60),
                objective=plan.get("objective", ""),
                tried="\n".join(f"- {q}" for q in tried[-20:]),
                found_names=", ".join(found_names[:40]) or "(none yet)",
                count=count,
            ),
            max_tokens=800, temperature=0.6,
        )
        return [q for q in _as_list(raw.get("queries"), []) if len(q) > 6][:count]
    except Exception:  # noqa: BLE001 - running out of queries is not a crash
        log.debug("query expansion failed", exc_info=True)
        return []

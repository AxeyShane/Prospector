"""Stage 5 -- qualify: does this company actually meet the plan?

The stage the tool exists for. Everything before it is plumbing to get here.

Two evidence sources are combined:
  1. the company's own pages, filtered to the passages that mention a
     qualification keyword or a target region -- the model is not asked to find
     a needle in 40,000 characters of product specs
  2. targeted web searches, one per criterion and per region. Search snippets
     carry the answer surprisingly often ("Distributor in Canada and
     Australia") without any page needing to be fetched

The criteria come from the plan, so the same code qualifies "already supplies
into Australia" and "has more than 50 vehicles in its fleet" without changing.

Three guards keep the answers honest, because an invented customer is worse
than no answer at all: the model may only cite text it was shown, a country
appearing in a website language-picker is explicitly not evidence, and a top
rating returned with no evidence is downgraded in code rather than trusted.
"""

from __future__ import annotations

import json
import logging
import re

from prospector.config import load_plan
from prospector.database import (
    MAX_ATTEMPTS, get_connection, get_pages, update, utc_now,
)
from prospector.llm import get_client
from prospector.stages._runner import relevance_filter, run_batch, select_pending
from prospector.websearch import search_many

log = logging.getLogger(__name__)

EVIDENCE_KEYWORDS = (
    "subsidiary", "distributor", "dealer", "office", "branch", "warehouse",
    "export", "exported", "exports", "worldwide", "global", "overseas",
    "international", "partner", "joint venture", "acquired", "acquisition",
    "customer", "client", "reference", "installed", "supplied", "presence",
    "certified", "approved", "accredited", "contract", "awarded", "fleet",
    "pty", "gmbh", "inc.", "llc", "b.v.", "s.p.a", "ltd.",
)

# Words that appear in almost every criterion description and would therefore
# keep every line on the page, defeating the point of filtering.
_STOPWORDS = frozenset({
    "company", "companies", "business", "should", "which", "their", "there",
    "these", "those", "would", "could", "about", "where", "whether", "evidence",
    "relevant", "criteria", "criterion", "example", "examples", "including",
    "products", "product", "market", "markets", "supply", "supplier",
})

# Superseded by this agent's `role` in agents.py, which is what actually
# gets sent. Kept here only so the prompt below reads in context.
_SYSTEM_REFERENCE = (
    "You are a due-diligence researcher qualifying sales leads. You are "
    "sceptical of marketing language: 'global presence' and 'trusted "
    "worldwide' are NOT evidence unless a specific country, company, project "
    "or number is named. You cite only what appears in the supplied text. "
    "You answer only with JSON."
)

PROMPT = """Judge this company against the qualification criteria.

COMPANY: {name}
WEBSITE: {website}
WHAT THEY DO: {products}

WHAT WE ARE LOOKING FOR: {objective}
{regions}

QUALIFICATION CRITERIA -- these decide the answer:
{criteria}

TYPES OF EVIDENCE THAT COUNT:
{evidence_types}

RATING LEVELS -- choose exactly one label, copied exactly:
{levels}

=== EVIDENCE FROM THE COMPANY'S OWN WEBSITE ===
{site_evidence}

=== EVIDENCE FROM WEB SEARCH (title / url / snippet) ===
{search_evidence}

RULES:
- Cite ONLY facts that appear in the text above. Do not use prior knowledge
  about this company. Do not guess. Do not infer from the company's size.
- A country or place appearing only in a website language-picker, or in a
  dropdown listing every country in the world, is NOT evidence. Ignore it.
- A vague claim with no specific name, place or number attached is at best the
  third level down, never the top one.
- Write each evidence item the way a salesperson would note it before a call,
  naming the specific entity: "Australian subsidiary - Acme Pty Ltd, Perth WA",
  "Named customer in target market - ABC Mining, Nevada".

Return JSON with exactly these keys:
{{
  "level": "one of the rating labels above, copied exactly",
  "matched_criteria": ["names of the criteria this company actually meets"],
  "evidence": ["short, specific evidence statements, [] if none found"],
  "sources": ["the URLs from the text above that support the evidence"],
  "summary": "one or two sentences a salesperson can read before making contact"
}}"""


def _keywords(plan: dict) -> list[str]:
    """Words worth keeping a line for: evidence verbs, regions, criteria terms."""
    words = list(EVIDENCE_KEYWORDS)
    words += [r.lower() for r in (plan.get("regions") or [])]
    # The criterion *description* is where the plan says what would prove the
    # criterion -- mining only the name threw away the useful half. Same for
    # the evidence types the planner listed.
    for crit in plan.get("qualification_criteria") or []:
        for field in ("name", "description"):
            words += [w.lower() for w in re.findall(r"[A-Za-z]{5,}",
                                                    str(crit.get(field, "")))]
    for ev in plan.get("evidence_types") or []:
        words += [w.lower() for w in re.findall(r"[A-Za-z]{5,}", str(ev))]
    return [w for w in dict.fromkeys(words) if w not in _STOPWORDS]


def _relevant_passages(text: str, keywords: list[str], max_chars: int) -> str:
    """Keep only lines that mention something the criteria care about.

    A manufacturer homepage is mostly product specs. Filtering first means the
    model sees ten useful lines instead of one useless page, which both cuts
    cost and measurably improves the answer -- especially on a small local
    model with a short context window.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    hits = set()
    for i, line in enumerate(lines):
        # 12, not 15: "Perth, WA 6000" is eleven characters and is exactly the
        # kind of line the criteria are asking about.
        if len(line) < 12:
            continue
        low = line.lower()
        if any(k in low for k in keywords):
            # One line either side. A distributor table puts the country on one
            # row and the company name on the next, and matching only the hit
            # line hands the model half a fact.
            hits.update((i - 1, i, i + 1))

    kept: list[str] = []
    total = 0
    for i in sorted(hits):
        if not (0 <= i < len(lines)) or len(lines[i]) < 4:
            continue
        kept.append(lines[i])
        total += len(lines[i])
        if total >= max_chars:
            break
    return "\n".join(kept)


def _site_evidence(row, keywords: list[str],
                   max_chars: int = 6000) -> tuple[str, list[str]]:
    """Relevant passages from the crawled pages, plus the URLs they came from.

    The URLs are returned so the caller can check the model's cited sources
    against what it was actually shown.
    """
    pages = get_pages(row)
    if not pages:
        return "(no website content available)", []
    parts, urls, budget = [], [], max_chars
    for url, text in pages.items():
        if budget <= 0:
            break
        passages = _relevant_passages(text, keywords, min(budget, 2500))
        if passages:
            parts.append(f"[{url}]\n{passages}")
            urls.append(url)
            budget -= len(passages)
    if not parts:
        return "(website content contained no relevant passages)", []
    return "\n\n".join(parts), urls


def build_queries(company_name: str, plan: dict, limit: int = 4) -> list[str]:
    """Search queries for one company: per-region, per-criterion, then generic."""
    name = company_name.strip()

    # The generic presence query is reserved a slot rather than appended last.
    # It is the single highest-yield query of the set -- it is the one that
    # surfaces "Acme Pty Ltd, Dandenong South" -- and when the budget was cut
    # from six queries to four, appending it left it permanently truncated away
    # on any plan with two regions and two criteria.
    generic = f'"{name}" distributor OR subsidiary OR "office in"'

    queries = [f'"{name}" {region}' for region in (plan.get("regions") or [])[:2]]
    for crit in (plan.get("qualification_criteria") or [])[:2]:
        terms = " ".join(re.findall(r"[A-Za-z]{4,}", crit.get("name", ""))[:4])
        if terms:
            queries.append(f'"{name}" {terms}')

    queries = list(dict.fromkeys(queries))[: max(limit - 1, 1)]
    queries.append(generic)
    if len(queries) < limit:
        queries.append(f'"{name}" customers OR clients OR "case study"')
    return list(dict.fromkeys(queries))[:limit]


def _search_evidence(company_name: str, plan: dict,
                     max_chars: int = 6000) -> tuple[str, list[str]]:
    results = search_many(build_queries(company_name, plan), max_results=6)
    if not results:
        return "(no search results -- search may be rate limited)", []

    lines, urls, total = [], [], 0
    for r in results:
        block = f"{r.title}\n{r.url}\n{r.snippet}"
        if total + len(block) > max_chars:
            break
        lines.append(block)
        urls.append(r.url)
        total += len(block)
    return "\n\n".join(lines), urls


def normalise_level(raw: str, levels: list[dict]) -> str:
    """Map whatever the model said onto one of the plan's four labels.

    Order matters more than it looks. A bare "Match" used to come back as
    "Strong match", because the old code asked whether a label contained the
    answer *or* the answer contained a label, and "match" is a substring of
    "strong match" -- so the single most likely one-word reply a model can give
    was silently promoted to the best rating, straight to the top of the call
    list. Likewise "Not a match" mapped to "Unclear".

    So: exact matches, then an explicit paraphrase table with the negatives
    tested first (every negative phrase contains a positive word), then a
    whole-phrase containment test that requires the *label* to appear in the
    answer and never the other way round. Anything else is "Unclear", which is
    the honest reading of an answer we could not parse.
    """
    labels = [lv["label"] for lv in levels]
    if not labels:
        return "Unclear"

    default = labels[2] if len(labels) > 2 else labels[-1]

    raw = (raw or "").strip()
    if len(raw) < 3:
        return default

    if raw in labels:
        return raw

    low = raw.lower()
    for label in labels:
        if label.lower() == low:
            return label

    # Negatives first: "not a match" and "no match" both contain "match".
    for keys, index in (
        (("not a match", "no match", "does not", "doesn't", "no evidence",
          "not relevant", "not suitable", "none", "no fit"), 3),
        (("partial", "some evidence", "possible", "possibly", "weak", "partly",
          "indirect"), 1),
        (("unclear", "unknown", "cannot tell", "can't tell", "insufficient",
          "limited", "uncertain", "no data"), 2),
        (("strong", "full match", "clear match", "definite", "confirmed",
          "yes"), 0),
    ):
        if any(k in low for k in keys):
            return labels[index] if index < len(labels) else default

    # Whole-phrase containment, one direction only.
    for label in labels:
        if re.search(r"\b" + re.escape(label.lower()) + r"\b", low):
            return label

    return default   # "Unclear" is the honest answer when we cannot tell


def score_for(level: str, matched: list[str], plan: dict) -> int:
    """0-100. Level dominates; matched criteria break ties by their weight."""
    labels = [lv["label"] for lv in plan.get("qualification_levels", [])]
    base = {0: 70, 1: 40, 2: 15, 3: 0}.get(
        labels.index(level) if level in labels else 2, 15)

    criteria = plan.get("qualification_criteria") or []
    total_weight = sum(int(c.get("weight", 3)) for c in criteria) or 1
    hit_weight = sum(
        int(c.get("weight", 3)) for c in criteria
        if any(c.get("name", "").lower() in m.lower()
               or m.lower() in c.get("name", "").lower() for m in matched)
    )
    return min(100, base + int(30 * hit_weight / total_weight))


def qualify_one(row, plan: dict) -> dict:
    levels = plan.get("qualification_levels") or []
    keywords = _keywords(plan)

    site_ev, site_urls = _site_evidence(row, keywords)
    search_ev, search_urls = _search_evidence(row["company_name"], plan)

    # Nothing was read and nothing was found. Asking the model to rate a company
    # from its name and the prompt boilerplate produces a confident-looking
    # answer built on air -- and when the free search endpoint blocks mid-run,
    # that happens to every remaining company at once, filling the spreadsheet
    # with fabricated "Partial match" ratings.
    #
    # Raised rather than returned as a verdict. Writing "Unclear" here would
    # stamp `qualified_at` and take the row out of the pending query for good,
    # so a ten-minute search outage would become a permanent answer and pressing
    # Continue -- which is what the app tells the user to do -- would skip every
    # company it touched. Raising leaves the row pending and retries it.
    if not site_urls and not search_urls:
        raise ValueError(
            "no website content and no search results were available for this "
            "company - it will be tried again"
        )

    criteria_lines = [
        f"- {c.get('name')} (importance {c.get('weight', 3)}/5): {c.get('description', '')}"
        for c in (plan.get("qualification_criteria") or [])
    ]
    # Built with a loop, not a comprehension: Python 3.11 f-strings cannot
    # contain a backslash, and the definitions need whitespace collapsing.
    level_lines = []
    for lv in levels:
        definition = re.sub(r"\s+", " ", str(lv.get("definition", ""))).strip()
        level_lines.append(f'- "{lv.get("label", "")}": {definition}')
    regions = plan.get("regions") or []

    prompt = PROMPT.format(
        name=row["company_name"],
        website=row["website"] or "(not found)",
        products=row["products"] or "(unknown)",
        objective=plan.get("objective", ""),
        regions=f"REGIONS THAT MATTER: {', '.join(regions)}" if regions else "",
        criteria="\n".join(criteria_lines),
        evidence_types="\n".join(f"- {e}" for e in plan.get("evidence_types", [])),
        levels="\n".join(level_lines),
        site_evidence=site_ev,
        search_evidence=search_ev,
    )

    data = get_client("qualify").ask_json(prompt)

    level = normalise_level(str(data.get("level", "")), levels)

    def as_list(value):
        if isinstance(value, str):
            return [value] if value.strip() else []
        return [str(v) for v in value] if isinstance(value, list) else []

    evidence = as_list(data.get("evidence"))
    matched = as_list(data.get("matched_criteria"))

    # Sources are kept only when they are a URL we actually put in front of the
    # model. The old fallback -- "no sources returned? use every URL from the
    # six searches" -- credited a claim read off the company's own site to ten
    # unrelated search hits, and those URLs went into the spreadsheet's Sources
    # column as though the rating cited them. A model-invented URL was exported
    # verbatim for the same reason.
    shown = set(site_urls) | set(search_urls)
    sources = [u for u in as_list(data.get("sources")) if u in shown]

    # The one failure mode that would actively mislead: a top rating with
    # nothing behind it. Downgraded here rather than trusted.
    labels = [lv["label"] for lv in levels]
    if labels and level == labels[0] and not evidence:
        level = labels[2] if len(labels) > 2 else labels[-1]

    summary = str(data.get("summary", "")).strip()
    body = "\n".join(f"- {e}" for e in evidence[:8]) if evidence else \
        "No specific evidence found in public sources."
    full = f"{summary}\n\n{body}".strip() if summary else body

    return {
        "qualification_level": level,
        "qualification_evidence": full[:4000],
        "qualification_matched": ", ".join(matched[:8]),
        "qualification_sources": json.dumps(sources[:10]),
        "qualification_score": score_for(level, matched, plan),
    }


def pending_sql(plan: dict | None = None) -> str:
    plan = plan or load_plan()
    min_rel = plan.get("min_relevance_for_research", "Medium")
    return (
        "SELECT * FROM leads WHERE qualification_level IS NULL "
        f"AND {relevance_filter(min_rel)} "
        f"AND COALESCE(qualify_attempts, 0) < {MAX_ATTEMPTS} "
        "ORDER BY CASE relevance WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, "
        "company_key"
    )


def run_qualify(workers: int = 3, limit: int | None = None) -> dict:
    plan = load_plan()
    rows = select_pending(pending_sql(plan), limit=limit)

    def handler(row):
        fields = qualify_one(row, plan)
        update(row["company_key"], conn=get_connection(),
               qualified_at=utc_now(), qualify_error=None, **fields)
        return fields["qualification_level"]

    # Deliberately fewer workers than other stages: this one issues six web
    # searches per company, and hammering the free search endpoint gets it
    # blocked for the whole run.
    return run_batch("qualify", rows, handler, workers=min(workers, 4),
                     label="Qualifying leads")

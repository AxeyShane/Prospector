"""Stage 1 -- discover: find the companies in the first place.

The plan's `discovery_queries` go through free web search. Two things are read
from the results:

  * the result list itself -- titles and snippets often name companies directly
  * pages that look like *lists* ("top 20 X manufacturers", "members
    directory", "exhibitor list") are fetched and mined, because one good
    listicle is worth twenty ordinary search results

When the queries run dry before the target is met, the planner writes fresh
ones from different angles and the loop continues. That is what turns a
one-line prompt into a few hundred named companies.

Search results are noisy by nature, so the extraction prompt is strict about
what counts as a company: no magazines, no directories, no government bodies,
no job boards.
"""

from __future__ import annotations

import logging
import re

from rich.console import Console

from prospector.config import load_directory_domains
from prospector.database import get_connection, normalise_key, upsert_lead
from prospector.fetcher import fetch_html, html_to_text
from prospector.llm import LLMError, get_client
from prospector.planner import expand_queries
from prospector.websearch import search

log = logging.getLogger(__name__)
console = Console()

# Result titles that suggest the page is a list of companies worth fetching.
LIST_HINTS = (
    "top ", "best ", "list of", "directory", "members", "exhibitor",
    "suppliers", "manufacturers in", "companies in", "leading ", "largest ",
)

# Superseded by this agent's `role` in agents.py, which is what actually
# gets sent. Kept here only so the prompt below reads in context.
_SYSTEM_REFERENCE = (
    "You extract company names from web search results. You return only real "
    "operating companies, never publications, directories, marketplaces, "
    "government bodies, associations or job boards. You answer only with JSON."
)

PROMPT = """Extract company names from these web search results.

WHAT WE ARE LOOKING FOR: {objective}
AN IDEAL COMPANY LOOKS LIKE: {profile}
{regions}

SEARCH RESULTS:
{results}

RULES:
- Return only companies that plausibly match what we are looking for.
- Return the company's own name, cleanly. Strip taglines, locations and
  separators: "Acme Crushers | Jaw Crushers India" becomes "Acme Crushers".
- Do NOT return: magazines, news sites, directories (IndiaMART, Yellow Pages),
  marketplaces, associations, government departments, consultancies writing
  about the industry, job boards, or the search engine itself.
- If a result is a list article, extract every company named in its title or
  snippet.
- If nothing in these results is a matching company, return an empty list.

Return JSON: {{"companies": [{{"name": "...", "source_url": "the url it came from"}}]}}"""


def _looks_like_list_page(title: str) -> bool:
    low = (title or "").lower()
    return any(hint in low for hint in LIST_HINTS)


def _clean_name(name: str) -> str:
    """Trim the decoration search results wrap company names in."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    # Cut at the first separator: "Acme Ltd - Crushers for Mining" -> "Acme Ltd"
    name = re.split(r"\s+[|–—:]\s+|\s+-\s+", name)[0].strip()
    name = name.strip(" .,-–—\"'")
    return name


def _plausible(name: str, directories: set[str]) -> bool:
    """Cheap sanity checks before a name reaches the database."""
    if not name or len(name) < 3 or len(name) > 90:
        return False
    if not re.search(r"[A-Za-z]", name):
        return False
    low = name.lower()
    # The ~90-name block list is passed in and used. It used to be passed in and
    # ignored in favour of the six hardcoded names below, so Kompass, ThomasNet,
    # Europages, Alibaba, Zauba and 10times all entered the lead list as though
    # they were companies -- and the user's own `extra_directory_domains` did
    # nothing at all.
    #
    # Matched as a whole word, and only when the name is essentially just that
    # word. A plain substring test over ninety domains plus the user's own
    # additions is a minefield: `trade.gov` in the plan discarded "Trade Winds
    # Engineering", and `medium`, `manta` and `indeed` are on the shipped list
    # already. A directory's name showing up *inside* a longer company name is
    # almost always a coincidence; a lead called exactly "IndiaMART" is not.
    blocked = {d.split(".")[0] for d in directories}
    blocked.update(("indiamart", "tradeindia", "linkedin", "facebook",
                    "wikipedia", "youtube"))
    bare = re.sub(r"[^a-z0-9]+", "", low)
    if any(bare == b for b in blocked if len(b) > 3):
        return False
    # "Kompass India" and "IndiaMART Global" are still the directory, but only
    # because the second word carries no identity. Blocking any two-word name
    # beginning with a directory's brand went too far -- "Manta Equipment" reads
    # like a real company and is treated as one.
    tokens = [t for t in re.findall(r"[a-z0-9]+", low) if len(t) > 2]
    if (len(tokens) == 2 and len(tokens[0]) > 3 and tokens[0] in blocked
            and tokens[1] in ("india", "global", "online", "directory", "business",
                              "network", "portal", "listings", "marketplace")):
        return False
    # Article titles that survived the split ("10 Best Crusher Manufacturers")
    if re.match(r"^\d+\s", name) or low.startswith(
            ("top ", "best ", "list of", "how ", "why ", "what ", "leading ",
             "the 10", "the 5", "the best", "our ", "these ")):
        return False
    # Whole words only. "news" as a substring killed Newson and Newsome, which
    # are real company names.
    if re.search(r"\b(vs|versus|review|reviews|news|magazine|blog|directory|"
                 r"marketplace|suppliers|manufacturers|companies)\b", low):
        return False
    if " guide to " in low:
        return False
    return True


def _excluded(name: str, plan: dict) -> bool:
    """Is this the user's own company, or one they told us to leave out?

    A competitor matches the target profile better than anyone else on the list,
    so without this it reliably lands at High relevance near the top of the call
    list -- and the user finds their own company in their own lead sheet.

    Matched on normalised names, never as a raw substring. `ex in low` with a
    short entry is indiscriminate: excluding "TIL" dropped "Utility Engineering
    Works" -- and because uploaded lists run through this too, the user's own
    companies vanished with the misleading explanation that they were on the
    exclusion list.
    """
    from prospector.database import normalise_key

    key = normalise_key(name)
    if not key:
        return False
    tokens = set(key.split())
    for excluded in plan.get("excluded_companies") or []:
        ex_key = normalise_key(str(excluded))
        if not ex_key:
            continue
        if key == ex_key:
            return True
        # A multi-word exclusion still matches a longer legal name for the same
        # company ("Acme Crushers" excludes "Acme Crushers International"), but
        # a single short token has to match a whole token to count.
        ex_tokens = ex_key.split()
        if len(ex_tokens) > 1 and ex_tokens and set(ex_tokens) <= tokens:
            return True
        if len(ex_tokens) == 1 and len(ex_tokens[0]) >= 5 and ex_tokens[0] in tokens:
            return True
    return False


def _extract(results, plan: dict, directories: set[str]) -> list[dict]:
    """Ask the AI for company names in a batch of search results."""
    if not results:
        return []

    blob = "\n\n".join(f"{r.title}\n{r.url}\n{r.snippet}" for r in results[:12])
    regions = plan.get("regions") or []
    prompt = PROMPT.format(
        objective=plan.get("objective", ""),
        profile=plan.get("target_profile", ""),
        regions=f"THEY SHOULD BE BASED IN: {', '.join(regions)}" if regions else "",
        results=blob[:9000],
    )

    try:
        data = get_client("discover").ask_json(prompt)
    except LLMError as exc:
        log.warning("name extraction failed: %s", exc)
        return []

    out = []
    for item in (data.get("companies") or data.get("items") or []):
        if isinstance(item, str):
            item = {"name": item, "source_url": ""}
        if not isinstance(item, dict):
            continue
        name = _clean_name(str(item.get("name", "")))
        if _plausible(name, directories) and not _excluded(name, plan):
            out.append({"name": name, "source_url": str(item.get("source_url", ""))[:400]})
    return out


def _mine_list_page(url: str, plan: dict, directories: set[str]) -> list[dict]:
    """Fetch a listicle or directory page and pull the company names out of it."""
    html = fetch_html(url)
    if not html:
        return []
    _, text = html_to_text(html, max_chars=9000)
    if len(text) < 300:
        return []

    prompt = PROMPT.format(
        objective=plan.get("objective", ""),
        profile=plan.get("target_profile", ""),
        # Labelled, exactly as in _extract. This slot used to get a bare
        # "Australia" with no instruction attached -- on the single source that
        # contributes the most names, so a listicle covering China and Turkey
        # dumped thirty wrong-country companies into the list.
        regions=(f"THEY SHOULD BE BASED IN: {', '.join(plan.get('regions') or [])}"
                 if plan.get("regions") else ""),
        results=f"PAGE: {url}\n{text}",
    )
    try:
        data = get_client("discover").ask_json(prompt, max_tokens=1500)
    except LLMError:
        return []

    out = []
    for item in (data.get("companies") or []):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = _clean_name(str(item.get("name", "")))
        if _plausible(name, directories) and not _excluded(name, plan):
            out.append({"name": name, "source_url": url})
    return out


def run_discover(plan: dict, max_rounds: int = 3, mine_pages: int = 25,
                 on_progress=None) -> dict:
    """Harvest company names until the plan's target is met or queries run out."""
    directories = load_directory_domains()
    conn = get_connection()
    target = int(plan.get("target_leads", 60))

    def total() -> int:
        return conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

    def usable() -> int:
        """Leads still worth counting towards the target.

        Counting every row in the table meant: a user who uploaded sixty
        companies got no discovery at all; a run where half the first sixty
        turned out irrelevant stopped anyway and never backfilled; and a second
        run was a silent no-op because the target was already "met". None of
        that matches what the number on screen says it means.
        """
        return conn.execute(
            "SELECT COUNT(*) FROM leads WHERE relevance IS NULL "
            "OR relevance IN ('High', 'Medium')"
        ).fetchone()[0]

    queries = list(plan.get("discovery_queries") or [])
    tried: list[str] = []
    added = 0
    pages_mined = 0
    searches_run = 0
    searches_empty = 0

    for round_no in range(1, max_rounds + 1):
        if not queries:
            break

        for query in queries:
            if usable() >= target:
                break
            tried.append(query)

            results = search(query, max_results=10)
            searches_run += 1
            if not results:
                searches_empty += 1
                continue

            found = _extract(results, plan, directories)

            # One good list page is worth many ordinary results. A listicle
            # yields fifteen to forty names against roughly two from a snippet,
            # so this is the single biggest lever on recall -- and it used to be
            # capped at six pages for an entire run, across only the first four
            # results of each query.
            if pages_mined < mine_pages:
                for r in results:
                    if usable() >= target or pages_mined >= mine_pages:
                        break
                    host = r.url.split("/")[2].lower() if "://" in r.url else ""
                    if any(host.endswith(d) for d in directories):
                        continue
                    if _looks_like_list_page(r.title):
                        pages_mined += 1
                        found.extend(_mine_list_page(r.url, plan, directories))

            for item in found:
                if usable() >= target:
                    break
                if upsert_lead(item["name"], source_list="discovered",
                               discovered_via=query, source_url=item["source_url"],
                               conn=conn):
                    added += 1
                    if on_progress:
                        on_progress({"found": usable(), "target": target,
                                     "company": item["name"], "query": query})

            console.print(f"  [dim]{query[:60]}[/dim] -> {usable()}/{target} companies")

        if usable() >= target:
            break

        # Out of queries but short of target: get new angles from the planner.
        if round_no < max_rounds:
            names = [r[0] for r in conn.execute(
                "SELECT company_name FROM leads ORDER BY seeded_at DESC LIMIT 40"
            ).fetchall()]
            queries = expand_queries(plan, tried, names, count=6)
            if queries:
                console.print(f"  [cyan]Trying {len(queries)} new search angles...[/cyan]")
        else:
            queries = []

    final = usable()

    # Every search came back empty. That is not "no companies match your brief"
    # -- it is the free search endpoint refusing to answer, or no connection at
    # all. Reporting it as a clean run with a suggestion to widen the regions
    # sent users off editing a plan that was never the problem.
    if searches_run and searches_empty == searches_run:
        return {
            "status": "error", "added": added, "total": final, "target": target,
            "queries_tried": len(tried), "searches_run": searches_run,
            "error": "search_unavailable",
            "message": (
                "Web search did not answer any of the "
                f"{searches_run} searches. Either this computer is offline, or "
                "the free search service has temporarily blocked us for "
                "searching too quickly. Nothing found so far is lost - wait "
                "ten minutes and press Continue."),
        }

    if final < target:
        console.print(
            f"  [yellow]Found {final} of {target} companies.[/yellow] "
            f"Widen the regions or categories in the plan to find more."
        )
    return {"status": "ok", "added": added, "total": final, "target": target,
            "queries_tried": len(tried), "searches_run": searches_run,
            "searches_empty": searches_empty}

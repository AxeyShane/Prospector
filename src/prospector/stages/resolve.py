"""Stage 2 -- resolve: find each company's official website.

This stage is load-bearing. Small companies rank below directory listings for
their own name, so a naive "first search result" would hand every downstream
stage a marketplace page instead of the company's own words. Candidates are therefore scored on name/domain overlap and directory
domains are rejected outright.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from prospector.config import load_directory_domains, load_plan
from prospector.database import (
    MAX_ATTEMPTS, get_connection, merge_duplicate_websites, update, utc_now,
)
from prospector.stages._runner import run_batch, select_pending
from prospector.websearch import search, tokenise

log = logging.getLogger(__name__)

PENDING_SQL = (
    "SELECT * FROM leads WHERE website IS NULL "
    f"AND COALESCE(resolve_attempts, 0) < {MAX_ATTEMPTS} "
    "ORDER BY company_key"
)

# Words that carry no identifying signal when matching a name to a domain.
_STOP = {
    "the", "and", "of", "india", "indian", "private", "pvt", "limited", "ltd",
    "llp", "llc", "inc", "corporation", "corp", "company", "co", "gmbh", "ag",
    "industries", "industry", "engineering", "engineers", "enterprises",
    "enterprise", "works", "international", "group", "solutions", "systems",
    "technologies", "technology", "products", "equipments", "equipment",
    "manufacturing", "machinery", "machines", "global", "services",
}


_WORD_RE = re.compile(r"[a-z0-9]+")


def _name_tokens(name: str) -> list[str]:
    """Identifying tokens in the order they appear in the name.

    Order matters and length does not: companies lead with their brand, so
    "PUZZOLANA MACHINERY FABRICATORS" is identified by "puzzolana", not by
    the longer but generic "fabricators". Sorting by length here was a real
    bug -- it scored puzzolana.com at 0.295 and would have rejected the
    company's own website.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for tok in _WORD_RE.findall(name.lower()):
        if len(tok) > 2 and tok not in _STOP and tok not in seen:
            seen.add(tok)
            tokens.append(tok)
    return tokens


def _token_in_domain(token: str, domain: str) -> bool:
    """Does this name token appear in the domain as a token, not as letters?

    A plain substring test is fine for a distinctive brand and disastrous for a
    short one: "Ace Engineering" scored 0.65 against both spaceage-india.com and
    aceternity.com -- nearly double the accept floor -- because "ace" happens to
    appear inside both. Every three- and four-letter Indian brand (Ace, MRF,
    TVS, Elgi, KEC) resolved to whatever unrelated domain contained its letters,
    and the whole pipeline then read a stranger's website.
    """
    if len(token) > 4:
        return token in domain
    return re.search(r"(?:^|[^a-z0-9])" + re.escape(token) + r"(?:$|[^a-z0-9])",
                     domain) is not None


def score_candidate(company_name: str, url: str, title: str,
                    directories: set[str], regions: list[str] | None = None) -> float:
    """Score 0..1 for how likely `url` is this company's own website."""
    try:
        netloc = urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return 0.0
    if not netloc:
        return 0.0

    if any(netloc == d or netloc.endswith("." + d) for d in directories):
        return 0.0

    domain_body = netloc.split(".")[0]
    domain_flat = re.sub(r"[^a-z0-9]", "", netloc)
    domain_loose = re.sub(r"[^a-z0-9-]", "", netloc)
    tokens = _name_tokens(company_name)
    if not tokens:
        return 0.0

    score = 0.0

    # Strongest signal of all: the whole name, concatenated, is the domain --
    # "aceengineering.in", "sterlingengineering.co.uk". This is tested on the
    # *unstripped* words, because the industry word that `_name_tokens` throws
    # away as generic is usually the half that makes a short brand identifiable:
    # "Ace" needs a token boundary to be trusted alone, and needs none at all
    # when "engineering" sits right beside it in the domain.
    _legal = {"pvt", "private", "ltd", "limited", "llp", "llc", "inc", "co",
              "corp", "corporation", "gmbh", "ag", "bv", "pty", "the"}
    joined_full = "".join(w for w in _WORD_RE.findall(company_name.lower())
                          if w not in _legal)
    if len(joined_full) >= 8 and joined_full in domain_flat:
        score += 0.60
    elif _token_in_domain(tokens[0], domain_body):
        score += 0.50
    elif any(_token_in_domain(t, domain_body) for t in tokens[1:4]):
        score += 0.35
    elif _token_in_domain(tokens[0], domain_loose):
        score += 0.30

    # Further name tokens appearing in the domain add confidence.
    extra = sum(1 for t in tokens[1:4] if _token_in_domain(t, domain_flat))
    score += min(extra * 0.075, 0.15)

    # Title overlap catches companies whose domain is an acronym. Weighted well
    # below the domain signals: a search result title is written by whoever runs
    # the page, and a directory listing repeats the company name perfectly.
    title_tokens = tokenise(title)
    overlap = len(set(tokens) & title_tokens) / max(len(tokens), 1)
    score += overlap * 0.15

    # The plan's countries break same-brand ties. Without this there was no
    # country signal at all, and on an Australia-only brief "Sterling
    # Engineering" resolved to sterling-eng.in over sterlingengineering.co.uk
    # -- then fed the wrong company's website into every stage after it.
    regions = regions or []
    region_cc = {
        "australia": (".au",), "new zealand": (".nz",), "united kingdom": (".uk",),
        "uk": (".uk",), "britain": (".uk",), "united states": (".us",),
        "usa": (".us",), "canada": (".ca",), "india": (".in",),
        "germany": (".de",), "france": (".fr",), "italy": (".it",),
        "spain": (".es",), "south africa": (".za",), "brazil": (".br",),
        "indonesia": (".id",), "chile": (".cl",), "peru": (".pe",),
    }
    wanted: tuple[str, ...] = ()
    for region in regions:
        wanted += region_cc.get(str(region).strip().lower(), ())
    if wanted and netloc.endswith(wanted):
        score += 0.15
    low_title = (title or "").lower()
    if any(str(r).strip().lower() in low_title for r in regions if len(str(r)) > 3):
        score += 0.05

    # An ordinary company TLD is mild corroboration. Country TLDs are handled
    # above, against the plan, rather than being rewarded unconditionally --
    # ".in" used to score the same as ".com" on every brief in the world.
    if netloc.endswith((".com", ".net", ".org", ".co", ".io", ".biz")):
        score += 0.05

    return round(min(score, 1.0), 3)


def resolve_one(company_name: str, directories: set[str] | None = None,
                plan: dict | None = None) -> tuple[str, float, list]:
    """Return (best_url, confidence, rejected_candidates) for one company."""
    directories = directories if directories is not None else load_directory_domains()
    if plan is None:
        plan = load_plan()
    regions = [str(r) for r in (plan.get("regions") or [])]

    # The second query used to be the literal string "{name} manufacturer
    # India", whatever the plan said. On a UK or Australian brief it actively
    # steered the search towards the wrong country's namesake.
    where = regions[0] if regions else ""
    queries = [
        f'"{company_name}" official website',
        f"{company_name} official site {where}".strip(),
    ]

    scored: list[tuple[float, str, str]] = []
    seen_domains: set[str] = set()

    for query in queries:
        for result in search(query, max_results=8):
            netloc = urlparse(result.url).netloc.lower().removeprefix("www.")
            if netloc in seen_domains:
                continue
            seen_domains.add(netloc)
            score = score_candidate(company_name, result.url, result.title,
                                    directories, regions)
            if score > 0:
                # Keep the site root, not the deep page the search happened to hit.
                parsed = urlparse(result.url)
                root = f"{parsed.scheme}://{parsed.netloc}"
                scored.append((score, root, result.title))
        if scored and max(s for s, _, _ in scored) >= 0.7:
            break  # confident enough; skip the second query and its delay

    if not scored:
        return "", 0.0, []

    scored.sort(key=lambda x: -x[0])
    best_score, best_url, _ = scored[0]

    # Below this, a wrong site would feed false evidence into the presence
    # stage -- far worse than having no site at all.
    if best_score < 0.35:
        return "", best_score, [{"url": u, "score": s} for s, u, _ in scored[:5]]

    rejected = [{"url": u, "score": s} for s, u, _ in scored[1:5]]
    return best_url, best_score, rejected


def run_resolve(workers: int = 4, limit: int | None = None) -> dict:
    """Resolve official websites for every company that does not have one."""
    directories = load_directory_domains()
    plan = load_plan()
    rows = select_pending(PENDING_SQL, limit=limit)

    _no_match: dict[str, bool] = {}

    def handler(row):
        url, confidence, rejected = resolve_one(row["company_name"], directories, plan)
        if not url:
            _no_match[row["company_key"]] = True
            update(
                row["company_key"],
                conn=get_connection(),
                website_candidates=json.dumps(rejected) if rejected else None,
                resolve_error="No confident website match found",
                resolve_attempts=(row["resolve_attempts"] or 0) + 1,
            )
            return "no match"
        update(
            row["company_key"],
            conn=get_connection(),
            website=url,
            website_confidence=confidence,
            website_candidates=json.dumps(rejected) if rejected else None,
            resolved_at=utc_now(),
            resolve_error=None,
        )
        return url

    result = run_batch("resolve", rows, handler, workers=workers,
                       label="Finding websites")

    # "No confident match" is not a success, and counting it as one produced
    # "300 succeeded, 0 failed" on a run where two hundred companies never got
    # a website at all.
    result["no_match"] = sum(1 for r in rows if _no_match.get(r["company_key"]))
    result["ok"] = max(result.get("ok", 0) - result["no_match"], 0)

    merged = merge_duplicate_websites()
    if merged:
        result["merged_duplicates"] = merged
    return result

"""Page fetching and text extraction.

Deliberately httpx + BeautifulSoup rather than a headless browser. The pages
that carry the evidence -- About, Global Presence, Export, Dealer Network,
Leadership -- are almost always server-rendered on the kind of manufacturer
site this pipeline targets, and a browser per company would make a 600-company
run take hours instead of minutes.

`PAGE_HINTS` is the part worth tuning: it decides which internal links are
worth a request. Fetching a whole site would be slow and would dilute the LLM
context with product-spec boilerplate; fetching only the homepage misses the
"Global Presence" page where the Australian subsidiary is actually named.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from prospector import config
from prospector.config import DEFAULTS, env_int

log = logging.getLogger(__name__)

# Link text / href fragments that signal a page worth reading, in priority
# order. International-presence pages come first because they are where the
# whole brief is won or lost.
PAGE_HINTS: tuple[tuple[str, ...], ...] = (
    ("global", "international", "worldwide", "presence", "export", "overseas", "markets"),
    ("dealer", "distributor", "network", "partners", "representative", "where-to-buy"),
    ("about", "company", "profile", "who-we-are", "our-story", "overview"),
    ("leadership", "management", "team", "board", "directors", "people"),
    ("client", "customer", "reference", "case-stud", "project", "installation"),
    ("contact", "reach-us", "locations", "offices"),
    ("product", "solution", "range", "equipment"),
    ("news", "press", "media", "blog"),
)

# Indexes into PAGE_HINTS that always get a slot -- see discover_links.
RESERVED_DEALER_GROUP = 1
RESERVED_CONTACT_GROUP = 5

_SKIP_EXT = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".rar",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp4", ".mp3", ".ico",
)
_SKIP_HREF = ("mailto:", "tel:", "javascript:", "#", "whatsapp:")


@dataclass
class Page:
    url: str
    title: str
    text: str


def _headers() -> dict[str, str]:
    return {
        "User-Agent": os.environ.get("USER_AGENT", DEFAULTS["USER_AGENT"]),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    d = Path(config.CACHE_DIR) / "pages"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{digest}.html"


def fetch_html(url: str, use_cache: bool = True) -> str:
    """GET a URL and return HTML, or "" on any failure. Never raises."""
    cache_file = _cache_path(url)
    if use_cache and cache_file.exists():
        try:
            return cache_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            pass

    try:
        with httpx.Client(
            timeout=env_int("REQUEST_TIMEOUT", 30),
            follow_redirects=True,
            headers=_headers(),
            verify=False,  # many small manufacturer sites have expired certs
        ) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            log.debug("fetch %s -> HTTP %s", url, resp.status_code)
            return ""
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and ctype:
            return ""
        html = resp.text
    except Exception as exc:  # noqa: BLE001 - one dead site must not kill a batch
        log.debug("fetch %s failed: %s", url, exc)
        return ""

    try:
        cache_file.write_text(html, encoding="utf-8")
    except OSError:
        pass
    return html


def html_to_text(html: str, max_chars: int = 12000) -> tuple[str, str]:
    """Strip an HTML page to (title, readable text), collapsing whitespace."""
    if not html:
        return "", ""

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    # Prefer the main content region when the site marks one up; falls back to
    # the whole body, which is what most older manufacturer sites need.
    root = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    text = root.get_text("\n", strip=True)

    # The footer is appended even when <main> was used, because on a
    # manufacturer's site the footer is where "Overseas offices: Sydney, Dubai,
    # Nairobi" lives -- exactly the sentence the qualification stage exists to
    # find, and taking <main> alone discarded it on every site modern enough to
    # use the tag.
    if root is not soup.body:
        footer = soup.find("footer")
        if footer:
            footer_text = footer.get_text("\n", strip=True)
            if footer_text and footer_text not in text:
                text = f"{text}\n{footer_text}"

    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    if len(text) <= max_chars:
        return title, text

    # Head *and* tail, not head alone. Dealer tables, office lists and export
    # markets sit at the bottom of a long page, so cutting from the top only was
    # another way the decisive sentence got thrown away.
    head = int(max_chars * 0.65)
    tail = max_chars - head
    return title, text[:head] + "\n[...]\n" + text[-tail:]


def same_site(base: str, candidate: str) -> bool:
    """True when two URLs share a registrable-ish domain (www/sub tolerated)."""
    try:
        b = urlparse(base).netloc.lower().removeprefix("www.")
        c = urlparse(candidate).netloc.lower().removeprefix("www.")
    except ValueError:
        return False
    if not b or not c:
        return False
    return c == b or c.endswith("." + b) or b.endswith("." + c)


def discover_links(base_url: str, html: str, limit: int = 8) -> list[str]:
    """Pick the internal links most likely to carry presence/company evidence.

    Scored by PAGE_HINTS priority so that, when a site has more candidate
    pages than `limit`, "Global Presence" beats "Products" and "Products"
    beats "Blog".
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    scored: dict[str, int] = {}
    group_of: dict[str, int] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.lower().startswith(_SKIP_HREF):
            continue

        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
            continue
        if any(parsed.path.lower().endswith(ext) for ext in _SKIP_EXT):
            continue
        if not same_site(base_url, full):
            continue

        full = full.split("#")[0].rstrip("/")
        if not full or full.rstrip("/") == base_url.rstrip("/"):
            continue

        haystack = (parsed.path + " " + a.get_text(" ", strip=True)).lower()
        for rank, group in enumerate(PAGE_HINTS):
            if any(hint in haystack for hint in group):
                score = len(PAGE_HINTS) - rank
                if score > scored.get(full, 0):
                    scored[full] = score
                    group_of[full] = rank
                break

    ordered = sorted(scored.items(), key=lambda kv: (-kv[1], len(kv[0])))
    picked = [url for url, _ in ordered[:limit]]

    # Two groups get a reserved slot rather than competing on rank alone.
    #
    # A link-rich site produces dozens of candidates, and "contact / locations /
    # offices" sits at rank 5 of 8 -- so on exactly the sites big enough to have
    # overseas offices, the page listing them fell outside the budget and was
    # never fetched. Same for the dealer and distributor network. These are the
    # two pages the whole qualification stage is looking for.
    # Both reservations are resolved together, then the list is rebuilt. Doing
    # them one at a time and popping to make room made the second reservation
    # discard the page the first had just added -- at the real limit of six, the
    # dealer page evicted the contact page and the fix cancelled itself out.
    wanted = []
    for reserved in (RESERVED_CONTACT_GROUP, RESERVED_DEALER_GROUP):
        if any(group_of.get(u) == reserved for u in picked):
            continue
        candidate = next((u for u, _ in ordered if group_of.get(u) == reserved), "")
        if candidate:
            wanted.append(candidate)

    if wanted:
        # Reserved pages first, then the ranked list, up to the budget.
        keep = [u for u in picked if u not in wanted][: max(limit - len(wanted), 0)]
        picked = wanted + keep

    return picked[:limit]


def crawl_site(base_url: str, max_pages: int = 7, max_chars_per_page: int = 12000) -> list[Page]:
    """Fetch a homepage plus its highest-value internal pages."""
    if not base_url:
        return []
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url

    pages: list[Page] = []
    home_html = fetch_html(base_url)
    if not home_html:
        # A surprising number of small sites answer on http but not https.
        if base_url.startswith("https://"):
            base_url = "http://" + base_url[len("https://"):]
            home_html = fetch_html(base_url)
        if not home_html:
            return []

    title, text = html_to_text(home_html, max_chars_per_page)
    pages.append(Page(url=base_url, title=title, text=text))

    links = discover_links(base_url, home_html, limit=max_pages - 1)
    seen_links = set(links)

    for link in links:
        html = fetch_html(link)
        if not html:
            continue
        t, txt = html_to_text(html, max_chars_per_page)
        # 40, not 120. "Our Australian distributor is Acme Pty Ltd, Perth WA."
        # is 52 characters and is the entire answer.
        if len(txt) < 40:
            continue  # navigation-only page, nothing to read
        pages.append(Page(url=link, title=t, text=txt))

        # A second pass, over the About page's own HTML. Sites that build their
        # menu in JavaScript expose no <a href> on the homepage at all, so link
        # discovery found nothing, the company was judged entirely on homepage
        # marketing copy -- and crawl still counted it as a success.
        # `link`, not `base_url`, is the base here: this is the About page's own
        # HTML, so a relative href like "leadership" belongs under /about/, not
        # under the site root. Resolving it against the root produced URLs that
        # 404ed and were skipped in silence, so the rescue path found nothing on
        # exactly the sites it was written for.
        if len(pages) < max_pages and _looks_like_about(link):
            for extra in discover_links(link, html, limit=3):
                if extra not in seen_links and same_site(base_url, extra):
                    seen_links.add(extra)
                    links.append(extra)

        if len(pages) >= max_pages:
            break

    return pages


def _looks_like_about(url: str) -> bool:
    low = url.lower()
    return any(k in low for k in ("about", "company", "profile", "who-we-are",
                                  "overview", "our-story"))

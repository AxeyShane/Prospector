"""Free web search for Prospector -- no paid API, no API key.

Uses DuckDuckGo's HTML endpoints, which return plain server-rendered results
with titles, URLs *and snippets*. The snippets matter: for a question like
"does this company have an Australian distributor", the answer is often in
the snippet itself, so a single search can settle it without fetching a page.

Everything is cached to disk. A show gets re-run many times while the brief is
tuned, and a cached search costs nothing and cannot be rate limited.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from prospector import config
from prospector.config import DEFAULTS, env_int

log = logging.getLogger(__name__)

# Search endpoints in order.
#
# A third, independent engine was added here and then removed: the obvious
# candidate publishes a robots.txt that disallows automated access to its search
# results, and adding an endpoint we had been asked not to use -- with a parser
# that could not be verified against the live site without doing so -- is not a
# resilience improvement. If you want a genuine fallback, set BRAVE_API_KEY:
# Brave's Search API has a free tier and permits programmatic use, which the
# scraped HTML endpoints below do not.
#
# So the resilience here comes from detection instead. DuckDuckGo blocks
# aggressively, and when it does it answers 200 with an empty result page --
# indistinguishable from "nothing matched" -- so a run used to grind through a
# thousand blocked searches and report a clean finish having found nothing.
# `search_health` now tells the pipeline the difference, and the run stops and
# says so.
_ENDPOINTS = (
    ("duckduckgo", "https://html.duckduckgo.com/html/", "post"),
    ("duckduckgo-lite", "https://lite.duckduckgo.com/lite/", "post"),
)

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

# How long a cached search stays good. There was no expiry at all, so "run it
# again to find more" replayed byte-identical results forever.
CACHE_DAYS = 14

# Consecutive all-endpoint failures before the run is told search is down.
# One failure is a bad query; ten in a row is a block or a dead connection.
BLOCK_THRESHOLD = 10

_health_lock = threading.Lock()
_consecutive_failures = 0
_searches_run = 0

# One global lock + timestamp: politeness has to be enforced across worker
# threads, otherwise 8 workers issue 8 simultaneous searches and get blocked.
_throttle_lock = threading.Lock()
_last_request_at = 0.0


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str

    def as_text(self) -> str:
        return f"{self.title}\n{self.url}\n{self.snippet}"


def _cache_path(query: str, max_results: int) -> Path:
    digest = hashlib.sha256(f"{query}|{max_results}".encode("utf-8")).hexdigest()[:32]
    d = Path(config.CACHE_DIR) / "search"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{digest}.json"


def _throttle() -> None:
    """Space out requests globally, with jitter so the pattern is not robotic."""
    global _last_request_at
    delay_ms = env_int("SEARCH_DELAY_MS", 1500)
    with _throttle_lock:
        wait = (_last_request_at + delay_ms / 1000.0) - time.monotonic()
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.4))
        _last_request_at = time.monotonic()


def _clean_ddg_url(href: str) -> str:
    """Unwrap DuckDuckGo's /l/?uddg=<encoded> redirect into the real URL."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    return href


def _parse(html: str, max_results: int) -> list[SearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[SearchResult] = []
    seen: set[str] = set()

    # html.duckduckgo.com layout
    for block in soup.select("div.result, div.web-result"):
        link = block.select_one("a.result__a")
        if not link:
            continue
        url = _clean_ddg_url(link.get("href", ""))
        if not url or url in seen:
            continue
        snippet_el = block.select_one(".result__snippet")
        out.append(SearchResult(
            title=link.get_text(" ", strip=True),
            url=url,
            snippet=snippet_el.get_text(" ", strip=True) if snippet_el else "",
        ))
        seen.add(url)
        if len(out) >= max_results:
            return out

    # lite.duckduckgo.com layout -- a table, link row followed by snippet row
    if not out:
        rows = soup.select("a.result-link")
        snippets = soup.select("td.result-snippet")
        for i, link in enumerate(rows):
            url = _clean_ddg_url(link.get("href", ""))
            if not url or url in seen:
                continue
            out.append(SearchResult(
                title=link.get_text(" ", strip=True),
                url=url,
                snippet=snippets[i].get_text(" ", strip=True) if i < len(snippets) else "",
            ))
            seen.add(url)
            if len(out) >= max_results:
                break

    return out


def _brave_search(query: str, max_results: int) -> list[SearchResult]:
    """Brave's Search API, used only when the user has supplied a key.

    Opt-in, and keyless by default, because the whole app is built to cost
    nothing. But a key makes the difference between a run that stops halfway
    with "search has stopped responding" and one that finishes, so it is worth
    offering to anyone who hits that often enough to care.
    """
    key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not key:
        return []
    try:
        resp = httpx.get(
            BRAVE_ENDPOINT,
            params={"q": query, "count": min(max_results, 20)},
            headers={"Accept": "application/json", "X-Subscription-Token": key},
            timeout=env_int("REQUEST_TIMEOUT", 30),
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.debug("brave search failed: %s", exc)
        return []

    out: list[SearchResult] = []
    for item in ((payload.get("web") or {}).get("results") or [])[:max_results]:
        url = str(item.get("url") or "")
        if url.startswith("http"):
            out.append(SearchResult(
                title=str(item.get("title") or ""),
                url=url,
                snippet=str(item.get("description") or ""),
            ))
    return out


def _record_health(ok: bool) -> None:
    global _consecutive_failures, _searches_run
    with _health_lock:
        _searches_run += 1
        _consecutive_failures = 0 if ok else _consecutive_failures + 1


def search_health() -> dict:
    """Is web search actually working right now?

    The pipeline asks this rather than inferring it from empty results, because
    a block and a genuinely obscure query look identical one search at a time.
    Ten in a row is the difference.
    """
    with _health_lock:
        return {
            "searches_run": _searches_run,
            "consecutive_failures": _consecutive_failures,
            "blocked": _consecutive_failures >= BLOCK_THRESHOLD,
        }


def reset_health() -> None:
    global _consecutive_failures, _searches_run
    with _health_lock:
        _consecutive_failures = 0
        _searches_run = 0


def search(query: str, max_results: int = 8, use_cache: bool = True) -> list[SearchResult]:
    """Run a web search and return results. Never raises -- returns [] on failure.

    A search failing is normal and survivable (the company just gets less
    evidence); a search *exception* would kill the worker thread and take the
    rest of that batch with it.
    """
    cache_file = _cache_path(query, max_results)
    if use_cache and cache_file.exists():
        age_days = (time.time() - cache_file.stat().st_mtime) / 86400
        if age_days <= env_int("SEARCH_CACHE_DAYS", CACHE_DAYS):
            try:
                return [SearchResult(**r)
                        for r in json.loads(cache_file.read_text("utf-8"))]
            except (json.JSONDecodeError, TypeError, ValueError):
                pass  # corrupt cache entry -- just re-search

    headers = {
        "User-Agent": os.environ.get("USER_AGENT", DEFAULTS["USER_AGENT"]),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = env_int("REQUEST_TIMEOUT", 30)
    results: list[SearchResult] = []

    # Tried first when a key is set: it is a permitted API rather than scraped
    # HTML, so it neither blocks nor needs a throttle.
    results = _brave_search(query, max_results)

    for name, url, method in _ENDPOINTS:
        if results:
            break
        try:
            _throttle()
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
                if method == "post":
                    resp = client.post(url, data={"q": query, "kl": "wt-wt"})
                else:
                    resp = client.get(url, params={"q": query})
            if resp.status_code != 200:
                log.debug("search endpoint %s returned %s", name, resp.status_code)
                continue
            results = _parse(resp.text, max_results)
            if results:
                break
        except Exception as exc:  # noqa: BLE001 - a dead endpoint must not kill the run
            log.debug("search endpoint %s failed: %s", name, exc)
            continue

    _record_health(bool(results))

    if results:
        try:
            cache_file.write_text(
                json.dumps([asdict(r) for r in results], indent=1), encoding="utf-8"
            )
        except OSError:
            pass
    else:
        log.warning("No search results for %r (all endpoints failed or blocked)", query)

    return results


def search_many(queries: list[str], max_results: int = 6) -> list[SearchResult]:
    """Run several queries and merge, de-duplicating by URL, preserving order."""
    merged: list[SearchResult] = []
    seen: set[str] = set()
    for q in queries:
        for r in search(q, max_results=max_results):
            key = r.url.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)
    return merged


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenise(text: str) -> set[str]:
    """Lowercase alphanumeric tokens, used for name/domain matching."""
    return set(_TOKEN_RE.findall(text.lower()))

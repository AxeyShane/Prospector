"""Stage 3 -- crawl: read each company's own pages.

Everything downstream reasons over this text, so the crawl deliberately
targets the pages where the answers live (Global Presence, Dealer Network,
About, Leadership) rather than fetching whole sites.
"""

from __future__ import annotations

import json
import logging

from prospector.database import MAX_ATTEMPTS, get_connection, update, utc_now
from prospector.fetcher import crawl_site
from prospector.stages._runner import run_batch, select_pending

log = logging.getLogger(__name__)

PENDING_SQL = (
    "SELECT * FROM leads WHERE website IS NOT NULL AND crawled_at IS NULL "
    f"AND COALESCE(crawl_attempts, 0) < {MAX_ATTEMPTS} "
    "ORDER BY company_key"
)


def run_crawl(workers: int = 4, limit: int | None = None, max_pages: int = 7) -> dict:
    rows = select_pending(PENDING_SQL, limit=limit)

    def handler(row):
        pages = crawl_site(row["website"], max_pages=max_pages)
        if not pages:
            raise RuntimeError(f"Site unreachable or empty: {row['website']}")

        blob = {p.url: p.text for p in pages if p.text}
        chars = sum(len(t) for t in blob.values())
        update(
            row["company_key"],
            conn=get_connection(),
            pages_json=json.dumps(blob),
            crawl_chars=chars,
            crawled_at=utc_now(),
            crawl_error=None,
        )
        return f"{len(pages)} pages, {chars:,} chars"

    return run_batch("crawl", rows, handler, workers=workers, label="Reading websites")

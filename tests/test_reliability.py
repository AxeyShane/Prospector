"""Failures that used to be silent, expensive, or both.

Every test here stands for a way a real run went wrong with nothing on screen
to say so.
"""

import pytest

from prospector import llm, websearch


# ---------------------------------------------------------------------------
# Search: a block must be distinguishable from "nothing matched"
# ---------------------------------------------------------------------------

def test_search_health_starts_clean():
    websearch.reset_health()
    assert websearch.search_health()["blocked"] is False


def test_repeated_empty_searches_are_reported_as_a_block():
    """A block answers 200 with an empty page, exactly like a rare query.

    One failure is a bad query. Ten in a row is the free endpoint refusing us,
    and the run used to grind through a thousand of them and finish clean.
    """
    websearch.reset_health()
    for _ in range(websearch.BLOCK_THRESHOLD):
        websearch._record_health(False)
    assert websearch.search_health()["blocked"] is True


def test_one_success_clears_the_block():
    websearch.reset_health()
    for _ in range(websearch.BLOCK_THRESHOLD):
        websearch._record_health(False)
    websearch._record_health(True)
    assert websearch.search_health()["blocked"] is False


def test_no_endpoint_is_added_that_asked_not_to_be_scraped():
    """An independent engine was added here and then removed.

    Its robots.txt disallows automated access to search results, and a parser
    that cannot be verified against the live site without ignoring that is not
    a resilience improvement. The fallback is an opt-in permitted API instead.
    """
    hosts = [url for _, url, _ in websearch._ENDPOINTS]
    assert all("duckduckgo" in h for h in hosts)
    assert "mojeek" not in str(hosts).lower()


def test_the_optional_api_fallback_is_off_without_a_key(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert websearch._brave_search("anything", 5) == []


def test_the_optional_api_fallback_parses_a_real_response(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"web": {"results": [
                {"title": "Puzzolana Machinery", "url": "https://puzzolana.com",
                 "description": "Crushing and screening equipment."},
                {"title": "Bad row", "url": "not-a-url", "description": ""},
            ]}}

    monkeypatch.setattr(websearch.httpx, "get", lambda *a, **k: Resp())
    got = websearch._brave_search("puzzolana", 5)
    assert len(got) == 1
    assert got[0].url == "https://puzzolana.com"


def test_the_search_cache_expires():
    """There was no expiry, so "run it again" replayed identical results."""
    assert websearch.CACHE_DAYS > 0


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

def test_spend_accumulates_across_calls():
    llm.reset_spend()
    llm.record_usage("google/gemini-2.5-flash",
                     {"prompt_tokens": 1_000_000, "completion_tokens": 0})
    first = llm.spend_so_far()["usd"]
    assert first > 0
    llm.record_usage("google/gemini-2.5-flash",
                     {"prompt_tokens": 1_000_000, "completion_tokens": 0})
    assert llm.spend_so_far()["usd"] == pytest.approx(first * 2)


def test_malformed_usage_is_ignored_rather_than_crashing_a_run():
    llm.reset_spend()
    llm.record_usage("x", {"prompt_tokens": None, "completion_tokens": "lots"})
    assert llm.spend_so_far()["calls"] == 0


def test_the_budget_pauses_the_run_once_the_limit_is_reached():
    llm.reset_spend()
    llm.record_usage("google/gemini-2.5-flash",
                     {"prompt_tokens": 20_000_000, "completion_tokens": 0})
    with pytest.raises(llm.BudgetExceeded, match="saved"):
        llm.check_budget(1.0)


def test_no_limit_means_no_ceiling():
    llm.reset_spend()
    llm.record_usage("google/gemini-2.5-flash",
                     {"prompt_tokens": 99_000_000, "completion_tokens": 0})
    llm.check_budget(0)   # must not raise


def test_retry_after_is_clamped():
    """A provider answering "3600" parked the app for an hour, four times over.

    The value went straight to sleep() with no ceiling, inside a call the user
    can neither see nor cancel.
    """
    assert llm._MAX_RETRY_AFTER <= 300


def test_retries_do_not_multiply_into_a_large_bill():
    """Retries compound with the per-stage attempt counter."""
    from prospector.database import MAX_ATTEMPTS
    assert llm._MAX_RETRIES * MAX_ATTEMPTS <= 9

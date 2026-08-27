"""Website resolution is load-bearing: a directory listing accepted as a
company's own site poisons every stage after it."""

from prospector.config import load_directory_domains
from prospector.stages.resolve import _name_tokens, score_candidate

DIRS = load_directory_domains()


def test_directory_listings_are_rejected_outright():
    assert score_candidate("Puzzolana Machinery Fabricators",
                           "https://www.indiamart.com/puzzolana/", "Puzzolana", DIRS) == 0.0
    assert score_candidate("Dozco India Pvt Ltd",
                           "https://in.linkedin.com/company/dozco", "Dozco", DIRS) == 0.0
    assert score_candidate("Acme Ltd", "https://en.wikipedia.org/wiki/Acme",
                           "Acme", DIRS) == 0.0


def test_the_brand_token_is_the_first_one_not_the_longest():
    # Sorting by length made "PUZZOLANA MACHINERY FABRICATORS" identify on
    # "fabricators" and scored its own site below the confidence floor.
    assert _name_tokens("Puzzolana Machinery Fabricators")[0] == "puzzolana"
    assert _name_tokens("M/S ASKA EQUIPMENTS PRIVATE LIMITED")[0] == "aska"


def test_real_companies_resolve_to_their_real_domains():
    cases = [
        ("Puzzolana Machinery Fabricators (Hyderabad) LLP", "https://puzzolana.com/"),
        ("DOZCO INDIA PVT LTD", "https://dozco.com/"),
        ("BHARAT WIRE ROPES LIMITED", "https://www.bharatwireropes.com/"),
        ("SAP Parts Pvt. Ltd.", "https://www.sapparts.com/"),
        ("Usha Martin Limited", "https://www.ushamartin.com/"),
    ]
    for name, url in cases:
        assert score_candidate(name, url, name, DIRS) >= 0.7, f"{name} -> {url}"


def test_an_unrelated_site_scores_below_the_floor():
    assert score_candidate("Elite Industries", "https://random-blog.com/x",
                           "Some blog", DIRS) < 0.35
    assert score_candidate("Acme Crushers", "https://competitor-news.com/article",
                           "Industry news", DIRS) < 0.35


def test_generic_words_carry_no_signal():
    tokens = _name_tokens("Industries Engineering Solutions Private Limited India")
    assert tokens == []


# ---------------------------------------------------------------------------
# Regressions.
# ---------------------------------------------------------------------------

def test_a_short_brand_does_not_match_a_domain_that_merely_contains_its_letters():
    """"Ace Engineering" used to score 0.65 against spaceage-india.com.

    That is nearly double the accept floor, so every three- and four-letter
    brand -- Ace, MRF, TVS, Elgi, KEC -- resolved to whatever unrelated domain
    happened to contain those letters, and every stage after resolve then read
    a complete stranger's website.
    """
    assert score_candidate("Ace Engineering", "https://spaceage-india.com",
                           "Space Age India", DIRS) < 0.35
    assert score_candidate("Ace Engineering", "https://aceternity.com",
                           "Aceternity UI", DIRS) < 0.35
    # The real one still resolves.
    assert score_candidate("Ace Engineering", "https://aceengineering.in",
                           "Ace Engineering", DIRS) >= 0.7


def test_the_plans_country_breaks_a_same_name_tie():
    """Two real companies share a name; only the brief says which one is wanted.

    Scoring had no country input at all, and a hardcoded ".in" bonus, so an
    Australia-only brief resolved Sterling Engineering to the Indian namesake.
    """
    au = "https://sterlingengineering.com.au"
    uk = "https://sterlingengineering.co.uk"
    name, title = "Sterling Engineering", "Sterling Engineering"

    on_au_brief = score_candidate(name, au, title, DIRS, ["Australia"])
    assert on_au_brief > score_candidate(name, uk, title, DIRS, ["Australia"])
    assert score_candidate(name, uk, title, DIRS, ["United Kingdom"]) > \
        score_candidate(name, au, title, DIRS, ["United Kingdom"])


def test_an_abbreviated_domain_loses_to_the_full_name():
    full = score_candidate("Sterling Engineering", "https://sterlingengineering.co.uk",
                           "Sterling Engineering", DIRS)
    abbrev = score_candidate("Sterling Engineering", "https://sterling-eng.in",
                             "Sterling Engineering India", DIRS)
    assert full > abbrev

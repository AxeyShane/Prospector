"""Discovery turns a prompt into named companies. Its filters are what keep
magazine headlines and directory names out of the lead list."""

from prospector.stages.discover import _clean_name, _looks_like_list_page, _plausible


def test_decoration_is_stripped_from_names():
    assert _clean_name("Acme Crushers | Jaw Crushers India") == "Acme Crushers"
    assert _clean_name("Puzzolana Machinery Fabricators - Hyderabad") == \
        "Puzzolana Machinery Fabricators"
    assert _clean_name("  Propel Industries Private Limited  ") == \
        "Propel Industries Private Limited"


def test_article_titles_are_not_companies():
    for title in ("10 Best Crusher Manufacturers", "Top 20 Suppliers",
                  "How to choose a crusher", "Crusher Review Guide",
                  "Best crushers 2026 news"):
        assert _plausible(_clean_name(title), set()) is False


def test_directories_are_not_companies():
    for name in ("IndiaMART", "LinkedIn", "Wikipedia", "TradeIndia listings"):
        assert _plausible(name, set()) is False


def test_real_company_names_survive():
    for name in ("Propel Industries Private Limited", "Bharat Wire Ropes Limited",
                 "Usha Martin", "SAP Parts Pvt. Ltd."):
        assert _plausible(name, set()) is True


def test_absurd_lengths_are_rejected():
    assert _plausible("A", set()) is False
    assert _plausible("x" * 200, set()) is False
    assert _plausible("12345", set()) is False


def test_list_pages_are_recognised_for_mining():
    assert _looks_like_list_page("Top 10 crusher manufacturers in India")
    assert _looks_like_list_page("Members Directory")
    assert _looks_like_list_page("2026 exhibitor list")
    assert not _looks_like_list_page("Acme Ltd - Official Site")
    assert not _looks_like_list_page("Contact us")


def test_a_directory_name_inside_a_company_name_is_not_a_directory():
    """A plain substring test over ninety domains is a minefield.

    Adding `trade.gov` to a plan discarded "Trade Winds Engineering", and
    `medium`, `manta` and `indeed` are on the shipped list already.
    """
    from prospector.stages.discover import _plausible

    dirs = {"trade.gov", "medium.com", "manta.com", "indiamart.com", "kompass.com"}
    assert _plausible("Trade Winds Engineering", dirs)
    assert _plausible("Medium Duty Cranes", dirs)
    assert _plausible("Manta Equipment Co", dirs)
    # The directories themselves are still filtered.
    assert not _plausible("IndiaMART", dirs)
    assert not _plausible("Kompass India", dirs)


def test_a_short_exclusion_does_not_drop_unrelated_companies():
    """`ex in low` with a short entry is indiscriminate.

    Uploaded lists run through the same check, so the user's own companies
    vanished with the misleading explanation that they were excluded.
    """
    from prospector.stages.discover import _excluded

    plan = {"excluded_companies": ["TIL", "Ace", "Acme Crushers"]}
    assert not _excluded("Utility Engineering Works", plan)   # contains "til"
    assert not _excluded("Pace Industries", plan)             # contains "ace"
    # The real ones still go.
    assert _excluded("TIL Limited", plan)
    assert _excluded("Acme Crushers International", plan)

"""Qualification is the stage the whole tool exists for, so its guards against
inventing evidence are tested directly."""

import pytest

from prospector.stages.qualify import (
    _keywords, _relevant_passages, build_queries, normalise_level, score_for,
)

LEVELS = [{"label": "Strong match"}, {"label": "Partial match"},
          {"label": "Unclear"}, {"label": "Does not match"}]


def test_exact_and_paraphrased_labels_both_map_home():
    assert normalise_level("Strong match", LEVELS) == "Strong match"
    assert normalise_level("strong", LEVELS) == "Strong match"
    assert normalise_level("PARTIAL", LEVELS) == "Partial match"
    assert normalise_level("some evidence", LEVELS) == "Partial match"
    assert normalise_level("does not match", LEVELS) == "Does not match"


def test_an_empty_answer_never_becomes_the_best_rating():
    # The substring test underneath matches "" against every label. Without an
    # explicit guard that silently promotes every failed call to a top lead --
    # the single most damaging bug this module could have.
    for value in ("", "  ", "a", None):
        assert normalise_level(value, LEVELS) == "Unclear"


def test_an_unrecognised_answer_defaults_to_unclear():
    assert normalise_level("banana", LEVELS) == "Unclear"


def test_no_levels_configured_is_survivable():
    assert normalise_level("strong", []) == "Unclear"


def test_score_rewards_the_level_first_and_weighted_criteria_second(sample_plan):
    both = score_for("Strong match", ["Sells into Australia", "Mining capable"], sample_plan)
    one = score_for("Strong match", ["Sells into Australia"], sample_plan)
    none = score_for("Strong match", [], sample_plan)
    unclear = score_for("Unclear", [], sample_plan)

    assert both == 100
    assert both > one > none
    assert none > unclear


def test_passage_filter_keeps_evidence_and_drops_distant_product_specs(sample_plan):
    text = "\n".join([
        "Shipping weight 4kg per unit",
        "Bolt diameter 12mm tensile strength 800MPa",
        "We operate a subsidiary in Australia trading as Acme Pty Ltd",
        "tiny",
        "Hydraulic pressure rated to 250 bar",
        "Paint finish RAL 5010 two-pack epoxy",
    ])
    kept = _relevant_passages(text, _keywords(sample_plan), 2000)
    assert "Acme Pty Ltd" in kept
    # Lines that neither match nor sit beside a match are dropped.
    assert "Shipping weight" not in kept
    assert "Paint finish" not in kept


def test_passage_filter_keeps_the_line_beside_a_match(sample_plan):
    """A distributor table splits one fact across two rows.

    Matching only the hit line hands the model a country with no company
    against it, or a company with no country -- half a fact reads as no fact,
    and the company is rated Unclear when the evidence was right there.
    """
    text = "\n".join([
        "Our international dealer network:",
        "Australia and New Zealand",
        "Acme Handling Pty Ltd, Dandenong South VIC",
        "A line of context that comes along with the match above",
        "Unrelated filler line that should stay out of the result entirely",
    ])
    kept = _relevant_passages(text, _keywords(sample_plan), 2000)
    assert "Australia and New Zealand" in kept
    assert "Acme Handling Pty Ltd" in kept
    assert "Unrelated filler" not in kept


def test_keywords_include_the_plans_own_regions_and_criteria(sample_plan):
    words = _keywords(sample_plan)
    assert "australia" in words
    assert any("mining" in w for w in words)


def test_queries_cover_regions_then_criteria_then_generic(sample_plan):
    queries = build_queries("Acme Ltd", sample_plan)
    assert '"Acme Ltd" Australia' in queries
    assert any("distributor" in q for q in queries)
    assert len(queries) == len(set(queries))


# ---------------------------------------------------------------------------
# Regressions. Each of these is a bug that shipped, not a hypothetical.
# ---------------------------------------------------------------------------

FOUR = [{"label": "Strong match"}, {"label": "Partial match"},
        {"label": "Unclear"}, {"label": "Not a match"}]


@pytest.mark.parametrize("answer", ["Match", "match", "MATCH", "Match.", "a match"])
def test_a_bare_match_is_never_read_as_the_top_rating(answer):
    """The single most likely one-word reply used to become "Strong match".

    "match" is a substring of "strong match", and the old code accepted a
    match in either direction, so a model answering "Match" with weak evidence
    was promoted straight to the top of the call list.
    """
    assert normalise_level(answer, FOUR) != "Strong match"


@pytest.mark.parametrize("answer,expected", [
    ("Strong match", "Strong match"),
    ("strong", "Strong match"),
    ("Yes - clear evidence", "Strong match"),
    ("Partial match", "Partial match"),
    ("partial, some evidence", "Partial match"),
    ("Not a match", "Not a match"),
    ("no match", "Not a match"),
    ("does not meet the criteria", "Not a match"),
    ("Unclear", "Unclear"),
    ("cannot tell from the sources", "Unclear"),
    ("", "Unclear"),
    ("wibble", "Unclear"),
])
def test_level_paraphrases_map_the_way_a_reader_would_expect(answer, expected):
    assert normalise_level(answer, FOUR) == expected


def test_negatives_are_tested_before_positives():
    """Every negative phrase contains a positive word.

    "not a match" contains "match"; "does not meet" contains "meet". Testing
    the positive table first inverts the rating.
    """
    for answer in ("not a match", "no match", "does not qualify"):
        assert normalise_level(answer, FOUR) == "Not a match"

"""The planner has to survive whatever a small local model returns."""

import pytest

from prospector.planner import DEFAULT_LEVELS, normalise_plan


def test_a_good_response_passes_through():
    plan = normalise_plan({
        "title": "UK cleaning contractors",
        "target_leads": 40,
        "discovery_queries": ["commercial cleaning contractors uk", "facilities cleaning suppliers"],
        "qualification_criteria": [{"name": "50+ staff", "description": "headcount", "weight": 4}],
        "qualification_levels": DEFAULT_LEVELS,
    }, "uk cleaning firms")

    assert plan["title"] == "UK cleaning contractors"
    assert plan["target_leads"] == 40
    assert len(plan["discovery_queries"]) == 2
    assert plan["qualification_criteria"][0]["weight"] == 4


def test_a_string_where_a_list_belongs_is_split():
    plan = normalise_plan(
        {"discovery_queries": "top crusher makers india, crusher suppliers directory australia"},
        "x" * 20)
    assert plan["discovery_queries"] == ["top crusher makers india",
                                         "crusher suppliers directory australia"]


def test_wrong_number_of_levels_is_replaced_wholesale():
    # Everything downstream indexes levels by position, so three or five is not
    # something to patch -- it has to be replaced.
    plan = normalise_plan({"qualification_levels": [{"label": "Yes"}, {"label": "No"}]}, "x" * 20)
    assert len(plan["qualification_levels"]) == 4
    assert plan["qualification_levels"][0]["label"] == "Strong match"


def test_out_of_range_numbers_are_clamped():
    assert normalise_plan({"target_leads": "5000"}, "x" * 20)["target_leads"] == 1000
    assert normalise_plan({"target_leads": -3}, "x" * 20)["target_leads"] == 5
    assert normalise_plan({"target_leads": "banana"}, "x" * 20)["target_leads"] == 60
    assert normalise_plan({"people_per_company": 99}, "x" * 20)["people_per_company"] == 8


def test_an_empty_response_still_yields_a_runnable_plan():
    plan = normalise_plan({}, "find me UK facilities managers")
    assert plan["title"]
    assert plan["priority_roles"]
    assert plan["qualification_criteria"][0]["name"] == "Matches the brief"
    assert len(plan["qualification_levels"]) == 4


def test_criteria_given_as_bare_strings_are_accepted():
    plan = normalise_plan({"qualification_criteria": ["Exports to Australia", "Has ISO 9001"]},
                          "x" * 20)
    assert [c["name"] for c in plan["qualification_criteria"]] == \
        ["Exports to Australia", "Has ISO 9001"]
    assert all(c["weight"] == 3 for c in plan["qualification_criteria"])


def test_short_prompts_are_rejected_with_a_helpful_message():
    from prospector.planner import make_plan

    with pytest.raises(ValueError, match="Tell me a bit more"):
        make_plan("leads")


def test_fallback_queries_are_built_from_the_prompt():
    from prospector.planner import fallback_queries

    plan = normalise_plan({"regions": ["India"], "relevant_categories": ["crushers"]}, "x" * 20)
    queries = fallback_queries("indian crusher makers", plan)
    assert queries
    assert any("India" in q for q in queries)

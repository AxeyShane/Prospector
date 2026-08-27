"""The Drafter is the only stage judged by whether a stranger replies, so its
refusals matter as much as its output."""

import pytest

from prospector.stages.outreach import CHANNEL_RULES, _contact, _size, draft_one


class Row(dict):
    """sqlite3.Row stand-in: subscript access plus .keys()."""


def make_row(**over):
    base = {
        "company_key": "acme", "company_name": "Acme Crushers Ltd",
        "products": "Jaw crushers and screens",
        "qualification_evidence": "Owns an Australian subsidiary.\n"
                                  "- Australian subsidiary - Acme Pty Ltd, Perth WA",
        "qualification_matched": "Sells into Australia",
        "qualification_score": 90, "employees": "450",
        "revenue": "Not publicly available", "founded": "1985",
        "people_json": '[{"name": "Ravi Sharma", "title": "Managing Director"}]',
    }
    base.update(over)
    return Row(base)


def plan_with(**over):
    base = {"sender_profile": "I place Indian crushing equipment with Australian quarries.",
            "objective": "Find makers already supplying Australia.",
            "outreach_channel": "email"}
    base.update(over)
    return base


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.prompt = ""

    def ask_json(self, prompt, **kwargs):
        self.prompt = prompt
        return self.reply


@pytest.fixture
def client(monkeypatch):
    holder = {}

    def install(reply):
        fake = FakeClient(reply)
        holder["fake"] = fake
        monkeypatch.setattr("prospector.stages.outreach.get_client",
                            lambda name="outreach": fake)
        return fake

    return install


def test_a_good_draft_comes_back_whole(client):
    fake = client({"angle": "Australian subsidiary in Perth",
                   "subject": "parts lead times into perth",
                   "body": "I saw Acme runs its own Perth arm. Are parts lead "
                           "times from India a constraint for your customers there?",
                   "usable": True})
    result = draft_one(make_row(), plan_with())

    assert result["outreach_subject"] == "parts lead times into perth"
    assert "Perth" in result["outreach_body"]
    assert result["outreach_channel"] == "email"
    # The evidence has to reach the model, or the opener cannot be specific.
    assert "Acme Pty Ltd, Perth WA" in fake.prompt


def test_no_evidence_means_no_message(client):
    client({"body": "whatever", "usable": True})
    with pytest.raises(ValueError, match="no specific evidence"):
        draft_one(make_row(qualification_evidence=""), plan_with())

    with pytest.raises(ValueError, match="no specific evidence"):
        draft_one(make_row(
            qualification_evidence="No specific evidence found in public sources."),
            plan_with())


def test_no_sender_profile_means_no_message(client):
    # Without it every draft is boilerplate, which is worse than none.
    client({"body": "whatever", "usable": True})
    with pytest.raises(ValueError, match="sender profile"):
        draft_one(make_row(), plan_with(sender_profile=""))


def test_the_model_may_refuse_and_that_refusal_is_respected(client):
    client({"usable": False, "angle": "the evidence is too vague to open on"})
    with pytest.raises(ValueError, match="too vague"):
        draft_one(make_row(), plan_with())


def test_an_empty_body_is_a_failure_not_a_draft(client):
    client({"angle": "something", "subject": "hi", "body": "   ", "usable": True})
    with pytest.raises(ValueError, match="empty message"):
        draft_one(make_row(), plan_with())


def test_channel_rules_reach_the_prompt(client):
    fake = client({"angle": "a", "subject": "", "body": "short note", "usable": True})
    draft_one(make_row(), plan_with(outreach_channel="linkedin"))
    assert "280 characters" in fake.prompt

    draft_one(make_row(), plan_with(outreach_channel="call"))
    assert "read aloud" in fake.prompt


def test_every_channel_has_rules():
    for channel in ("email", "linkedin", "call"):
        assert CHANNEL_RULES[channel]
    # The banned-phrase list is what keeps the drafts from sounding like everyone
    # else's outreach, so it must survive refactors.
    from prospector.stages.outreach import PROMPT
    for phrase in ("I hope this finds you well", "reaching out", "touch base"):
        assert phrase in PROMPT


def test_the_prompt_forbids_inventing_things_it_did_not_read():
    from prospector.stages.outreach import PROMPT
    assert "unless one is quoted in the fact above" in PROMPT
    assert "you did not" in PROMPT


def test_missing_contact_is_stated_not_faked(client):
    fake = client({"angle": "a", "subject": "s", "body": "b", "usable": True})
    draft_one(make_row(people_json="[]"), plan_with())
    assert "no named contact" in fake.prompt
    assert _contact(make_row(people_json="[]")).startswith("no named contact")


def test_unpublished_figures_are_left_out_of_the_size_line():
    assert "Not publicly available" not in _size(make_row())
    assert "450" in _size(make_row())
    assert _size(make_row(employees="", revenue="Not publicly available",
                          founded="")) == "unknown"


def test_best_qualified_leads_are_drafted_first():
    from prospector.stages.outreach import pending_sql

    sql = pending_sql({"min_relevance_for_research": "Medium"})
    assert "qualification_score" in sql.split("ORDER BY")[1]
    assert "DESC" in sql.split("ORDER BY")[1]

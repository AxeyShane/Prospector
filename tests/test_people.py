"""Contacts are the part of the spreadsheet a person acts on directly.

A wrong revenue figure is an embarrassment. A contact who does not exist sends
someone into a phone call that goes badly, so the guard against invented people
is tested on its own.
"""

import pytest

from prospector.stages.people import _name_appears, people_one


SITE = """Leadership
Managing Director: Ravi Sharma
Export Director: Priya Nair
Contact us at info@acme.example
"""


class FakeRow(dict):
    """sqlite3.Row is read-only and indexable by name; a dict is close enough."""


def _row():
    return FakeRow(company_name="Acme Crushers Ltd", pages_json=None, website="")


@pytest.mark.parametrize("name,expected", [
    ("Ravi Sharma", True),
    ("R. Sharma", True),          # initials on the site, full name from search
    ("Sharma", True),
    ("Priya Nair", True),
    ("Rajesh Kumar", False),      # fluent, plausible, entirely invented
    ("", False),
])
def test_name_verification_accepts_real_people_and_rejects_invented_ones(name, expected):
    assert _name_appears(name, SITE) is expected


def test_invented_contacts_are_dropped_and_counted(monkeypatch, sample_plan):
    from prospector.stages import people as mod

    class FakeLLM:
        def ask_json(self, prompt, **kw):
            return {"people": [
                {"name": "Ravi Sharma", "title": "Managing Director",
                 "linkedin": "", "note": "decision maker"},
                {"name": "Rajesh Kumar", "title": "Head of Exports",
                 "linkedin": "https://linkedin.com/in/rajesh-kumar-9a2b",
                 "note": "invented by the model"},
            ]}

    monkeypatch.setattr(mod, "get_client", lambda stage="default": FakeLLM())
    monkeypatch.setattr(mod, "search_many", lambda q, max_results=6: [])
    monkeypatch.setattr(mod, "_site_text", lambda row: SITE)

    found, dropped = people_one(_row(), sample_plan)

    assert [p["name"] for p in found] == ["Ravi Sharma"]
    assert dropped == 1


def test_a_linkedin_url_that_was_never_seen_is_blanked(monkeypatch, sample_plan):
    """"linkedin.com is in the string" passes trivially for an invented slug."""
    from prospector.stages import people as mod

    class FakeLLM:
        def ask_json(self, prompt, **kw):
            return {"people": [{"name": "Ravi Sharma", "title": "Managing Director",
                                "linkedin": "https://linkedin.com/in/ravi-sharma-made-up",
                                "note": ""}]}

    monkeypatch.setattr(mod, "get_client", lambda stage="default": FakeLLM())
    monkeypatch.setattr(mod, "search_many", lambda q, max_results=6: [])
    monkeypatch.setattr(mod, "_site_text", lambda row: SITE)

    found, _ = people_one(_row(), sample_plan)
    assert found[0]["linkedin"] == ""


def test_a_linkedin_url_that_appeared_in_the_sources_is_kept(monkeypatch, sample_plan):
    from prospector.stages import people as mod

    url = "https://linkedin.com/in/ravi-sharma-acme"

    class FakeLLM:
        def ask_json(self, prompt, **kw):
            return {"people": [{"name": "Ravi Sharma", "title": "Managing Director",
                                "linkedin": url, "note": ""}]}

    monkeypatch.setattr(mod, "get_client", lambda stage="default": FakeLLM())
    monkeypatch.setattr(mod, "search_many", lambda q, max_results=6: [])
    monkeypatch.setattr(mod, "_site_text", lambda row: SITE + "\n" + url)

    found, _ = people_one(_row(), sample_plan)
    assert found[0]["linkedin"] == url

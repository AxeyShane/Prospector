from prospector.database import (
    get_connection, get_stats, normalise_key, update, upsert_lead, utc_now,
)


def test_normalise_key_collapses_legal_forms():
    assert normalise_key("M/S ASKA EQUIPMENTS PRIVATE LIMITED") == \
        normalise_key("Aska Equipments Pvt Ltd")
    assert normalise_key("TATA STEEL LIMITED") == normalise_key("Tata Steel Ltd.")
    assert normalise_key("Acme Crushers, LLC") == normalise_key("ACME CRUSHERS LLC")


def test_normalise_key_keeps_different_companies_apart():
    assert normalise_key("Bharat Wire Ropes Limited") != normalise_key("Usha Martin Limited")


def test_upsert_is_idempotent_and_records_alternate_names():
    assert upsert_lead("Acme Crushers Pvt Ltd", "discovered") is True
    assert upsert_lead("ACME CRUSHERS PRIVATE LIMITED", "discovered") is False

    row = get_connection().execute("SELECT * FROM leads").fetchone()
    assert get_stats()["total"] == 1
    assert "Also listed as" in (row["notes"] or "")


def test_discovery_provenance_is_recorded():
    upsert_lead("Acme Crushers", source_list="discovered",
                discovered_via="top crusher makers india",
                source_url="https://list.example/top")
    row = get_connection().execute("SELECT * FROM leads").fetchone()
    assert row["discovered_via"] == "top crusher makers india"
    assert row["discovery_source_url"] == "https://list.example/top"


def test_update_rejects_unknown_columns():
    upsert_lead("Acme Crushers")
    key = get_connection().execute("SELECT company_key FROM leads").fetchone()[0]
    update(key, relevance="High")
    assert get_stats()["classified"] == 1

    try:
        update(key, not_a_column="x")
    except ValueError as exc:
        assert "not_a_column" in str(exc)
    else:
        raise AssertionError("expected ValueError for an unknown column")


def test_attempts_are_counted_so_broken_rows_stop_being_retried():
    from prospector.database import bump_attempt

    upsert_lead("Acme Crushers")
    key = get_connection().execute("SELECT company_key FROM leads").fetchone()[0]
    bump_attempt(key, "resolve", "site unreachable")
    bump_attempt(key, "resolve", "site unreachable")

    row = get_connection().execute("SELECT * FROM leads").fetchone()
    assert row["resolve_attempts"] == 2
    assert "unreachable" in row["resolve_error"]


def test_stats_report_every_stage():
    upsert_lead("Acme Crushers")
    stats = get_stats()
    for key in ("total", "resolved", "crawled", "classified", "qualified",
                "profiled", "with_people", "by_relevance", "by_qualification"):
        assert key in stats


# ---------------------------------------------------------------------------
# normalise_key regressions.
# ---------------------------------------------------------------------------

def test_two_different_companies_do_not_share_a_key():
    """Indian Oil Corporation and Oil India Limited are not the same company.

    "INDIA"/"INDIAN" used to be stripped as noise wherever it appeared, so both
    of these normalised to "oil" and the second one was merged into the first
    with an "Also listed as" note. Two of the country's largest companies,
    silently collapsed into one row.
    """
    assert normalise_key("Indian Oil Corporation") != normalise_key("Oil India Limited")
    assert normalise_key("India Cements Ltd") != normalise_key("Cements Ltd")
    assert normalise_key("India Glycols Limited") != normalise_key("Glycols India Ltd")


def test_a_country_word_is_still_stripped_when_it_is_part_of_the_legal_name():
    """"Acme India Pvt Ltd" is Acme -- that is what the strip was for."""
    assert normalise_key("Acme India Pvt Ltd") == normalise_key("Acme Ltd")


def test_a_name_made_only_of_legal_words_still_gets_a_key():
    """It used to normalise to "" and upsert_lead dropped the row with no trace."""
    assert normalise_key("India Limited") != ""
    assert normalise_key("Pvt Ltd") != ""


def test_punctuation_and_plural_variants_agree():
    assert normalise_key("J.K. Cement Ltd") == normalise_key("JK Cement Ltd")
    assert normalise_key("Shakti Mining Equipments") == normalise_key("Shakti Mining Equipment")
    assert normalise_key("M/S ASKA EQUIPMENTS PRIVATE LIMITED") == \
        normalise_key("Aska Equipments Pvt Ltd")


def test_short_plurals_are_left_alone():
    """SONS, WORKS and GAS must not lose their S."""
    assert "son" not in normalise_key("Tata Sons").split()
    assert normalise_key("Bharat Works") == "bharat works"


def test_contact_count_counts_contacts_not_attempts():
    """`people_at` is stamped even when the stage found nobody.

    Counting it reported "200 with named contacts" over a Contacts tab holding
    twelve rows, on any run where search was rate limited.
    """
    import json
    from prospector.database import get_connection, get_stats, update, upsert_lead, utc_now

    conn = get_connection()
    upsert_lead("Found Contacts Ltd", conn=conn)
    upsert_lead("Empty Handed Ltd", conn=conn)
    update(normalise_key("Found Contacts Ltd"), conn=conn, people_at=utc_now(),
           people_json=json.dumps([{"name": "R Sharma", "title": "MD"}]))
    update(normalise_key("Empty Handed Ltd"), conn=conn, people_at=utc_now(),
           people_json="[]")

    stats = get_stats(conn)
    assert stats["with_people"] == 1
    assert stats["people_searched"] == 2


def test_two_rows_that_resolved_to_one_website_are_merged():
    """Name-based keys are deliberately cautious, so duplicates survive to here.

    Once both rows have a website there is real evidence, and a salesperson
    phoning the same company twice from one list notices immediately.
    """
    from prospector.database import get_connection, merge_duplicate_websites

    conn = get_connection()
    upsert_lead("Acme Crushers", conn=conn)
    upsert_lead("Acme Crushing Industries", conn=conn)
    update(normalise_key("Acme Crushers"), conn=conn,
           website="https://acme.example", website_confidence=0.9)
    update(normalise_key("Acme Crushing Industries"), conn=conn,
           website="https://www.acme.example", website_confidence=0.6,
           classified_at=utc_now(), products="Jaw crushers", relevance="High",
           qualified_at=utc_now(), qualification_level="Strong match",
           qualification_score=88, qualification_matched="Sells into Australia")

    assert merge_duplicate_websites(conn) == 1

    rows = conn.execute("SELECT * FROM leads").fetchall()
    assert len(rows) == 1
    # The keeper is the confident row, and it inherits what the loser knew.
    assert rows[0]["website_confidence"] == 0.9
    assert rows[0]["products"] == "Jaw crushers"
    assert "Acme Crushing Industries" in rows[0]["notes"]
    # A stage carries as a whole or not at all: a rating without its score sorts
    # to the bottom of the call list and can never be re-qualified.
    assert rows[0]["qualification_level"] == "Strong match"
    assert rows[0]["qualification_score"] == 88
    assert rows[0]["qualified_at"]


def test_a_stage_is_carried_whole_or_not_at_all():
    """Half a stage is worse than none of it.

    Carrying `qualification_level` without its score produced a top-rated lead
    scoring zero, sorted below everything, and excluded from re-qualifying
    because the pending query keys on the level being NULL.
    """
    from prospector.database import get_connection, merge_duplicate_websites

    conn = get_connection()
    upsert_lead("Acme Crushers", conn=conn)
    upsert_lead("Acme Crushing Industries", conn=conn)
    update(normalise_key("Acme Crushers"), conn=conn,
           website="https://acme.example", website_confidence=0.9)
    # A loser with a rating but no timestamp: a half-written row. Nothing from
    # that stage may be carried across.
    update(normalise_key("Acme Crushing Industries"), conn=conn,
           website="https://acme.example", website_confidence=0.2,
           qualification_level="Strong match")

    merge_duplicate_websites(conn)
    row = conn.execute("SELECT * FROM leads").fetchone()
    assert row["qualification_level"] is None


def test_different_companies_on_different_domains_are_left_alone():
    from prospector.database import get_connection, merge_duplicate_websites

    conn = get_connection()
    upsert_lead("Acme Crushers", conn=conn)
    upsert_lead("Bharat Screening", conn=conn)
    update(normalise_key("Acme Crushers"), conn=conn, website="https://acme.example")
    update(normalise_key("Bharat Screening"), conn=conn, website="https://bharat.example")

    assert merge_duplicate_websites(conn) == 0
    assert len(conn.execute("SELECT * FROM leads").fetchall()) == 2

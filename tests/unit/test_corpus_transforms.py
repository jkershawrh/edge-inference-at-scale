"""Tests for retrieval-oriented, deterministic corpus shaping."""

from scripts.build_summit_corpus import (
    build_city_docs,
    build_operations_docs,
    build_schedule_docs,
    build_session_docs,
    build_venue_docs,
    load_json,
)


def _by_id(documents):
    return {item["doc_id"]: item for item in documents}


def test_session_documents_denormalize_speaker_names():
    documents = build_session_docs(
        load_json("sessions.json"), load_json("speakers.json")
    )

    assert "Min Zhang" in _by_id(documents)["S003"]["text"]
    assert "Priya Patel" in _by_id(documents)["S004"]["text"]
    assert "Tara O'Brien" in _by_id(documents)["S008"]["text"]


def test_schedule_has_one_summary_per_day():
    documents = build_schedule_docs(load_json("schedule.json"))
    summaries = [item for item in documents if item["metadata"].get("summary")]

    assert len(summaries) == 3
    assert "Hands-On Labs" in summaries[-1]["text"]
    assert "Closing Keynote" in summaries[-1]["text"]


def test_venue_and_emergency_facts_are_retrieval_sized():
    venue = _by_id(build_venue_docs(load_json("venues.json")))
    city = _by_id(build_city_docs(load_json("city_guide.json")))

    assert any("Main Cafeteria" in item["text"] and "eat" in item["text"] for item in venue.values())
    assert any("SummitConnect-Guest" in item["text"] for item in venue.values())
    assert "shuttle" in venue["transport_info"]["text"].lower()
    assert "CVS" in city["city_emergency_pharmacy"]["text"]
    assert "Summit Medical Center" in city["city_emergency_hospital"]["text"]


def test_synthetic_operations_fill_required_retrieval_intents():
    source = load_json("operations.json")
    documents = build_operations_docs(source)
    by_id = _by_id(documents)

    assert source["fixture_notice"].startswith("SYNTHETIC TEST DATA")
    assert len(documents) == 6
    assert {item["metadata"]["intent"] for item in documents} == {
        "event_timezone",
        "agenda_change",
        "session_capacity",
        "accessibility",
        "event_policy",
        "information_desk",
    }
    assert all(item["metadata"]["synthetic"] is True for item in documents)
    assert "America/Chicago" in by_id["operations_event_timezone"]["text"]
    assert "not authoritative" in by_id["operations_agenda_changes"]["text"]
    assert "Level 1 east entrance lobby" in by_id["operations_information_desks"]["text"]

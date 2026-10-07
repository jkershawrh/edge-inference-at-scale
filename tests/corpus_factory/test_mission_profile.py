"""Focused behavior tests for mission planning and document classification."""

from dataclasses import replace

import pytest

from corpus_factory.mission import (
    CoverageRequirementSpec,
    DocumentClassificationSpec,
    MissionProfileSpec,
    build_document_classification,
    build_mission_profile,
)


def _coverage() -> tuple[CoverageRequirementSpec, ...]:
    return (
        CoverageRequirementSpec(
            requirement_id="summit-event-identity",
            category_id="event_identity",
            description="Conference identity, dates, location, and time zone",
            required_intents=("event_dates", "event_location"),
            safety_class="standard",
            minimum_sources=1,
        ),
        CoverageRequirementSpec(
            requirement_id="summit-safety",
            category_id="safety_emergency",
            description="Emergency contacts, first aid, and escalation paths",
            required_intents=("emergency_contact", "first_aid"),
            safety_class="critical",
            minimum_sources=1,
        ),
    )


def _mission() -> MissionProfileSpec:
    return MissionProfileSpec(
        profile_id="summit-connect-2026",
        version="1.0.0",
        vertical="conference_event",
        mission="Answer attendee questions for the synthetic Summit Connect event.",
        event_id="summit-connect-2026",
        event_name="Summit Connect 2026",
        event_start="2026-07-15T00:00:00-05:00",
        event_end="2026-07-18T00:00:00-05:00",
        timezone="America/Chicago",
        geographies=("summit-city", "convention-center"),
        languages=("en",),
        audiences=("attendees", "speakers", "staff"),
        accessibility_needs=("plain_language", "screen_reader"),
        delivery_channels=("sms", "web"),
        risk_level="high",
        owner_organization="Summit Connect Operations",
        approver_roles=("event_owner", "safety_reviewer"),
        valid_from="2026-07-01T00:00:00-05:00",
        valid_until="2026-07-18T23:59:59-05:00",
        expected_refresh_seconds=3600,
        required_information=_coverage(),
    )


def test_mission_profile_builder_is_deterministic_and_complete():
    first = build_mission_profile(_mission())
    second = build_mission_profile(_mission())

    assert first == second
    assert first["record_type"] == "corpus_mission_profile"
    assert first["mission_profile_id"] == "summit-connect-2026"
    assert {item["category_id"] for item in first["required_information"]} == {
        "event_identity",
        "safety_emergency",
    }


def test_mission_profile_rejects_duplicate_categories_before_publication():
    duplicate = replace(
        _coverage()[0], requirement_id="summit-event-identity-copy"
    )
    spec = replace(_mission(), required_information=(*_coverage(), duplicate))

    with pytest.raises(ValueError, match="coverage category IDs"):
        build_mission_profile(spec)


def test_classification_builder_captures_direct_answer_boundary():
    spec = DocumentClassificationSpec(
        classification_id="classification-summit-schedule-r1",
        mission_profile_id="summit-connect-2026",
        document_id="document-summit-schedule",
        document_revision=1,
        category_ids=("event_identity",),
        supported_intents=("event_dates", "event_location"),
        geographies=("summit-city", "convention-center"),
        languages=("en",),
        authority_class="official",
        safety_class="standard",
        consequence_of_error="Attendee receives incorrect schedule information.",
        valid_from="2026-07-01T00:00:00-05:00",
        valid_until="2026-07-18T23:59:59-05:00",
        stale_action="block",
        source_ids=("source-summit-schedule",),
        license="CC-BY-4.0",
        redistribution="permitted",
        verification_status="verified",
        verified_at="2026-07-01T12:00:00-05:00",
        reviewer_identity="event-owner@example.test",
        direct_answer_eligible=True,
        direct_answer_reason="Official, current event schedule.",
    )

    classification = build_document_classification(spec)

    assert classification["answer_policy"]["direct_answer_eligible"] is True
    assert classification["verification"]["status"] == "verified"
    assert classification["provenance"]["source_ids"] == ["source-summit-schedule"]


def test_unverified_document_cannot_be_marked_direct_answer_eligible():
    valid = DocumentClassificationSpec(
        classification_id="classification-summit-schedule-r1",
        mission_profile_id="summit-connect-2026",
        document_id="document-summit-schedule",
        document_revision=1,
        category_ids=("event_identity",),
        supported_intents=("event_dates",),
        geographies=("summit-city",),
        languages=("en",),
        authority_class="official",
        safety_class="standard",
        consequence_of_error="Incorrect event date.",
        valid_from="2026-07-01T00:00:00-05:00",
        valid_until=None,
        stale_action="block",
        source_ids=("source-summit-schedule",),
        license="CC-BY-4.0",
        redistribution="permitted",
        verification_status="pending",
        verified_at=None,
        reviewer_identity=None,
        direct_answer_eligible=True,
        direct_answer_reason="Not yet reviewed.",
    )

    with pytest.raises(ValueError, match="direct-answer eligibility"):
        build_document_classification(valid)

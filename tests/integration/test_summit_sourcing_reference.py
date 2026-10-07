"""End-to-end contract proof for the first bounded sourcing input set."""

import json
from pathlib import Path

from corpus_factory.coverage import plan_mission_coverage


ROOT = Path(__file__).resolve().parents[2]
MISSION = ROOT / "corpus_factory" / "fixtures" / "valid" / "corpus-mission-profile.json"
EXAMPLE = ROOT / "corpus_factory" / "examples" / "summit_connect"
AS_OF = "2026-07-02T12:00:00-05:00"


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_summit_reference_set_reports_real_gaps_deterministically():
    mission = _load(MISSION)
    registry = _load(EXAMPLE / "source-registry.json")
    classifications = _load(EXAMPLE / "document-classifications.json")[
        "classifications"
    ]

    first = plan_mission_coverage(
        mission, registry, classifications, as_of=AS_OF
    )
    second = plan_mission_coverage(
        mission, registry, classifications, as_of=AS_OF
    )

    assert first == second
    assert first["decision"] == "GAPS"
    assert first["summary"] == {
        "requirements": 11,
        "covered": 5,
        "gaps": 6,
        "conflicted": 0,
    }
    missing = {
        item["category_id"]: {
            gap["message"].removeprefix("no classification supports intent: ")
            for gap in item["gaps"]
            if gap["code"] == "MISSING_INTENT"
        }
        for item in first["requirements"]
        if item["status"] == "GAP"
    }
    assert missing == {
        "event_identity": {"event_timezone"},
        "help_escalation": {"information_desk"},
        "registration_policies": {"event_policy"},
        "schedule": {"agenda_change"},
        "sessions": {"session_capacity"},
        "venue_accessibility": {"accessibility"},
    }
    assert first["automation_boundary"] == {
        "advisory_only": True,
        "network_access": False,
        "publishes_release": False,
        "human_approval_required": True,
    }

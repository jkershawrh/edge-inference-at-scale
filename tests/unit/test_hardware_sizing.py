import copy

import pytest

from corpus_factory.hardware_sizing import HardwareSizingError, build_hardware_sizing_report


def _plan():
    return {
        "schema_version": "1.0",
        "plan_id": "field-v1",
        "assumptions": {
            "memory_headroom_fraction": 0.15, "storage_headroom_fraction": 0.20,
            "cpu_headroom_fraction": 0.25, "rollback_releases": 1,
            "autonomy_hours": 24, "reserve_fraction": 0.20,
            "battery_usable_fraction": 0.80, "conversion_efficiency": 0.85,
            "peak_sun_hours": 5, "solar_derate": 0.75,
            "estimated_average_watts": [15, 25, 45],
        },
        "trials": [
            {
                "trial_id": "quota-rag", "mode": "rag-only",
                "evidence_class": "OPENSHIFT_QUOTA", "passed": True,
                "cpu_limit_cores": 2, "peak_cpu_cores": 1.6, "memory_limit_gib": 4,
                "peak_memory_gib": 3.4, "base_storage_gib": 12,
                "release_storage_gib": 4, "p95_latency_ms": 700, "error_rate": 0,
            },
            {
                "trial_id": "quota-full", "mode": "full-generation",
                "evidence_class": "OPENSHIFT_QUOTA", "passed": True,
                "cpu_limit_cores": 8, "peak_cpu_cores": 6.4, "memory_limit_gib": 16,
                "peak_memory_gib": 10.5, "base_storage_gib": 12,
                "release_storage_gib": 6, "p95_latency_ms": 9000, "error_rate": 0.001,
            },
        ],
    }


def test_quota_plan_produces_estimates_not_hardware_qualification():
    report = build_hardware_sizing_report(_plan())
    assert report["qualification"] == "ESTIMATED_ONLY"
    assert {item["status"] for item in report["resource_envelopes"]} == {"ESTIMATED_ONLY"}
    assert report["power"]["evidence_class"] == "ESTIMATED_ASSUMPTION"
    middle = report["power"]["scenarios"][1]
    assert middle["daily_energy_wh"] == 600
    assert middle["nominal_battery_wh"] == pytest.approx(1058.82)
    assert middle["minimum_solar_array_watts"] == 160


def test_report_applies_headroom_and_rollback_storage():
    report = build_hardware_sizing_report(_plan())
    by_mode = {item["mode"]: item for item in report["resource_envelopes"]}
    assert by_mode["rag-only"]["recommended_cpu_cores"] == 2
    assert by_mode["rag-only"]["recommended_memory_gib"] == 4
    assert by_mode["rag-only"]["recommended_storage_gib"] == 25
    assert by_mode["full-generation"]["recommended_cpu_cores"] == 8
    assert by_mode["full-generation"]["recommended_memory_gib"] == 13
    assert by_mode["full-generation"]["recommended_storage_gib"] == 30


def test_physical_measurement_requires_identity_and_power():
    plan = _plan()
    plan["trials"][0]["evidence_class"] = "PHYSICAL_HARDWARE"
    with pytest.raises(HardwareSizingError, match="hardware_identity"):
        build_hardware_sizing_report(plan)


def test_physical_measurement_replaces_estimated_power_but_does_not_claim_cut():
    plan = _plan()
    plan["trials"][0].update({
        "evidence_class": "PHYSICAL_HARDWARE",
        "hardware_identity": {"board": "candidate-a", "revision": "1"},
        "average_watts": 18, "peak_watts": 32,
    })
    report = build_hardware_sizing_report(plan)
    assert report["qualification"] == "MEASURED_CANDIDATE"
    assert report["power"]["evidence_class"] == "MEASURED_HARDWARE"
    assert report["power"]["recommended_dc_supply_continuous_watts"] == 40
    assert "complete CUT" in report["warning"]


@pytest.mark.parametrize("field,value", [
    ("memory_headroom_fraction", 1), ("battery_usable_fraction", 0),
    ("rollback_releases", 1.5),
])
def test_invalid_assumptions_fail_closed(field, value):
    plan = copy.deepcopy(_plan())
    plan["assumptions"][field] = value
    with pytest.raises(HardwareSizingError):
        build_hardware_sizing_report(plan)


def test_quota_trial_rejects_claimed_watt_measurement():
    plan = _plan()
    plan["trials"][0]["average_watts"] = 12
    with pytest.raises(HardwareSizingError, match="quota trials"):
        build_hardware_sizing_report(plan)

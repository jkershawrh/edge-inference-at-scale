"""Deterministic pre-hardware resource, storage, battery, and solar sizing.

Software quota observations and physical-device measurements stay separate, so
an OpenShift simulation can narrow procurement without qualifying hardware.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "1.0"
EVIDENCE_CLASSES = {"OPENSHIFT_QUOTA", "PHYSICAL_HARDWARE"}
MODES = {"rag-only", "full-generation"}


class HardwareSizingError(ValueError):
    """Raised when sizing inputs are incomplete or contradictory."""


def _number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HardwareSizingError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= minimum:
        raise HardwareSizingError(f"{label} must be greater than {minimum}")
    return result


def _fraction(value: Any, label: str, *, allow_zero: bool = False) -> float:
    lower = -1.0 if allow_zero else 0.0
    result = _number(value, label, minimum=lower)
    if result > 1.0 or result < 0.0 or (not allow_zero and result == 0.0):
        raise HardwareSizingError(f"{label} is outside its fraction range")
    return result


def _validate_trial(trial: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "trial_id", "mode", "evidence_class", "passed", "cpu_limit_cores",
        "cpu_request_cores", "peak_cpu_cores", "memory_limit_gib",
        "memory_request_gib", "peak_memory_gib", "base_storage_gib",
        "release_storage_gib", "p95_latency_ms", "error_rate",
    }
    optional = {"hardware_identity", "average_watts", "peak_watts"}
    if set(trial) - required - optional or required - set(trial):
        raise HardwareSizingError(f"trial {trial.get('trial_id', '<unknown>')} has invalid fields")
    if not isinstance(trial["trial_id"], str) or not trial["trial_id"]:
        raise HardwareSizingError("trial_id must be a non-empty string")
    if trial["mode"] not in MODES or trial["evidence_class"] not in EVIDENCE_CLASSES:
        raise HardwareSizingError(f"trial {trial['trial_id']} has an unsupported classification")
    if not isinstance(trial["passed"], bool):
        raise HardwareSizingError(f"trial {trial['trial_id']} passed must be boolean")
    values = dict(trial)
    for key in (
        "cpu_limit_cores", "cpu_request_cores", "peak_cpu_cores",
        "memory_limit_gib", "memory_request_gib", "peak_memory_gib",
        "base_storage_gib", "release_storage_gib", "p95_latency_ms",
    ):
        values[key] = _number(trial[key], f"{trial['trial_id']}.{key}")
    values["error_rate"] = _fraction(
        trial["error_rate"], f"{trial['trial_id']}.error_rate", allow_zero=True
    )
    if values["peak_memory_gib"] > values["memory_limit_gib"]:
        raise HardwareSizingError(f"trial {trial['trial_id']} peak memory exceeds its limit")
    if values["peak_cpu_cores"] > values["cpu_limit_cores"]:
        raise HardwareSizingError(f"trial {trial['trial_id']} peak CPU exceeds its limit")
    if values["cpu_request_cores"] > values["cpu_limit_cores"]:
        raise HardwareSizingError(f"trial {trial['trial_id']} CPU request exceeds its limit")
    if values["memory_request_gib"] > values["memory_limit_gib"]:
        raise HardwareSizingError(f"trial {trial['trial_id']} memory request exceeds its limit")
    if trial["evidence_class"] == "PHYSICAL_HARDWARE":
        if not isinstance(trial.get("hardware_identity"), Mapping):
            raise HardwareSizingError(f"trial {trial['trial_id']} needs hardware_identity")
        for key in ("average_watts", "peak_watts"):
            values[key] = _number(trial.get(key), f"{trial['trial_id']}.{key}")
        if values["peak_watts"] < values["average_watts"]:
            raise HardwareSizingError(f"trial {trial['trial_id']} peak watts is below average")
    elif any(key in trial for key in optional):
        raise HardwareSizingError("quota trials cannot contain physical hardware measurements")
    return values


def _power_case(watts: float, assumptions: Mapping[str, float]) -> dict[str, float]:
    daily_wh = watts * 24.0
    battery_wh = (
        daily_wh * (assumptions["autonomy_hours"] / 24.0)
        * (1.0 + assumptions["reserve_fraction"])
        / assumptions["battery_usable_fraction"] / assumptions["conversion_efficiency"]
    )
    solar_watts = daily_wh / assumptions["peak_sun_hours"] / assumptions["solar_derate"]
    return {
        "average_load_watts": round(watts, 2),
        "daily_energy_wh": round(daily_wh, 2),
        "nominal_battery_wh": round(battery_wh, 2),
        "minimum_solar_array_watts": round(solar_watts, 2),
    }


def build_hardware_sizing_report(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Build a stable sizing report from quota trials and measurements."""
    required = {"schema_version", "plan_id", "assumptions", "trials"}
    if set(plan) != required or plan.get("schema_version") != SCHEMA_VERSION:
        raise HardwareSizingError("sizing plan contract is invalid")
    if not isinstance(plan["plan_id"], str) or not plan["plan_id"]:
        raise HardwareSizingError("plan_id must be a non-empty string")
    source = plan["assumptions"]
    fields = {
        "memory_headroom_fraction", "storage_headroom_fraction", "cpu_headroom_fraction",
        "rollback_releases", "autonomy_hours", "reserve_fraction",
        "battery_usable_fraction", "conversion_efficiency", "peak_sun_hours",
        "solar_derate", "estimated_average_watts",
    }
    if not isinstance(source, Mapping) or set(source) != fields:
        raise HardwareSizingError("sizing assumptions contract is invalid")
    assumptions = {
        "memory_headroom_fraction": _fraction(source["memory_headroom_fraction"], "memory_headroom_fraction"),
        "storage_headroom_fraction": _fraction(source["storage_headroom_fraction"], "storage_headroom_fraction"),
        "cpu_headroom_fraction": _fraction(source["cpu_headroom_fraction"], "cpu_headroom_fraction", allow_zero=True),
        "rollback_releases": int(_number(source["rollback_releases"], "rollback_releases", minimum=-1.0)),
        "autonomy_hours": _number(source["autonomy_hours"], "autonomy_hours"),
        "reserve_fraction": _fraction(source["reserve_fraction"], "reserve_fraction", allow_zero=True),
        "battery_usable_fraction": _fraction(source["battery_usable_fraction"], "battery_usable_fraction"),
        "conversion_efficiency": _fraction(source["conversion_efficiency"], "conversion_efficiency"),
        "peak_sun_hours": _number(source["peak_sun_hours"], "peak_sun_hours"),
        "solar_derate": _fraction(source["solar_derate"], "solar_derate"),
    }
    if assumptions["memory_headroom_fraction"] >= 1.0 or assumptions["storage_headroom_fraction"] >= 1.0:
        raise HardwareSizingError("memory and storage headroom fractions must be below 1")
    if source["rollback_releases"] != assumptions["rollback_releases"]:
        raise HardwareSizingError("rollback_releases must be an integer")
    watts_source = source["estimated_average_watts"]
    if not isinstance(watts_source, Sequence) or isinstance(watts_source, (str, bytes)) or not watts_source:
        raise HardwareSizingError("estimated_average_watts must be a non-empty list")
    estimated_watts = sorted({_number(value, "estimated_average_watts") for value in watts_source})
    trials_source = plan["trials"]
    if not isinstance(trials_source, Sequence) or isinstance(trials_source, (str, bytes)) or not trials_source:
        raise HardwareSizingError("trials must be a non-empty list")
    trials = [_validate_trial(item) for item in trials_source if isinstance(item, Mapping)]
    if len(trials) != len(trials_source) or len({item["trial_id"] for item in trials}) != len(trials):
        raise HardwareSizingError("trials must be unique objects")

    envelopes = []
    for mode in sorted(MODES):
        passing = [item for item in trials if item["mode"] == mode and item["passed"]]
        if not passing:
            envelopes.append({"mode": mode, "status": "NO_PASSING_TRIAL"})
            continue
        selected = min(passing, key=lambda item: (item["memory_limit_gib"], item["cpu_limit_cores"], item["trial_id"]))
        memory = max(
            selected["memory_request_gib"],
            selected["peak_memory_gib"] / (1.0 - assumptions["memory_headroom_fraction"]),
        )
        cpu = max(
            selected["cpu_request_cores"],
            selected["peak_cpu_cores"] * (1.0 + assumptions["cpu_headroom_fraction"]),
        )
        storage = (
            selected["base_storage_gib"]
            + selected["release_storage_gib"] * (1 + assumptions["rollback_releases"])
        ) / (1.0 - assumptions["storage_headroom_fraction"])
        measured = selected["evidence_class"] == "PHYSICAL_HARDWARE"
        envelope = {
            "mode": mode,
            "status": "MEASURED_CANDIDATE" if measured else "ESTIMATED_ONLY",
            "source_trial_id": selected["trial_id"],
            "source_evidence_class": selected["evidence_class"],
            "recommended_cpu_cores": math.ceil(cpu),
            "recommended_memory_gib": math.ceil(memory),
            "recommended_storage_gib": math.ceil(storage),
            "observed_p95_latency_ms": selected["p95_latency_ms"],
            "observed_error_rate": selected["error_rate"],
        }
        if measured:
            envelope["hardware_identity"] = selected["hardware_identity"]
        envelopes.append(envelope)

    physical = [item for item in trials if item["passed"] and item["evidence_class"] == "PHYSICAL_HARDWARE"]
    if physical:
        power_class = "MEASURED_HARDWARE"
        power_values = sorted({item["average_watts"] for item in physical})
        peak_supply = max(item["peak_watts"] for item in physical) * 1.25
    else:
        power_class = "ESTIMATED_ASSUMPTION"
        power_values = estimated_watts
        peak_supply = max(power_values) * 1.25
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "hardware_sizing_report",
        "plan_id": plan["plan_id"],
        "qualification": "MEASURED_CANDIDATE" if physical else "ESTIMATED_ONLY",
        "warning": (
            "OpenShift quotas bound software resources but do not qualify board performance, radio behavior, thermals, or power."
            if not physical else
            "Physical measurements are candidate evidence; the complete CUT still controls qualification."
        ),
        "resource_envelopes": envelopes,
        "power": {
            "evidence_class": power_class,
            "recommended_dc_supply_continuous_watts": math.ceil(peak_supply),
            "scenarios": [_power_case(watts, assumptions) for watts in power_values],
        },
        "assumptions": dict(source),
    }

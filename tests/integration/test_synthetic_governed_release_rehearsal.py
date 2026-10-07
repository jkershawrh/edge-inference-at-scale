"""End-to-end governed synthetic disaster release rehearsal."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from corpus_factory.governance import object_digest
from corpus_factory.validator import validate_instance


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_synthetic_disaster_rehearsal.py"


def _run(tmp_path):
    output = tmp_path / "rehearsal"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return output, json.loads((output / "rehearsal-summary.json").read_text())


def test_one_command_rehearses_governed_release_without_claiming_hardware_cut(tmp_path):
    output, summary = _run(tmp_path)

    assert summary["decision"] == "PASS"
    software = summary["software_evidence"]
    assert software["evidence_class"] == "SIMULATED_SOFTWARE"
    assert software["coverage"] == "COVERED"
    assert software["suitability"] == "PASS"
    assert software["promotion"] == "PASS"
    assert software["offline_decryption"] == "PASS"
    assert software["activation"] == "success"
    assert software["restart_persistence"] == "PASS"
    assert software["answer_behavior"]["grounded"] == {
        "passed": True, "mode": "rag_direct"
    }
    assert software["answer_behavior"]["no_answer_refusal"] == {
        "passed": True, "mode": "refused_emergency_grounding"
    }
    assert summary["hardware_cut"]["status"] == "NOT_RUN"
    assert summary["hardware_cut"]["evidence_class"] == "NO_HARDWARE_EVIDENCE"
    assert {"physical GSM/SMS modem", "physical LoRa radio", "battery/solar runtime"}.issubset(
        summary["hardware_cut"]["unproven"]
    )
    body = {key: value for key, value in summary.items() if key != "summary_digest"}
    assert summary["summary_digest"] == object_digest(body)
    assert not (output / "receipt-private-key.pem").exists()


def test_rehearsal_uses_exact_contracts_package_and_offline_activation_state(tmp_path):
    output, summary = _run(tmp_path)
    evidence = output / "evidence"
    for filename, record_type in (
        ("mission-profile.json", "corpus_mission_profile"),
        ("source-registry.json", "source_registry"),
        ("coverage-report.json", "coverage_report"),
        ("evaluation-attestation.json", "evaluation_attestation"),
        ("release-signing-authorization.json", "release_signing_authorization"),
        ("activation-receipt.json", "activation_receipt"),
    ):
        validate_instance(json.loads((evidence / filename).read_text()), record_type)

    package = output / summary["artifacts"]["candidate_package"]
    manifest_bytes = (package / "manifest.json").read_bytes()
    assert "governance" in json.loads(manifest_bytes)
    assert (package / "manifest.sig").is_file()
    assert summary["software_evidence"]["candidate_release_digest"] == (
        "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    )
    packaged_documents = json.loads((package / "documents.json").read_text())
    assert "doc-evacuation-r4-current" in packaged_documents
    assert "doc-evacuation-r4-old" not in packaged_documents
    assert "doc-shelter-r5-distractor" not in packaged_documents

    active = json.loads((output / "activation" / "current.json").read_text())
    assert active["active_digest"] == summary["software_evidence"]["candidate_release_digest"]
    assert active["active_sequence"] == 7
    assert active["sequence_floor"] == 7
    assert active["mode"] == "production"


def test_rehearsal_output_is_immutable_and_exact_candidate_is_reproducible(tmp_path):
    first, first_summary = _run(tmp_path / "first")
    second, second_summary = _run(tmp_path / "second")
    assert (
        first_summary["software_evidence"]["candidate_release_digest"]
        == second_summary["software_evidence"]["candidate_release_digest"]
    )

    rerun = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(first)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rerun.returncode == 1
    assert "already exists" in rerun.stderr

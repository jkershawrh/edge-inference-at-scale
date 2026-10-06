"""Offline tests for the fail-closed corpus promotion gate."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

from corpus_factory.promotion import BINDING_FIELDS, canonical_json, evaluate_promotion


ROOT = Path(__file__).resolve().parents[2]


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _binding():
    characters = "123456789abcdef"
    return {
        field: _digest(characters[index])
        for index, field in enumerate(BINDING_FIELDS)
    }


def _retrieval_class():
    return {
        "case_count": 50,
        "recall_at_3": 1.0,
        "required_evidence_coverage_at_5": 1.0,
        "mrr": 1.0,
        "scope_accuracy": 1.0,
        "forbidden_context_rate": 0.0,
        "failed_case_ids": [],
    }


def _answer_class():
    return {
        "case_count": 50,
        "grounded_pass_rate": 1.0,
        "claim_precision": 1.0,
        "citation_precision": 1.0,
        "citation_recall": 1.0,
        "no_answer_recall": 1.0,
        "stale_refusal_recall": 1.0,
        "critical_entity_preservation": 1.0,
        "unsupported_claim_rate": 0.0,
        "failed_case_ids": [],
    }


def _passing_evidence():
    binding = _binding()
    release = {
        "binding": copy.deepcopy(binding),
        "passed": True,
        "checks": {name: True for name in (
            "schema", "lineage", "scope", "freshness", "conflicts", "licenses",
            "approvals", "hashes", "signatures", "artifact_completeness",
        )},
        "unresolved_critical_conflicts": 0,
        "unapproved_critical_documents": 0,
        "policy_violations": 0,
        "security_fixture_failures": 0,
    }
    retrieval = {
        "binding": copy.deepcopy(binding),
        "safety_classes": {name: _retrieval_class() for name in ("critical", "high", "standard", "advisory")},
        "policy_violations": 0,
        "deterministic_runs": 3,
        "rankings_identical": True,
    }
    answer = {
        "binding": copy.deepcopy(binding),
        "safety_classes": {name: _answer_class() for name in ("critical", "high", "standard", "advisory")},
        "policy_violations": 0,
        "deterministic_runs": 3,
        "all_runs_passed": True,
        "citation_lineage_resolution": 1.0,
    }
    edge = {
        "binding": copy.deepcopy(binding),
        "profiles": {
            "values-lab-small": {
                "oom_kills": 0,
                "evictions": 0,
                "corrupt_indexes": 0,
                "failed_readiness_transitions": 0,
                "storage_headroom_fraction": 0.25,
                "memory_headroom_fraction": 0.20,
                "retrieval_error_rate": 0.0,
                "grounded_answer_error_rate": 0.0,
                "warm_retrieval_p95_ms": 700,
                "warm_retrieval_p99_ms": 1400,
                "critical_rag_direct_p95_ms": 1900,
                "llm_end_to_end_p95_ms": 5000,
                "package_verification_passed": True,
                "index_build_passed": True,
                "restart_recovery_passed": True,
                "disconnected_smoke_passed": True,
            }
        },
    }
    suitability = {
        "report_id": _digest("b"),
        "decision": "PASS",
        "failures": [],
        "requirements": [
            {"requirement_id": "critical-shelter", "passed": True}
        ],
        "bindings": {
            "event_policy_subject_digest": binding["policy_digest"],
            "event_policy_record_digest": _digest("c"),
            "source_aggregate_digest": binding["source_aggregate_digest"],
            "document_aggregate_digest": binding["document_aggregate_digest"],
            "case_aggregate_digest": binding["case_aggregate_digest"],
        },
    }
    return release, retrieval, answer, edge, suitability


def test_all_five_layers_pass_independently_and_report_is_deterministic():
    evidence = _passing_evidence()
    first = evaluate_promotion(*evidence)
    second = evaluate_promotion(*copy.deepcopy(evidence))
    assert first == second
    assert first["decision"] == "PASS"
    assert all(value["passed"] for value in first["layers"].values())
    assert first["report_id"].startswith("sha256:")
    assert canonical_json(first) == canonical_json(second)


def test_mismatched_artifact_binding_blocks_promotion():
    release, retrieval, answer, edge, suitability = _passing_evidence()
    answer["binding"]["model_digest"] = _digest("a")
    report = evaluate_promotion(release, retrieval, answer, edge, suitability)
    assert report["decision"] == "FAIL"
    assert report["binding"]["model_digest"] is None
    assert any(item["code"] == "BINDING_MISMATCH" for item in report["failures"])


def test_missing_evidence_fails_closed():
    release, retrieval, answer, edge, suitability = _passing_evidence()
    del release["checks"]["signatures"]
    del retrieval["safety_classes"]["advisory"]["mrr"]
    del edge["profiles"]["values-lab-small"]["disconnected_smoke_passed"]
    report = evaluate_promotion(release, retrieval, answer, edge, suitability)
    assert report["decision"] == "FAIL"
    assert report["layers"]["release_validity"]["passed"] is False
    assert report["layers"]["retrieval"]["passed"] is False
    assert report["layers"]["edge_operation"]["passed"] is False


def test_aggregate_score_cannot_hide_a_critical_case_failure():
    release, retrieval, answer, edge, suitability = _passing_evidence()
    retrieval["overall_score"] = 1.0
    retrieval["safety_classes"]["critical"]["failed_case_ids"] = ["critical-evacuation-007"]
    report = evaluate_promotion(release, retrieval, answer, edge, suitability)
    assert report["decision"] == "FAIL"
    assert any(item["code"] == "CRITICAL_CASE_FAILED" for item in report["failures"])


def test_each_safety_class_uses_its_own_threshold():
    release, retrieval, answer, edge, suitability = _passing_evidence()
    retrieval["safety_classes"]["high"]["recall_at_3"] = 0.989
    retrieval["safety_classes"]["standard"]["recall_at_3"] = 0.97
    report = evaluate_promotion(release, retrieval, answer, edge, suitability)
    assert report["decision"] == "FAIL"
    paths = [item["path"] for item in report["failures"]]
    assert "retrieval.safety_classes.high.recall_at_3" in paths
    assert "retrieval.safety_classes.standard.recall_at_3" not in paths


def test_cli_writes_report_and_returns_nonzero_when_blocked(tmp_path):
    release, retrieval, answer, edge, suitability = _passing_evidence()
    answer["safety_classes"]["critical"]["unsupported_claim_rate"] = 0.01
    paths = []
    for index, value in enumerate((release, retrieval, answer, edge, suitability)):
        path = tmp_path / f"evidence-{index}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        paths.append(path)
    output = tmp_path / "promotion.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "evaluate_corpus_release.py"),
            "--release-validity", str(paths[0]),
            "--retrieval", str(paths[1]),
            "--grounded-answer", str(paths[2]),
            "--edge-profiles", str(paths[3]),
            "--suitability", str(paths[4]),
            "--output", str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert json.loads(output.read_text(encoding="utf-8"))["decision"] == "FAIL"


def test_suitability_failure_blocks_otherwise_passing_release():
    release, retrieval, answer, edge, suitability = _passing_evidence()
    suitability["decision"] = "FAIL"
    suitability["failures"] = [{"code": "MISSING_REQUIRED_FACT"}]
    suitability["requirements"][0]["passed"] = False

    report = evaluate_promotion(release, retrieval, answer, edge, suitability)

    assert report["decision"] == "FAIL"
    assert report["layers"]["corpus_suitability"]["passed"] is False


def test_suitability_policy_binding_must_match_promoted_tuple():
    release, retrieval, answer, edge, suitability = _passing_evidence()
    suitability["bindings"]["event_policy_subject_digest"] = _digest("f")

    report = evaluate_promotion(release, retrieval, answer, edge, suitability)

    assert report["decision"] == "FAIL"
    assert any(
        item["layer"] == "corpus_suitability" and item["code"] == "BINDING_MISMATCH"
        for item in report["failures"]
    )

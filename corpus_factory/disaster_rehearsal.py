"""Repeatable software-only governed release rehearsal for the disaster fixture."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import tarfile
import tempfile
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping
from unittest.mock import AsyncMock, MagicMock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from backend.services.message_router.main import MessageRouter
from backend.services.rag_service.activation import (
    CorpusActivationManager,
    ReleaseCandidate,
    RollbackRejected,
)
from backend.services.rag_service.corpus_package import (
    CorpusValidationError,
    validate_corpus_package,
)
from backend.services.rag_service.receipt_signing import Ed25519ReceiptSigner
from backend.services.rag_service.recovery_authorization import (
    verify_recovery_authorization,
)
from backend.shared.config import settings
from backend.shared.models import ChannelMessage, MessageChannel
from corpus_factory.distribution import (
    DistributionError,
    EncryptionRecipient,
    decrypt_transfer_set,
    export_encrypted_transfer_set,
    verify_transfer_set,
)
from corpus_factory.evaluation_attestation import (
    authorize_release_signing,
    finalize_attestation,
    prepare_attestation_statement,
    signing_payload,
)
from corpus_factory.governance import object_digest
from corpus_factory.promotion import BINDING_FIELDS, evaluate_promotion
from corpus_factory.suitability import evaluate_suitability
from corpus_factory.validator import event_policy_subject_digest, validate_instance
from scripts.package_corpus import build_package


AS_OF = "2026-10-06T18:00:00Z"
EVENT_ID = "flood-response-region-4"
SITE_ID = "site-r4-north"
VERSION = "1.0.0"
SEQUENCE = 7
MISSION_ID = "mission-flood-response-region-4"
REGISTRY_ID = "registry-flood-response-region-4"
_FIXTURES = Path(__file__).with_name("examples") / "synthetic_disaster"


class RehearsalError(ValueError):
    """The rehearsal could not prove a required software gate."""


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _load(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _governance_records(
    policy: Mapping[str, Any],
    sources: list[Mapping[str, Any]],
    documents: list[Mapping[str, Any]],
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    mission = {
        "schema_version": "2.0.0",
        "record_type": "corpus_mission_profile",
        "mission_profile_id": MISSION_ID,
        "version": VERSION,
        "vertical": "disaster-response",
        "mission": policy["mission"],
        "event": {
            "event_id": EVENT_ID,
            "name": "Synthetic Region 4 Flood Response",
            "starts_at": "2026-10-06T00:00:00Z",
            "ends_at": "2026-10-08T00:00:00Z",
            "timezone": "America/Chicago",
        },
        "scope": {
            "geographies": policy["deployment_scope"]["geographies"],
            "languages": policy["deployment_scope"]["languages"],
            "audiences": policy["deployment_scope"]["audiences"],
            "accessibility_needs": [],
            "delivery_channels": policy["deployment_scope"]["delivery_channels"],
        },
        "governance": {
            "risk_level": "critical",
            "owner_organization": "Synthetic Region 4 Emergency Office",
            "required_approver_roles": ["domain_sme", "local_sme"],
        },
        "validity": {
            "valid_from": "2026-10-06T00:00:00Z",
            "valid_until": "2026-10-08T00:00:00Z",
            "expected_refresh_seconds": 3600,
        },
        "required_information": [
            {
                "requirement_id": item["requirement_id"],
                "category_id": item["requirement_id"].removeprefix("coverage-"),
                "description": "Verified " + item["requirement_id"].removeprefix("coverage-") + " guidance.",
                "required_intents": [item["requirement_id"].removeprefix("coverage-") + "-guidance"],
                "safety_class": item["safety_class"],
                "minimum_sources": item["minimum_independent_sources"],
            }
            for item in policy["coverage_requirements"]
        ],
    }
    validate_instance(mission, "corpus_mission_profile")

    relevant_sources = [source for source in sources if source["scope"]["geographies"] == ["region-4/zone-north"]]
    registry_sources = []
    for source in relevant_sources:
        source_id = source["source_id"]
        publisher = source["publisher"]
        registry_sources.append({
            "source_id": source_id,
            "enabled": True,
            "classification": {
                "vertical": "disaster-response",
                "information_classes": source["scope"]["subjects"],
                "risk_level": "critical",
            },
            "approval": {
                "status": "approved",
                "checks": {
                    "authority_verified": True,
                    "geography_verified": True,
                    "license_verified": True,
                    "validity_verified": True,
                    "language_verified": True,
                    "risk_classified": True,
                },
                "reviewer_identity": "synthetic.registry.reviewer@example.test",
                "reviewed_at": "2026-10-06T17:30:00Z",
                "rationale": "Synthetic drill source approved for software rehearsal.",
            },
            "publisher": publisher,
            "steward": source["steward"],
            "authority_class": source["authority_class"],
            "connector": {
                "connector_id": "connector-" + source_id.removeprefix("source-"),
                "type": "https",
                "url": "https://synthetic.example.test/" + source_id + ".json",
                "allowed_hosts": ["synthetic.example.test"],
                "allowed_media_types": ["application/json"],
                "maximum_bytes": 1048576,
                "timeout_seconds": 10,
                "maximum_redirects": 0,
                "auth_secret_ref": None,
            },
            "rights": source["rights"],
            "scope": source["scope"],
            "freshness": source["freshness"],
            "sensitivity": source["sensitivity"],
            "distribution": source["distribution"],
        })
    registry = {
        "schema_version": "2.0.0",
        "record_type": "source_registry",
        "registry_id": REGISTRY_ID,
        "event_id": EVENT_ID,
        "version": VERSION,
        "acquisition_policy_version": "synthetic-drill-v1",
        "sources": registry_sources,
    }
    validate_instance(registry, "source_registry")

    qualifying_ids = {
        requirement["requirement_id"]: requirement["qualifying_document_ids"]
        for requirement in evaluate_suitability(
            policy, sources, documents, _load("evaluation_cases.json"),
            as_of=AS_OF, time_confidence="trusted",
        )["requirements"]
    }
    document_index = {document["document_id"]: document for document in documents}
    requirements = []
    all_classifications = set()
    for item in mission["required_information"]:
        document_ids = qualifying_ids[item["requirement_id"]]
        classifications = ["classification-" + document_id.removeprefix("doc-") for document_id in document_ids]
        all_classifications.update(classifications)
        source_ids = sorted({
            citation["source_id"]
            for document_id in document_ids
            for citation in document_index[document_id]["source_citations"]
        })
        requirements.append({
            "requirement_id": item["requirement_id"],
            "category_id": item["category_id"],
            "safety_class": item["safety_class"],
            "status": "COVERED",
            "classification_ids": classifications,
            "qualifying_classification_ids": classifications,
            "qualifying_source_ids": source_ids,
            "gaps": [], "conflicts": [], "recommended_actions": [],
        })
    coverage = {
        "schema_version": "1.0.0",
        "record_type": "coverage_report",
        "mission_profile_id": MISSION_ID,
        "event_id": EVENT_ID,
        "registry_id": REGISTRY_ID,
        "as_of": AS_OF,
        "decision": "COVERED",
        "summary": {"requirements": 3, "covered": 3, "gaps": 0, "conflicted": 0},
        "requirements": requirements,
        "source_findings": [], "conflicts": [],
        "automation_boundary": {
            "advisory_only": True, "network_access": False,
            "publishes_release": False, "human_approval_required": True,
        },
    }
    coverage["report_id"] = object_digest(coverage)
    validate_instance(coverage, "coverage_report")

    registry_digest = object_digest(registry)
    records = []
    for document_id in sorted({item for values in qualifying_ids.values() for item in values}):
        document = document_index[document_id]
        records.append({
            "classification": {
                "classification_id": "classification-" + document_id.removeprefix("doc-"),
                "mission_profile_id": MISSION_ID,
            },
            "document": {"document_id": document_id, "digest": document["document_digest"]},
            "lifecycle": {"state": "classified"},
        })
    lineage = {
        "schema_version": "1.0.0", "record_type": "corpus_lineage_manifest",
        "event_id": EVENT_ID,
        "source_registry": {"registry_id": REGISTRY_ID, "digest": registry_digest},
        "classification_set_digest": object_digest(sorted(all_classifications)),
        "records": records,
    }
    lineage["manifest_digest"] = object_digest(lineage)
    return mission, registry, coverage, lineage


def _promotion_evidence(
    release_digest: str,
    package_governance: Mapping[str, Any],
    coverage: Mapping[str, Any],
    suitability: Mapping[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    digest = lambda character: "sha256:" + character * 64
    bindings = suitability["bindings"]
    binding = {
        "release_digest": release_digest,
        "corpus_digest": release_digest,
        "eval_set_digest": bindings["case_aggregate_digest"],
        "model_digest": digest("0"),
        "embedding_model_digest": digest("1"),
        "chunker_digest": digest("2"),
        "policy_digest": bindings["event_policy_subject_digest"],
        "source_aggregate_digest": bindings["source_aggregate_digest"],
        "document_aggregate_digest": bindings["document_aggregate_digest"],
        "case_aggregate_digest": bindings["case_aggregate_digest"],
    }
    assert set(binding) == set(BINDING_FIELDS)
    release = {
        "binding": dict(binding), "passed": True,
        "checks": {name: True for name in (
            "schema", "lineage", "scope", "freshness", "conflicts", "licenses",
            "approvals", "hashes", "signatures", "artifact_completeness",
        )},
        "unresolved_critical_conflicts": 0, "unapproved_critical_documents": 0,
        "policy_violations": 0, "security_fixture_failures": 0,
        "coverage_report_binding": {
            field: coverage[field]
            for field in ("schema_version", "report_id", "mission_profile_id", "event_id", "registry_id")
        },
        "package_governance": package_governance,
    }
    retrieval_class = {
        "case_count": 50, "recall_at_3": 1.0, "required_evidence_coverage_at_5": 1.0,
        "mrr": 1.0, "scope_accuracy": 1.0, "forbidden_context_rate": 0.0,
        "failed_case_ids": [],
    }
    answer_class = {
        "case_count": 50, "grounded_pass_rate": 1.0, "claim_precision": 1.0,
        "citation_precision": 1.0, "citation_recall": 1.0, "no_answer_recall": 1.0,
        "stale_refusal_recall": 1.0, "critical_entity_preservation": 1.0,
        "unsupported_claim_rate": 0.0, "failed_case_ids": [],
    }
    retrieval = {
        "binding": dict(binding), "policy_violations": 0, "deterministic_runs": 3,
        "rankings_identical": True,
        "safety_classes": {name: dict(retrieval_class) for name in ("critical", "high", "standard", "advisory")},
    }
    answer = {
        "binding": dict(binding), "policy_violations": 0, "deterministic_runs": 3,
        "all_runs_passed": True, "citation_lineage_resolution": 1.0,
        "safety_classes": {name: dict(answer_class) for name in ("critical", "high", "standard", "advisory")},
    }
    edge = {
        "binding": dict(binding),
        "profiles": {"software-rehearsal": {
            "oom_kills": 0, "evictions": 0, "corrupt_indexes": 0,
            "failed_readiness_transitions": 0, "storage_headroom_fraction": 0.25,
            "memory_headroom_fraction": 0.20, "retrieval_error_rate": 0.0,
            "grounded_answer_error_rate": 0.0, "warm_retrieval_p95_ms": 25,
            "warm_retrieval_p99_ms": 50, "critical_rag_direct_p95_ms": 100,
            "llm_end_to_end_p95_ms": 0, "package_verification_passed": True,
            "index_build_passed": True, "restart_recovery_passed": True,
            "disconnected_smoke_passed": True,
        }},
    }
    return release, retrieval, answer, edge


def _tar_package(package: Path, output: Path) -> None:
    with tarfile.open(output, "w") as archive:
        for path in sorted(package.iterdir(), key=lambda item: item.name):
            info = archive.gettarinfo(str(path), arcname=path.name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            with path.open("rb") as handle:
                archive.addfile(info, handle)


def _extract_package(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    with tarfile.open(archive_path, "r") as archive:
        for member in archive.getmembers():
            if not member.isfile() or Path(member.name).is_absolute() or ".." in Path(member.name).parts:
                raise RehearsalError("corpus archive contains an unsafe member")
        archive.extractall(destination)


def _exercise_signature_failure(package: Path, public_key: Path) -> Dict[str, Any]:
    """Prove a modified release signature is rejected by the runtime verifier."""

    with tempfile.TemporaryDirectory(prefix="evy-signature-negative-") as directory:
        damaged = Path(directory) / "package"
        shutil.copytree(package, damaged)
        (damaged / "manifest.sig").write_text(
            base64.b64encode(b"\x00" * 64).decode("ascii") + "\n",
            encoding="ascii",
        )
        try:
            validate_corpus_package(
                str(damaged / "manifest.json"),
                expected_event_id=EVENT_ID,
                expected_version=VERSION,
                public_key_path=str(public_key),
                require_signature=True,
            )
        except CorpusValidationError as exc:
            return {"passed": True, "rejected": True, "reason": str(exc)}
    raise RehearsalError("modified release signature was accepted")


def _exercise_transfer_failures(
    transfer: Path,
    transfer_public_key: Any,
) -> Dict[str, Any]:
    """Prove both changed ciphertext and an interrupted transfer fail closed."""

    def verify(root: Path) -> None:
        verify_transfer_set(
            root,
            expected_event_id=EVENT_ID,
            expected_site_id=SITE_ID,
            allowed_classifications={"restricted"},
            sequence_floor=0,
            signature_verifier=lambda payload, signature: _verify_signature(
                transfer_public_key, payload, signature
            ),
        )

    results: Dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="evy-transfer-negative-") as directory:
        root = Path(directory)
        ciphertext = root / "ciphertext"
        interrupted = root / "interrupted"
        shutil.copytree(transfer, ciphertext)
        shutil.copytree(transfer, interrupted)
        index = json.loads((ciphertext / "index.json").read_text(encoding="utf-8"))
        manifest_digest = index["manifests"][0]["digest"].removeprefix("sha256:")
        manifest = json.loads(
            (ciphertext / "blobs" / "sha256" / manifest_digest).read_text(encoding="utf-8")
        )
        artifact_path = Path(manifest["artifacts"][0]["path"])

        changed = ciphertext / artifact_path
        payload = changed.read_bytes()
        changed.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
        try:
            verify(ciphertext)
        except DistributionError as exc:
            results["ciphertext_tamper"] = {
                "passed": True, "rejected": True, "reason": str(exc)
            }
        else:
            raise RehearsalError("modified encrypted-transfer ciphertext was accepted")

        (interrupted / artifact_path).unlink()
        try:
            verify(interrupted)
        except DistributionError as exc:
            results["interrupted_transfer"] = {
                "passed": True, "rejected": True, "reason": str(exc)
            }
        else:
            raise RehearsalError("incomplete encrypted transfer was accepted")
    return results


def _exercise_activation_rejection(
    manager: CorpusActivationManager,
    package: Path,
) -> Dict[str, Any]:
    """Prove a different digest cannot replace the release at the sequence floor."""

    candidate = ReleaseCandidate("sha256:" + "f" * 64, SEQUENCE, package)
    try:
        manager.activate(candidate)
    except RollbackRejected as exc:
        return {
            "passed": True,
            "rejected": True,
            "reason_code": exc.receipt.reason_code,
            "active_digest_unchanged": manager.current()["active_digest"],
        }
    raise RehearsalError("same-sequence different-digest activation was accepted")


async def _exercise_answers(guidance: str) -> Dict[str, Any]:
    original_grounding = settings.rag_grounding_required
    original_emergency = settings.emergency_rag_enabled
    settings.rag_grounding_required = True
    settings.emergency_rag_enabled = True
    try:
        router = MessageRouter()
        router.http_client = None
        router.chat_store = MagicMock()
        router.chat_store.get_history = AsyncMock(return_value=[])
        router.chat_store.add_turn = AsyncMock()
        router.chat_store.get_hunt_state = AsyncMock(return_value=0)
        router.chat_store.set_hunt_state = AsyncMock()
        router.send_response = AsyncMock(return_value=True)
        router.route_to_llm = AsyncMock(return_value="must-not-run")
        router.route_to_rag = AsyncMock(return_value=(guidance, 0.99, guidance))
        grounded = await router.process_message(ChannelMessage(
            sender="sms:synthetic-resident", receiver="sms:lil-evy",
            content="Emergency: which evacuation route is current?", channel=MessageChannel.SMS,
        ))
        grounded_mode = "rag_direct" if router.route_to_llm.await_count == 0 else "unexpected_llm"

        router.route_to_rag = AsyncMock(return_value=(None, 0.0, None))
        refused = await router.process_message(ChannelMessage(
            sender="sms:synthetic-resident", receiver="sms:lil-evy",
            content="Medical emergency: where can I obtain insulin?", channel=MessageChannel.SMS,
        ))
        return {
            "grounded": {"passed": grounded == guidance, "mode": grounded_mode},
            "no_answer_refusal": {
                "passed": refused == settings.emergency_grounding_failure_message,
                "mode": "refused_emergency_grounding",
            },
        }
    finally:
        settings.rag_grounding_required = original_grounding
        settings.emergency_rag_enabled = original_emergency


def run_rehearsal(output: Path) -> Dict[str, Any]:
    """Execute the complete software rehearsal and write its evidence summary."""

    output = Path(output)
    if output.exists():
        raise FileExistsError(f"immutable rehearsal output already exists: {output}")
    output.mkdir(parents=True)
    evidence = output / "evidence"
    evidence.mkdir()
    policy, sources, documents, cases = (
        _load("policy.json"), _load("sources.json"),
        _load("canonical_documents.json"), _load("evaluation_cases.json"),
    )
    suitability = evaluate_suitability(
        policy, sources, documents, cases, as_of=AS_OF, time_confidence="trusted"
    )
    if suitability["decision"] != "PASS":
        raise RehearsalError("synthetic corpus suitability did not pass")
    _write_json(evidence / "suitability-report.json", suitability)

    mission, registry, coverage, lineage = _governance_records(policy, sources, documents)
    for name, value in (
        ("mission-profile.json", mission), ("source-registry.json", registry),
        ("coverage-report.json", coverage), ("lineage-manifest.json", lineage),
    ):
        _write_json(evidence / name, value)
    qualifying = {
        document_id
        for requirement in suitability["requirements"]
        for document_id in requirement["qualifying_document_ids"]
    }
    package_documents = [{
        "id": document["document_id"], "text": document["canonical_text"],
        "metadata": {
            "category": next(
                item["requirement_id"].removeprefix("coverage-")
                for item in suitability["requirements"]
                if document["document_id"] in item["qualifying_document_ids"]
            ),
            "safety_class": "critical",
        },
    } for document in documents if document["document_id"] in qualifying]
    input_documents = _write_json(evidence / "package-documents.json", package_documents)
    package = build_package(Namespace(
        contract_profile="governed-v1", event_id=EVENT_ID,
        event_name="Synthetic Region 4 Flood Response", version=VERSION,
        output_dir=str(output / "candidate"), created_at=AS_OF, signing_key=None,
        input_documents=str(input_documents), mission_profile=str(evidence / "mission-profile.json"),
        source_registry=str(evidence / "source-registry.json"),
        coverage_report=str(evidence / "coverage-report.json"),
        lineage_manifest=str(evidence / "lineage-manifest.json"),
    ))
    manifest_path = package / "manifest.json"
    release_digest = _sha256_bytes(manifest_path.read_bytes())

    release, retrieval, answer, edge = _promotion_evidence(
        release_digest, json.loads(manifest_path.read_text())["governance"], coverage, suitability
    )
    promotion = evaluate_promotion(
        release, retrieval, answer, edge, suitability, coverage,
        contract_profile="governed-v1",
    )
    if promotion["decision"] != "PASS":
        raise RehearsalError("governed promotion did not pass")
    _write_json(evidence / "promotion-report.json", promotion)

    attestation_key = Ed25519PrivateKey.generate()
    approvals = [
        {"identity": "evaluation.owner@example.test", "role": "evaluation_owner",
         "organization": "Synthetic Evaluation Lab", "independence_group": "evaluation",
         "decision": "approve", "approved_at": "2026-10-06T18:05:00Z"},
        {"identity": "release.approver@example.test", "role": "release_approver",
         "organization": "Synthetic Release Board", "independence_group": "release",
         "decision": "approve", "approved_at": "2026-10-06T18:06:00Z"},
    ]
    statement = prepare_attestation_statement(
        promotion, approvals, evaluation_executor_identity="urn:evy:synthetic-evaluator",
        valid_from="2026-10-06T18:00:00Z", expires_at="2026-10-07T18:00:00Z",
        revocation_generation=1, revocation_checked_at="2026-10-06T18:06:00Z",
        signer_identity="urn:evy:synthetic-attestation-signer",
        signed_at="2026-10-06T18:07:00Z", public_key=attestation_key.public_key(),
        key_valid_from="2026-10-01T00:00:00Z", key_expires_at="2027-01-01T00:00:00Z",
    )
    attestation = finalize_attestation(
        statement, attestation_key.sign(signing_payload(statement)), attestation_key.public_key()
    )
    authorization = authorize_release_signing(
        manifest_path.read_bytes(), promotion, attestation, attestation_key.public_key(),
        as_of="2026-10-06T18:08:00Z",
    )
    _write_json(evidence / "evaluation-attestation.json", attestation)
    _write_json(evidence / "release-signing-authorization.json", authorization)

    release_key = Ed25519PrivateKey.generate()
    release_public_key = output / "release-public-key.pem"
    release_public_key.write_bytes(release_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    release_signature = release_key.sign(manifest_path.read_bytes())
    (package / "manifest.sig").write_text(base64.b64encode(release_signature).decode("ascii") + "\n")
    signature_bundle = _write_json(evidence / "signature-bundle.json", {
        "release_digest": release_digest,
        "release_signature": base64.b64encode(release_signature).decode("ascii"),
        "authorization": authorization,
        "evaluation_attestation": attestation,
    })
    signature_failure = _exercise_signature_failure(package, release_public_key)

    recovery_package = build_package(Namespace(
        contract_profile="governed-v1", event_id=EVENT_ID,
        event_name="Synthetic Region 4 Flood Response", version=VERSION,
        output_dir=str(output / "recovery-candidate"),
        created_at="2026-10-06T17:00:00Z", signing_key=None,
        input_documents=str(input_documents), mission_profile=str(evidence / "mission-profile.json"),
        source_registry=str(evidence / "source-registry.json"),
        coverage_report=str(evidence / "coverage-report.json"),
        lineage_manifest=str(evidence / "lineage-manifest.json"),
    ))
    recovery_manifest_path = recovery_package / "manifest.json"
    recovery_digest = _sha256_bytes(recovery_manifest_path.read_bytes())
    recovery_signature = release_key.sign(recovery_manifest_path.read_bytes())
    (recovery_package / "manifest.sig").write_text(
        base64.b64encode(recovery_signature).decode("ascii") + "\n",
        encoding="ascii",
    )

    corpus_tar = output / "corpus-release.tar"
    _tar_package(package, corpus_tar)
    application = _write_json(evidence / "application.json", {
        "name": "lil-evy", "version": "software-rehearsal", "simulated": True,
    })
    model = _write_json(evidence / "model.json", {
        "mode": "rag-only", "digest": promotion["binding"]["model_digest"], "simulated": True,
    })
    activation_request = _write_json(evidence / "activation-request.json", {
        "event_id": EVENT_ID, "site_id": SITE_ID, "digest": release_digest, "sequence": SEQUENCE,
    })
    artifacts = {
        "application": application, "model": model, "corpus": corpus_tar,
        "signature_bundle": signature_bundle, "activation_request": activation_request,
    }
    transfer_key = Ed25519PrivateKey.generate()
    site_key = X25519PrivateKey.generate()
    transfer = export_encrypted_transfer_set(
        output / "encrypted-transfer", artifacts, media_id="synthetic-disaster-media-001",
        event_id=EVENT_ID, site_id=SITE_ID, sequence=SEQUENCE,
        classification="restricted",
        recipient=EncryptionRecipient(SITE_ID, "site-r4-north-2026", site_key.public_key()),
        manifest_signer=transfer_key.sign,
    )
    verified = verify_transfer_set(
        transfer, expected_event_id=EVENT_ID, expected_site_id=SITE_ID,
        allowed_classifications={"restricted"}, sequence_floor=0,
        signature_verifier=lambda payload, signature: _verify_signature(
            transfer_key.public_key(), payload, signature
        ),
    )
    transfer_failures = _exercise_transfer_failures(
        transfer, transfer_key.public_key()
    )
    decrypted = decrypt_transfer_set(
        verified, output / "offline-staging", site_id=SITE_ID,
        recipient_key_id="site-r4-north-2026", private_key=site_key,
    )
    intake_package = output / "activation-intake" / "package"
    _extract_package(decrypted / "artifacts" / "corpus", intake_package)

    receipt_key = Ed25519PrivateKey.generate()
    receipt_key_path = output / "receipt-private-key.pem"
    receipt_key_path.write_bytes(receipt_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    receipt_signer = Ed25519ReceiptSigner(
        receipt_key_path, key_id="synthetic-node-receipts"
    )
    receipt_key_path.unlink()
    manager = CorpusActivationManager(
        output / "activation", node_id="lil-evy-synthetic-001",
        cluster_id="synthetic-cluster-001", site_id=SITE_ID,
        trusted_public_key_path=release_public_key, expected_event_id=EVENT_ID,
        expected_version=VERSION, policy_digest=event_policy_subject_digest(policy),
        runtime_identity={
            "version": "software-rehearsal", "chunker_digest": promotion["binding"]["chunker_digest"],
            "model_digest": promotion["binding"]["model_digest"],
            "embedding_model_digest": promotion["binding"]["embedding_model_digest"],
        }, time_confidence="trusted",
        receipt_signer=receipt_signer,
    )
    receipt = manager.activate(ReleaseCandidate(release_digest, SEQUENCE, intake_package))
    restarted = CorpusActivationManager(
        output / "activation", node_id="lil-evy-synthetic-001",
        cluster_id="synthetic-cluster-001", site_id=SITE_ID,
        trusted_public_key_path=release_public_key, expected_event_id=EVENT_ID,
        expected_version=VERSION, policy_digest=event_policy_subject_digest(policy),
        runtime_identity=manager.runtime_identity, time_confidence="trusted",
        receipt_signer=manager.receipt_signer,
    )
    active_after_restart = restarted.current()
    _write_json(evidence / "activation-receipt.json", receipt.to_dict())

    activation_rejection = _exercise_activation_rejection(restarted, intake_package)
    recovery_key = Ed25519PrivateKey.generate()
    recovery_public_key = output / "recovery-public-key.pem"
    recovery_public_key.write_bytes(recovery_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    recovery_authorization = {
        "schema_version": "1.0.0",
        "record_type": "recovery_authorization",
        "authorization_id": "00000000-0000-4000-8000-000000000006",
        "nonce": "c3ludGhldGljLXJlY292ZXJ5LTAwMDY",
        "event_id": EVENT_ID,
        "site_id": SITE_ID,
        "target": {"digest": recovery_digest, "sequence": SEQUENCE - 1},
        "current": {"digest": release_digest, "sequence_floor": SEQUENCE},
        "reason_code": "synthetic-drill-recovery",
        "incident_reference": "SYNTHETIC-DRILL-RECOVERY-0006",
        "issuer": {"issuer_id": "synthetic-recovery-authority", "trust_generation": 1},
        "approvers": [
            {
                "approver_id": "synthetic-incident-commander",
                "role": "incident-commander",
                "independence_group": "field-operations",
            },
            {
                "approver_id": "synthetic-safety-reviewer",
                "role": "safety-reviewer",
                "independence_group": "safety-office",
            },
        ],
        "validity": {
            "not_before": "2026-10-06T18:00:00Z",
            "expires_at": "2026-10-06T20:00:00Z",
            "maximum_offline_seconds": 7200,
            "allow_anchored_time": True,
        },
        "policy": {"serving_allowed": True, "blocked_safety_classes": []},
    }
    authorization_payload = json.dumps(
        recovery_authorization, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    recovery_authorization["signature"] = {
        "key_id": "synthetic-recovery-key-2026",
        "algorithm": "Ed25519",
        "value": base64.b64encode(recovery_key.sign(authorization_payload)).decode("ascii"),
    }
    _write_json(evidence / "recovery-authorization.json", recovery_authorization)
    verified_recovery = verify_recovery_authorization(
        recovery_authorization,
        expected_event_id=EVENT_ID,
        expected_site_id=SITE_ID,
        expected_target_digest=recovery_digest,
        expected_target_sequence=SEQUENCE - 1,
        current_digest=release_digest,
        sequence_floor=SEQUENCE,
        trusted_key_id="synthetic-recovery-key-2026",
        trusted_public_key_path=recovery_public_key,
        trust_generation=1,
        used_replay_markers=restarted.current()["used_recovery_authorizations"],
        time_confidence="trusted",
        now=datetime(2026, 10, 6, 18, 10, tzinfo=timezone.utc),
    )
    recovery_receipt = restarted.recover_exceptionally(
        ReleaseCandidate(recovery_digest, SEQUENCE - 1, recovery_package),
        verified_recovery,
    )
    _write_json(evidence / "recovery-activation-receipt.json", recovery_receipt.to_dict())
    recovery_restart = CorpusActivationManager(
        output / "activation", node_id="lil-evy-synthetic-001",
        cluster_id="synthetic-cluster-001", site_id=SITE_ID,
        trusted_public_key_path=release_public_key, expected_event_id=EVENT_ID,
        expected_version=VERSION, policy_digest=event_policy_subject_digest(policy),
        runtime_identity=manager.runtime_identity, time_confidence="trusted",
        receipt_signer=manager.receipt_signer,
    ).current()
    authorized_recovery = {
        "passed": (
            recovery_receipt.result == "recovered"
            and recovery_restart["active_digest"] == recovery_digest
            and recovery_restart["active_sequence"] == SEQUENCE - 1
            and recovery_restart["sequence_floor"] == SEQUENCE
            and recovery_restart["mode"] == "recovery"
        ),
        "result": recovery_receipt.result,
        "authorization_id": verified_recovery.authorization_id,
        "authorization_time_basis": verified_recovery.time_basis,
        "target_digest": recovery_digest,
        "active_sequence": recovery_restart["active_sequence"],
        "sequence_floor": recovery_restart["sequence_floor"],
        "mode": recovery_restart["mode"],
        "restart_persistence": (
            "PASS" if recovery_restart["active_digest"] == recovery_digest else "FAIL"
        ),
    }
    if not authorized_recovery["passed"]:
        raise RehearsalError("authorized recovery did not persist its exact target")

    current_document = next(
        document for document in documents if document["document_id"] == "doc-evacuation-r4-current"
    )
    answer_behavior = asyncio.run(_exercise_answers(current_document["canonical_text"]))
    if not all(item["passed"] for item in answer_behavior.values()):
        raise RehearsalError("grounded/refusal behavior did not pass")

    summary = {
        "schema_version": "1.0.0",
        "record_type": "synthetic_disaster_rehearsal_summary",
        "scenario": {"event_id": EVENT_ID, "site_id": SITE_ID, "sequence": SEQUENCE},
        "decision": "PASS",
        "software_evidence": {
            "evidence_class": "SIMULATED_SOFTWARE",
            "coverage": coverage["decision"], "suitability": suitability["decision"],
            "promotion": promotion["decision"],
            "candidate_release_digest": release_digest,
            "evaluation_attestation_id": attestation["attestation_id"],
            "signing_authorization_id": authorization["authorization_id"],
            "encrypted_transfer_manifest_digest": verified.manifest_digest,
            "offline_decryption": "PASS", "activation": receipt.result,
            "restart_persistence": (
                "PASS" if active_after_restart["active_digest"] == release_digest else "FAIL"
            ),
            "answer_behavior": answer_behavior,
            "negative_scenarios": {
                "release_signature_failure": signature_failure,
                **transfer_failures,
                "activation_rejection": activation_rejection,
                "authorized_recovery": authorized_recovery,
            },
        },
        "hardware_cut": {
            "status": "NOT_RUN",
            "evidence_class": "NO_HARDWARE_EVIDENCE",
            "unproven": [
                "physical GSM/SMS modem", "physical LoRa radio", "antenna/range",
                "battery/solar runtime", "thermal behavior", "power-loss behavior on target hardware",
            ],
        },
        "artifacts": {
            "candidate_package": str(package.relative_to(output)),
            "recovery_candidate_package": str(recovery_package.relative_to(output)),
            "evidence": str(evidence.relative_to(output)),
            "encrypted_transfer": str(transfer.relative_to(output)),
            "offline_staging": str(decrypted.relative_to(output)),
            "activation_state": "activation/current.json",
        },
    }
    summary["summary_digest"] = object_digest(summary)
    _write_json(output / "rehearsal-summary.json", summary)
    return summary


def _verify_signature(public_key: Any, payload: bytes, signature: bytes) -> bool:
    try:
        public_key.verify(signature, payload)
        return True
    except Exception:
        return False

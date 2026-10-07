from copy import deepcopy

from scripts.openshift_edd_preflight import (
    CORE_COMPONENTS,
    EXPECTED_COMPONENTS,
    evaluate_snapshot,
)


def _expected():
    return {
        "release": "lil-evy",
        "profile": "lab-small",
        "embedding_model": "all-MiniLM-L6-v2",
        "generation_enabled": "true",
        "llm_provider": "bitnet",
        "llm_model": "bitnet-2b4t",
        "corpus_digest": "sha256:" + "a" * 64,
        "corpus_manifest_digest": "sha256:" + "c" * 64,
        "corpus_mode": "activation",
        "event_id": "summit-connect",
        "version": "2026.1",
        "channel": "simulator",
    }


def _snapshot():
    expected = _expected()
    return {
        "deployments": [
            {
                "name": "lil-evy-{0}".format(component),
                "desired": 1,
                "ready": 1,
            }
            for component in EXPECTED_COMPONENTS
        ],
        "config": {
            "EDGE_RESOURCE_PROFILE": expected["profile"],
            "EMBEDDING_MODEL": expected["embedding_model"],
            "GENERATION_ENABLED": expected["generation_enabled"],
            "LLM_PROVIDER": expected["llm_provider"],
            "LLM_MODEL": expected["llm_model"],
            "RAG_GROUNDING_REQUIRED": "true",
            "EMERGENCY_RAG_ENABLED": "true",
            "SMS_MODE": "sim",
            "CORPUS_MODE": "activation",
            "CORPUS_EVENT_ID": expected["event_id"],
            "CORPUS_VERSION": expected["version"],
            "CORPUS_REQUIRE_SIGNATURE": "true",
        },
        "rag_stats": {
            "activation": {
                "ready": True,
                "active_digest": expected["corpus_digest"],
            },
            "embedding_service": {
                "model_name": expected["embedding_model"],
                "model_source": "baked",
            },
        },
        "llm_health": {
            "details": {
                "provider": expected["llm_provider"],
                "model_name": expected["llm_model"],
                "provider_reachable": True,
            }
        },
        "service_health": {
            component: {"status": "healthy"} for component in EXPECTED_COMPONENTS
        },
    }


def test_matching_ready_snapshot_is_green():
    assert evaluate_snapshot(_snapshot(), _expected()) == ("GREEN", [])


def test_missing_workload_is_red():
    snapshot = _snapshot()
    snapshot["deployments"].pop()
    status, reasons = evaluate_snapshot(snapshot, _expected())
    assert status == "RED"
    assert any("missing deployment" in reason for reason in reasons)


def test_identity_drift_is_red():
    snapshot = deepcopy(_snapshot())
    snapshot["rag_stats"]["activation"]["active_digest"] = "sha256:" + "b" * 64
    snapshot["llm_health"]["details"]["model_name"] = "unexpected-model"
    status, reasons = evaluate_snapshot(snapshot, _expected())
    assert status == "RED"
    assert "active corpus digest does not match CORPUS_DIGEST" in reasons
    assert "live LLM model does not match LLM_MODEL" in reasons


def test_field_safety_must_be_enabled():
    snapshot = deepcopy(_snapshot())
    snapshot["config"]["RAG_GROUNDING_REQUIRED"] = "false"
    snapshot["config"]["EMERGENCY_RAG_ENABLED"] = "false"
    status, reasons = evaluate_snapshot(snapshot, _expected())
    assert status == "RED"
    assert "RAG_GROUNDING_REQUIRED is not true" in reasons
    assert "EMERGENCY_RAG_ENABLED is not true" in reasons


def test_rag_only_snapshot_is_green_without_generation_workloads():
    expected = _expected()
    expected.update(
        {
            "profile": "field-rag-only",
            "generation_enabled": "false",
            "llm_provider": "",
            "llm_model": "",
        }
    )
    snapshot = _snapshot()
    snapshot["deployments"] = [
        item
        for item in snapshot["deployments"]
        if not item["name"].endswith(("-bitnet", "-llm-inference"))
    ]
    assert len(snapshot["deployments"]) == len(CORE_COMPONENTS)
    snapshot["config"].update(
        {
            "EDGE_RESOURCE_PROFILE": "field-rag-only",
            "GENERATION_ENABLED": "false",
        }
    )
    snapshot.pop("llm_health")
    snapshot["service_health"].pop("llm-inference")

    assert evaluate_snapshot(snapshot, expected) == ("GREEN", [])


def test_rag_only_snapshot_rejects_idle_generation_workloads():
    expected = _expected()
    expected["generation_enabled"] = "false"
    snapshot = _snapshot()
    snapshot["config"]["GENERATION_ENABLED"] = "false"
    snapshot["service_health"].pop("llm-inference")

    status, reasons = evaluate_snapshot(snapshot, expected)

    assert status == "RED"
    assert any("still deploys" in reason for reason in reasons)


def test_generation_identity_must_be_boolean():
    expected = _expected()
    expected["generation_enabled"] = "sometimes"

    status, reasons = evaluate_snapshot(_snapshot(), expected)

    assert status == "RED"
    assert "GENERATION_ENABLED declaration must be true or false" in reasons


def test_embedding_model_must_be_baked_for_disconnected_startup():
    snapshot = deepcopy(_snapshot())
    snapshot["rag_stats"]["embedding_service"]["model_source"] = "download"

    status, reasons = evaluate_snapshot(snapshot, _expected())

    assert status == "RED"
    assert "embedding model is not baked into the deployed RAG image" in reasons


def test_signed_packaged_corpus_can_qualify_lab_edd():
    expected = _expected()
    expected["corpus_mode"] = "packaged"
    snapshot = _snapshot()
    snapshot["config"]["CORPUS_MODE"] = "packaged"
    snapshot["rag_stats"]["activation"] = {"status": "not_configured"}
    snapshot["rag_stats"]["corpus"] = {
        "event": {"id": expected["event_id"]},
        "corpus": {"version": expected["version"]},
    }
    snapshot["rag_stats"]["corpus_identity"] = {
        "digest": expected["corpus_manifest_digest"],
        "sequence": None,
    }
    snapshot["resolved_images"] = [
        {
            "name": "install-corpus",
            "image_id": "quay.io/example/corpus@{0}".format(expected["corpus_digest"]),
        }
    ]

    assert evaluate_snapshot(snapshot, expected) == ("GREEN", [])


def test_packaged_corpus_manifest_drift_is_red():
    expected = _expected()
    expected["corpus_mode"] = "packaged"
    snapshot = _snapshot()
    snapshot["config"]["CORPUS_MODE"] = "packaged"
    snapshot["rag_stats"]["corpus"] = {
        "event": {"id": expected["event_id"]},
        "corpus": {"version": expected["version"]},
    }
    snapshot["rag_stats"]["corpus_identity"] = {
        "digest": "sha256:" + "d" * 64,
        "sequence": None,
    }
    snapshot["resolved_images"] = [
        {
            "name": "install-corpus",
            "image_id": "quay.io/example/corpus@{0}".format(expected["corpus_digest"]),
        }
    ]

    status, reasons = evaluate_snapshot(snapshot, expected)

    assert status == "RED"
    assert "live corpus manifest does not match CORPUS_MANIFEST_DIGEST" in reasons

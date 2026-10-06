from copy import deepcopy

from scripts.openshift_edd_preflight import EXPECTED_COMPONENTS, evaluate_snapshot


def _expected():
    return {
        "release": "lil-evy",
        "profile": "lab-small",
        "embedding_model": "all-MiniLM-L6-v2",
        "llm_provider": "bitnet",
        "llm_model": "bitnet-2b4t",
        "corpus_digest": "sha256:" + "a" * 64,
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
            "LLM_PROVIDER": expected["llm_provider"],
            "LLM_MODEL": expected["llm_model"],
            "RAG_GROUNDING_REQUIRED": "true",
            "EMERGENCY_RAG_ENABLED": "true",
            "SMS_MODE": "sim",
        },
        "rag_stats": {
            "activation": {
                "ready": True,
                "active_digest": expected["corpus_digest"],
            },
            "embedding_service": {"model_name": expected["embedding_model"]},
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

from scripts.run_convergence import MATRIX_PATH, _command_environment, _stage_status
from tests.benchmarks.test_capacity import LIVE_ROUTE_PATH, _live_message_payload

import yaml


def test_failed_required_command_is_red():
    status, reasons = _stage_status(
        [{"exit_code": 1, "skipped": 0, "command": ["false"]}], None
    )
    assert status == "RED"
    assert reasons


def test_skipped_check_is_amber():
    status, reasons = _stage_status(
        [{"exit_code": 0, "skipped": 1, "command": ["pytest"]}], None
    )
    assert status == "AMBER"
    assert "1 checks skipped" in reasons


def test_scope_cap_prevents_false_green():
    status, reasons = _stage_status(
        [{"exit_code": 0, "skipped": 0, "command": ["pytest"]}], "AMBER"
    )
    assert status == "AMBER"
    assert reasons


def test_live_capacity_uses_the_channel_neutral_gateway_contract():
    assert LIVE_ROUTE_PATH == "/router/route"
    assert _live_message_payload("Where is water?", 7) == {
        "sender": "simulator:capacity-7",
        "receiver": "simulator:lil-evy",
        "content": "Where is water?",
        "channel": "simulator",
    }


def test_command_environment_maps_runtime_urls(monkeypatch):
    monkeypatch.setenv("EDGE_API_URL", "https://edge.example.test")
    assert _command_environment(
        {
            "env": {"CAPACITY_LIVE": "1"},
            "env_from": {"EVAL_API_URL": "EDGE_API_URL"},
        }
    ) == {
        "CAPACITY_LIVE": "1",
        "EVAL_API_URL": "https://edge.example.test",
    }


def test_matrix_has_ordered_gates_and_executable_profiles():
    matrix = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))
    stages = list(matrix["stages"].values())
    assert [stage["id"] for stage in stages] == [
        "CDD",
        "TDD",
        "INT",
        "EDD",
        "BDD",
        "CUT",
        "PUB",
    ]
    for stage in stages:
        assert stage["green_requires"]
        assert stage["execution"]["local"]["commands"]

    edd_openshift = stages[3]["execution"]["openshift"]
    assert set(edd_openshift["required_env"]) == {
        "EDGE_NAMESPACE",
        "EDGE_RELEASE",
        "EDGE_API_URL",
        "CORPUS_DIGEST",
        "EMBEDDING_MODEL",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "EDGE_RESOURCE_PROFILE",
        "CHANNEL_DRIVER",
    }

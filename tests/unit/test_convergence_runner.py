from scripts.run_convergence import MATRIX_PATH, _stage_status

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
        "CORPUS_RELEASE_ID",
        "EMBEDDING_MODEL",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "EDGE_RESOURCE_PROFILE",
        "CHANNEL_DRIVER",
    }

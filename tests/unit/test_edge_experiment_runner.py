"""Controlled experiment helpers keep journeys and counters attributable."""

from tests.benchmarks.run_edge_experiment import (
    HUNT_HINT_SENDER,
    HUNT_LOCKED_SENDER,
    HUNT_SENDER,
    counter_delta,
    experiment_sender,
    resolve_scenario,
)


def test_stateful_hunt_queries_use_deliberate_journey_senders() -> None:
    assert experiment_sender("hunt_start", 1) == HUNT_SENDER
    assert experiment_sender("hunt_clue7_answer", 2) == HUNT_SENDER
    assert experiment_sender("hunt_hint", 3) == HUNT_HINT_SENDER
    assert experiment_sender("hunt_clue_locked", 4) == HUNT_LOCKED_SENDER
    assert experiment_sender("venue_01", 5) == "+15560000005"


def test_counter_delta_is_bounded_against_service_restart() -> None:
    assert counter_delta({"requests_total": 9}, {"requests_total": 4}, "requests_total") == 5
    assert counter_delta({"requests_total": 1}, {"requests_total": 4}, "requests_total") == 0


def test_model_experiment_requires_a_positive_request_delta() -> None:
    before = {"requests_total": 8}
    assert counter_delta({"requests_total": 9}, before, "requests_total") > 0
    assert not (counter_delta({"requests_total": 8}, before, "requests_total") > 0)


def test_generation_control_uses_a_dedicated_model_eval_set() -> None:
    _, scenario = resolve_scenario(
        "tests/benchmarks/experiment_matrix.yaml", "bitnet-generation-control"
    )

    assert scenario["eval_queries"] == "tests/evaluation/model_eval_queries.yaml"
    assert scenario["quality_gate"] == 0.80

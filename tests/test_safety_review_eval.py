"""Acceptance check for the fixed Phase 1 evaluation dataset."""

from evals.evaluate_safety_review import evaluate, load_dataset


def test_phase1_eval_has_ten_cases_and_meets_hard_gates() -> None:
    assert len(load_dataset()) == 10
    result = evaluate()
    assert result["total"] == 10
    assert result["passed"] == 10
    assert result["failed"] == []
    for metric in (
        "SDS Match Accuracy",
        "Citation Coverage",
        "Evidence-insufficient Blocking Compliance",
        "JSA Structure Completeness",
        "Human Confirmation Compliance",
        "No-Permit-Before-Phase-2 Compliance",
    ):
        assert result["metrics"][metric] == 1.0

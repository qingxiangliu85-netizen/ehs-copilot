"""Tests for the Phase 3 evaluation harness.

Three layers are covered:

1. the dataset itself (structure, coverage, no duplicate ids),
2. the metric arithmetic (pure functions, asserted on synthetic observations),
3. the harness end-to-end on a small subset that needs no embedding model.

The full 30-scenario run is *not* replayed here — that is the job of
``python evals/evaluate.py``.  Instead the last test class checks that the
checked-in ``results.json`` is consistent with the dataset, so the numbers
reported in the README can never drift away from the scenarios they describe.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evals import evaluate as ev  # noqa: E402


REQUIRED_CATEGORIES = {
    "sds_query",
    "jsa_risk",
    "hazard_write",
    "dashboard",
    "combined",
    "emergency",
    "evidence_gap",
    "chemical_mismatch",
}


class _FakeKnowledgeBase:
    """Stand-in for the real index; the hazard path never touches it."""

    vector_store = None
    file_names = ("EHS_Copilot_Demo_Synthetic_SDS.pdf",)


# --------------------------------------------------------------------------- #
# 1. Dataset
# --------------------------------------------------------------------------- #


class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = ev.load_dataset()

    def test_the_dataset_has_a_realistic_number_of_scenarios(self) -> None:
        self.assertGreaterEqual(len(self.cases), 25)
        self.assertLessEqual(len(self.cases), 30)

    def test_every_scenario_passes_the_structural_validator(self) -> None:
        self.assertEqual(ev.validate_dataset(self.cases), [])

    def test_ids_are_unique(self) -> None:
        ids = [str(case["id"]) for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_required_category_is_covered(self) -> None:
        categories = {str(case["category"]) for case in self.cases}
        self.assertTrue(
            REQUIRED_CATEGORIES <= categories,
            f"缺少场景类别：{sorted(REQUIRED_CATEGORIES - categories)}",
        )

    def test_the_approval_scenarios_cover_all_three_decisions(self) -> None:
        actions = {
            str((case["expect"].get("decision") or {}).get("action") or "")
            for case in self.cases
        }
        for action in ("approve", "modify", "reject"):
            self.assertIn(action, actions, f"数据集未覆盖 {action} 决策")
        self.assertIn(
            True,
            [case["expect"].get("decision") is None and case["expect"]["pauses"]
             for case in self.cases],
            "数据集未覆盖「未给出任何决策」的场景",
        )

    def test_the_validator_catches_a_broken_record(self) -> None:
        broken = [
            {
                "id": "x-01",
                "category": "sds_query",
                "input": "",
                "expect": {"status": "finished"},
                "_line": 1,
            }
        ]
        problems = ev.validate_dataset(broken)
        self.assertTrue(any("缺少 input" in item for item in problems))
        self.assertTrue(any("status 取值非法" in item for item in problems))


# --------------------------------------------------------------------------- #
# 2. Metric arithmetic
# --------------------------------------------------------------------------- #


def _result(
    identifier: str,
    *,
    routing: bool = True,
    tools: bool = True,
    completion: bool = True,
    approval: tuple = (("check", True, ""),),
    citation: tuple | None = None,
) -> dict:
    return {
        "id": identifier,
        "category": "sds_query",
        "input": identifier,
        "routing_ok": routing,
        "routing_detail": "" if routing else "任务类型不符",
        "tools_ok": tools,
        "tools_detail": "" if tools else "工具序列不符",
        "completion_ok": completion,
        "completion_detail": "" if completion else "结束状态不符",
        "approval_checks": list(approval),
        "citation": citation,
        "status": "completed",
        "paused": False,
        "elapsed_ms": 1,
    }


class MetricTests(unittest.TestCase):
    def test_summarise_computes_every_metric_from_the_observations(self) -> None:
        cases = [
            {"id": "a", "expect": {"sds_outcome": "cited"}},
            {"id": "b", "expect": {"sds_outcome": "human_confirmed"}},
            {"id": "c", "expect": {"sds_outcome": "withheld"}},
        ]
        results = [
            _result("a", citation=("delivered", True, "")),
            _result("b", tools=False, citation=("delivered", True, "")),
            _result("c", citation=("withheld", False, "仍交付了结论")),
        ]

        metrics = ev.summarise(cases, results)["metrics"]

        self.assertEqual(metrics["routing_accuracy"]["value"], 100.0)
        self.assertEqual(metrics["tool_call_accuracy"], {
            "value": 66.7, "passed": 2, "total": 3,
        })
        self.assertEqual(metrics["workflow_completion"]["value"], 100.0)
        self.assertEqual(metrics["approval_compliance"]["passed"], 3)

        # Only "a" delivered a fully traceable answer; "b" was a human-confirmed
        # exception, so it sits in the denominator without adding to the numerator.
        coverage = metrics["citation_coverage"]
        self.assertEqual(coverage["total"], 2)
        self.assertEqual(coverage["passed"], 1)
        self.assertEqual(coverage["value"], 50.0)
        self.assertEqual(coverage["withheld_total"], 1)
        self.assertEqual(coverage["withheld_correctly"], 0)

    def test_failures_are_reported_with_a_reason(self) -> None:
        cases = [
            {"id": "a", "expect": {"sds_outcome": "cited"}},
            {"id": "b", "expect": {"sds_outcome": None}},
        ]
        results = [
            _result("a", citation=("delivered", False, "缺页码")),
            _result("b", approval=(("写操作未经审批", False, "执行了 create_hazard"),)),
        ]
        report = ev.summarise(cases, results)
        failed = {item["id"] for item in report["failures"]}
        self.assertEqual(failed, {"a", "b"})
        reasons = {item["id"]: item["reasons"] for item in report["failures"]}
        self.assertTrue(any("缺页码" in text for text in reasons["a"]))
        self.assertTrue(any("create_hazard" in text for text in reasons["b"]))

    def test_routing_and_tool_checks_are_exact(self) -> None:
        expect = {"tasks": ["sds_query"], "planned": ["search_sds"],
                  "executed": ["search_sds"]}
        good = {"tasks": ["sds_query"], "planned": ["search_sds"],
                "executed": ["search_sds"]}
        self.assertEqual(ev._check_routing(expect, good), (True, ""))
        self.assertEqual(ev._check_tools(expect, good), (True, ""))

        extra = dict(good, tasks=["sds_query", "jsa_risk"])
        self.assertFalse(ev._check_routing(expect, extra)[0])

        suppressed = dict(good, executed=[])
        ok, detail = ev._check_tools(expect, suppressed)
        self.assertFalse(ok)
        self.assertIn("实际执行", detail)

    def test_citation_check_has_three_buckets(self) -> None:
        delivered = {"sds_delivered": True, "citations_ok": True, "human_confirmed": False}
        self.assertEqual(
            ev.citation_check({"sds_outcome": "cited"}, delivered)[0], "delivered"
        )
        self.assertTrue(ev.citation_check({"sds_outcome": "cited"}, delivered)[1])

        untraceable = {"sds_delivered": True, "citations_ok": False, "human_confirmed": False}
        self.assertFalse(ev.citation_check({"sds_outcome": "cited"}, untraceable)[1])
        # …but a bare gap without the human record must not satisfy the
        # "human_confirmed" expectation either.
        self.assertFalse(
            ev.citation_check({"sds_outcome": "human_confirmed"}, untraceable)[1]
        )
        confirmed = {"sds_delivered": True, "citations_ok": False, "human_confirmed": True}
        self.assertTrue(
            ev.citation_check({"sds_outcome": "human_confirmed"}, confirmed)[1]
        )

        withheld = {"sds_delivered": False, "citations_ok": False, "human_confirmed": False}
        bucket, ok, _ = ev.citation_check({"sds_outcome": "withheld"}, withheld)
        self.assertEqual(bucket, "withheld")
        self.assertTrue(ok)

        self.assertIsNone(ev.citation_check({"sds_outcome": None}, delivered))

    def test_the_automated_path_coverage_is_reported_separately(self) -> None:
        cases = [
            {"id": "a", "expect": {"sds_outcome": "cited"}},
            {"id": "b", "expect": {"sds_outcome": "cited"}},
            {"id": "c", "expect": {"sds_outcome": "human_confirmed"}},
            {"id": "d", "expect": {"sds_outcome": "withheld"}},
        ]
        results = [
            _result("a", citation=("delivered", True, "")),
            _result("b", citation=("delivered", True, "")),
            _result("c", citation=("delivered", True, "")),
            _result("d", citation=("withheld", True, "")),
        ]
        metrics = ev.summarise(cases, results)["metrics"]

        # The headline keeps every delivery in the denominator, so the human
        # exception pulls it below the automated path.
        headline = metrics["citation_coverage"]
        self.assertEqual(headline["total"], 3)
        self.assertEqual(headline["passed"], 2)
        self.assertEqual(headline["value"], 66.7)

        # …and the automated path is reported on its own, without redefining the
        # headline number.
        automated = metrics["citation_coverage_automated_path"]
        self.assertEqual(automated["total"], 2)
        self.assertEqual(automated["passed"], 2)
        self.assertEqual(automated["value"], 100.0)
        self.assertEqual(automated["human_override"], 1)
        self.assertEqual(automated["human_override_confirmed"], 1)

    def test_a_real_traceability_gap_still_lowers_the_automated_path(self) -> None:
        """Nothing relaxed: a cited delivery with no evidence must fail."""
        cases = [{"id": "a", "expect": {"sds_outcome": "cited"}}]
        results = [_result("a", citation=("delivered", False, "缺页码"))]
        report = ev.summarise(cases, results)
        automated = report["metrics"]["citation_coverage_automated_path"]
        self.assertEqual(automated["total"], 1)
        self.assertEqual(automated["passed"], 0)
        self.assertEqual(automated["value"], 0.0)
        self.assertEqual([item["id"] for item in report["failures"]], ["a"])


# --------------------------------------------------------------------------- #
# 3. Approval invariants
# --------------------------------------------------------------------------- #


def _observed(**overrides) -> dict:
    base = {
        "id": "t",
        "executed": [],
        "approvals": [],
        "paused": False,
        "pending_kind": "",
        "pending_operation": "",
        "status": "completed",
        "delta_hazards": 0,
        "changed_ids": [],
        "changed_at_pause": [],
        "hazards_after": {},
        "before": {},
    }
    base.update(overrides)
    return base


def _labels(checks) -> dict:
    return {label: ok for label, ok, _ in checks}


class ApprovalInvariantTests(unittest.TestCase):
    def test_a_write_without_an_approval_record_fails(self) -> None:
        checks = ev.approval_checks(
            {"pauses": True, "operation": "create_hazard", "gate_kind": "plan"},
            _observed(
                executed=["create_hazard"],
                paused=True,
                pending_kind="plan",
                pending_operation="create_hazard",
                status="awaiting_approval",
            ),
        )
        self.assertFalse(_labels(checks)["写操作只在存在人工审批记录时执行"])

    def test_an_auto_approval_does_not_count_as_human(self) -> None:
        checks = ev.approval_checks(
            {"pauses": True, "operation": "create_hazard", "gate_kind": "plan",
             "decision": {"action": "approve"}},
            _observed(
                executed=["create_hazard"],
                paused=True,
                pending_kind="plan",
                pending_operation="create_hazard",
                status="completed",
                approvals=[{"action": "auto", "auto": True}],
            ),
        )
        self.assertFalse(_labels(checks)["写操作只在存在人工审批记录时执行"])

    def test_a_pause_after_a_mutation_fails(self) -> None:
        checks = ev.approval_checks(
            {"pauses": True, "operation": "create_hazard", "gate_kind": "plan",
             "decision": None},
            _observed(
                paused=True,
                pending_kind="plan",
                pending_operation="create_hazard",
                status="awaiting_approval",
                changed_at_pause=["HZ-001"],
            ),
        )
        self.assertFalse(_labels(checks)["暂停发生在任何记录变更之前"])

    def test_no_decision_means_the_run_must_stay_suspended(self) -> None:
        pending_ok = ev.approval_checks(
            {"pauses": True, "operation": "create_hazard", "gate_kind": "plan",
             "decision": None},
            _observed(
                paused=True,
                pending_kind="plan",
                pending_operation="create_hazard",
                status="awaiting_approval",
            ),
        )
        self.assertTrue(_labels(pending_ok)["未获人工决策时保持挂起且未写入"])

        leaked = ev.approval_checks(
            {"pauses": True, "operation": "create_hazard", "gate_kind": "plan",
             "decision": None},
            _observed(
                executed=["create_hazard"],
                paused=True,
                pending_kind="plan",
                pending_operation="create_hazard",
                status="completed",
                delta_hazards=1,
            ),
        )
        self.assertFalse(_labels(leaked)["未获人工决策时保持挂起且未写入"])

    def test_rejection_must_leave_the_records_untouched(self) -> None:
        checks = ev.approval_checks(
            {"pauses": True, "operation": "update_hazard", "gate_kind": "plan",
             "decision": {"action": "reject"}},
            _observed(
                paused=True,
                pending_kind="plan",
                pending_operation="update_hazard",
                status="rejected",
                changed_ids=["DEMO-HZ-005"],
            ),
        )
        self.assertFalse(_labels(checks)["拒绝后流程停止且零写入"])

    def test_a_modification_escalation_must_be_rejected(self) -> None:
        checks = ev.approval_checks(
            {"pauses": True, "operation": "create_hazard", "gate_kind": "plan",
             "status": "rejected", "decision": {"action": "modify"}},
            _observed(
                paused=True,
                pending_kind="plan",
                pending_operation="create_hazard",
                status="rejected",
            ),
        )
        self.assertTrue(_labels(checks)["会引入新审批项的修改被按拒绝处理"])

        applied = ev.approval_checks(
            {"pauses": True, "operation": "create_hazard", "gate_kind": "plan",
             "status": "completed", "decision": {"action": "modify"},
             "record_assert": {"HZ-001": {"责任人": "王五"}}},
            _observed(
                executed=["create_hazard"],
                paused=True,
                pending_kind="plan",
                pending_operation="create_hazard",
                status="completed",
                delta_hazards=1,
                approvals=[{"action": "modify", "auto": False}],
                hazards_after={"HZ-001": (("责任人", "王五"),)},
            ),
        )
        self.assertTrue(_labels(applied)["修改后的参数被实际采用"])


# --------------------------------------------------------------------------- #
# 4. Harness end-to-end (no embedding model required)
# --------------------------------------------------------------------------- #


class HarnessTests(unittest.TestCase):
    """Replay real scenarios through the real graph, using stub SDS stores."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = {str(case["id"]): case for case in ev.load_dataset()}

    def _score(self, case_id: str, knowledge_base) -> dict:
        case = self.cases[case_id]
        observed = ev.execute_case(case, knowledge_base)
        return ev.score_case(case, observed)

    def test_a_rejected_write_never_reaches_the_ledger(self) -> None:
        scored = self._score("hz-02", _FakeKnowledgeBase())
        self.assertTrue(scored["routing_ok"], scored["routing_detail"])
        self.assertTrue(scored["tools_ok"], scored["tools_detail"])
        self.assertTrue(scored["completion_ok"], scored["completion_detail"])
        self.assertTrue(all(ok for _, ok, _ in scored["approval_checks"]))

    def test_an_approved_write_lands_in_the_ledger(self) -> None:
        scored = self._score("hz-01", _FakeKnowledgeBase())
        self.assertTrue(scored["completion_ok"], scored["completion_detail"])
        self.assertTrue(all(ok for _, ok, _ in scored["approval_checks"]))

    def test_an_undecided_write_stays_suspended(self) -> None:
        case = self.cases["hz-07"]
        observed = ev.execute_case(case, _FakeKnowledgeBase())
        self.assertTrue(observed["paused"])
        self.assertEqual(observed["status"], "awaiting_approval")
        self.assertEqual(observed["delta_hazards"], 0)
        self.assertEqual(observed["executed"], [])

    def test_a_modification_cannot_escalate_to_a_major_risk_write(self) -> None:
        case = self.cases["hz-08"]
        observed = ev.execute_case(case, _FakeKnowledgeBase())
        self.assertEqual(observed["status"], "rejected")
        self.assertEqual(observed["delta_hazards"], 0)
        self.assertIn("major_risk_write_requires_approval", observed["guard_codes"])

    def test_an_evidence_gap_is_withheld_when_rejected(self) -> None:
        scored = self._score("ev-01", None)
        self.assertTrue(scored["completion_ok"], scored["completion_detail"])
        self.assertIsNotNone(scored["citation"])
        self.assertEqual(scored["citation"][0], "withheld")
        self.assertTrue(all(ok for _, ok, _ in scored["approval_checks"]))

    def test_the_harness_reports_a_failure_when_expectation_is_wrong(self) -> None:
        """The instrument must be able to say "no" — this deliberately lies."""
        case = json.loads(json.dumps(self.cases["hz-01"]))
        case["expect"]["status"] = "blocked"
        observed = ev.execute_case(case, _FakeKnowledgeBase())
        scored = ev.score_case(case, observed)
        self.assertFalse(scored["completion_ok"])
        self.assertIn("结束状态", scored["completion_detail"])


# --------------------------------------------------------------------------- #
# 5. The recorded results stay consistent with the dataset
# --------------------------------------------------------------------------- #


class RecordedResultsTests(unittest.TestCase):
    def test_the_results_file_matches_the_dataset_it_describes(self) -> None:
        if not ev.RESULTS_JSON.exists():
            self.skipTest("尚未运行 evals/evaluate.py，跳过结果一致性检查")

        payload = json.loads(ev.RESULTS_JSON.read_text(encoding="utf-8"))
        cases = ev.load_dataset()

        self.assertEqual(payload["case_count"], len(cases))
        for key in (
            "routing_accuracy",
            "tool_call_accuracy",
            "citation_coverage",
            "approval_compliance",
            "workflow_completion",
        ):
            metric = payload["metrics"][key]
            self.assertIn("value", metric)
            self.assertIn("passed", metric)
            self.assertIn("total", metric)
            self.assertGreaterEqual(metric["value"], 0.0)
            self.assertLessEqual(metric["value"], 100.0)

        recorded_ids = {item["id"] for item in payload["per_case"]}
        self.assertEqual(recorded_ids, {str(case["id"]) for case in cases})

        for _key, metric in payload["metrics"].items():
            self.assertLessEqual(metric["passed"], metric["total"])

    def test_the_human_override_is_never_excluded_from_the_headline(self) -> None:
        """Coverage may not be inflated by dropping samples or relaxing rules."""
        if not ev.RESULTS_JSON.exists():
            self.skipTest("尚未运行 evals/evaluate.py，跳过结果一致性检查")

        payload = json.loads(ev.RESULTS_JSON.read_text(encoding="utf-8"))
        headline = payload["metrics"]["citation_coverage"]
        automated = payload["metrics"]["citation_coverage_automated_path"]

        # Every delivered answer stays in the headline denominator.
        self.assertEqual(
            headline["total"], automated["total"] + automated["human_override"]
        )
        self.assertEqual(payload["sds_delivered_answers"], headline["total"])
        self.assertEqual(
            payload["sds_scenarios_total"],
            payload["sds_delivered_answers"] + payload["sds_withheld_answers"],
        )

        # The human-confirmed exception must still be present, not filtered out.
        self.assertGreater(
            payload["sds_human_override_deliveries"],
            0,
            "人工放行的场景被剔除了——这会把主指标虚高到 100%",
        )
        self.assertGreater(automated["human_override"], 0)

        # The separately reported automated path is where 100% may legitimately
        # appear; it must not have been folded into the headline.
        self.assertLess(headline["value"], automated["value"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Tests for V3 phase 2: human-in-the-loop, guardrails and the timeline.

The tests use a stub vector store so routing, guardrails and the approval gates
can be asserted without loading the embedding model.
"""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from langchain_core.documents import Document
from streamlit.testing.v1 import AppTest

import jsa
from hazards import create_demo_hazard_records, create_hazard_record
from tools import ToolContext
from workflow import guardrails
from workflow.graph import new_thread_id, resume_workflow, run_workflow
from workflow.state import (
    STATUS_AWAITING_APPROVAL,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_REJECTED,
)
from workflow.timeline import (
    KEY_APPROVAL_DONE,
    KEY_APPROVAL_PENDING,
    KEY_COMPLETED,
    KEY_EVIDENCE,
    KEY_JSA,
    KEY_PLAN,
    KEY_RISK,
    KEY_TASK,
    KEY_TOOL,
    KEY_WRITE,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEMO_FILE = "EHS_Copilot_Demo_Synthetic_SDS.pdf"
DEMO_ALIAS = "ehscopilotdemocleanerdx01"

CREATE_TEXT = "新增一条隐患：配电箱前堆放杂物，风险等级高，责任人张三"
UPDATE_TEXT = "更新隐患 DEMO-HZ-005 的状态为整改中"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class _StubIndex:
    ntotal = 1


def ppe_document(*, source: str = "demo.pdf", page: object = 2, sections: str = "8",
                 aliases: str = "demo") -> tuple[Document, float]:
    metadata: dict[str, object] = {"source": source, "sections": sections,
                                   "product_aliases": aliases}
    if page is not None:
        metadata["page"] = page
    return (
        Document(
            page_content="8. 个体防护：操作时必须佩戴耐酸碱手套、护目镜与面屏。",
            metadata=metadata,
        ),
        0.1,
    )


class StubVectorStore:
    """Minimal stand-in for FAISS: only the attributes ``rag`` touches."""

    index = _StubIndex()

    def __init__(self, documents: list[tuple[Document, float]] | None = None) -> None:
        self._documents = documents if documents is not None else [ppe_document()]

    def similarity_search_with_score(self, question: str, k: int = 5):
        return list(self._documents)


def make_context(**overrides) -> ToolContext:
    """Build a session context backed by the stub store and demo hazards."""
    defaults: dict[str, object] = {
        "vector_store": StubVectorStore(),
        "jsa_records": [],
        "hazard_records": create_demo_hazard_records(),
        "loaded_files": (DEMO_FILE,),
    }
    defaults.update(overrides)
    return ToolContext(**defaults)  # type: ignore[arg-type]


def gate_id_of(result) -> str:
    assert result.pending_approval is not None, "expected a pending approval"
    return str(result.pending_approval["gate_id"])


# --------------------------------------------------------------------------- #
# 1. Human-in-the-loop: writes must be approved
# --------------------------------------------------------------------------- #


class WriteApprovalTests(unittest.TestCase):
    def test_create_hazard_is_not_executed_without_a_decision(self) -> None:
        context = make_context()
        before = len(context.hazard_records)

        result = run_workflow(CREATE_TEXT, context, auto_approve=False)

        self.assertEqual(result.status, STATUS_AWAITING_APPROVAL)
        self.assertTrue(result.awaiting_approval)
        self.assertEqual(len(context.hazard_records), before)
        self.assertEqual(result.tool_results, ())
        self.assertEqual(result.pending_approval["operation"], "create_hazard")
        self.assertIn("write_requires_approval", result.pending_approval["guard_codes"])

    def test_approve_continues_and_executes_the_write(self) -> None:
        context = make_context()
        before = len(context.hazard_records)
        first = run_workflow(CREATE_TEXT, context, auto_approve=False)

        result = resume_workflow(
            first.thread_id,
            context,
            {"gate_id": gate_id_of(first), "action": "approve", "round": 1},
        )

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertEqual(len(context.hazard_records), before + 1)
        self.assertEqual([item["tool"] for item in result.tool_results], ["create_hazard"])
        self.assertEqual(result.approvals[0]["action"], "approve")
        self.assertFalse(result.approvals[0]["auto"])

    def test_reject_stops_the_flow_without_writing(self) -> None:
        context = make_context()
        before = len(context.hazard_records)
        first = run_workflow(CREATE_TEXT, context, auto_approve=False)

        result = resume_workflow(
            first.thread_id,
            context,
            {
                "gate_id": gate_id_of(first),
                "action": "reject",
                "round": 1,
                "note": "现场已自行整改",
            },
        )

        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(len(context.hazard_records), before)
        self.assertEqual(result.tool_results, ())
        self.assertIn("已被人工拒绝", result.final_answer)
        self.assertEqual(result.approvals[0]["action"], "reject")
        self.assertEqual(result.approvals[0]["note"], "现场已自行整改")

    def test_modify_continues_with_the_edited_arguments(self) -> None:
        context = make_context()
        before = len(context.hazard_records)
        first = run_workflow(CREATE_TEXT, context, auto_approve=False)

        calls = [dict(item) for item in first.pending_approval["calls"]]
        calls[0]["arguments"]["owner"] = "王五"
        calls[0]["arguments"]["risk_level"] = "中"

        result = resume_workflow(
            first.thread_id,
            context,
            {
                "gate_id": gate_id_of(first),
                "action": "modify",
                "calls": calls,
                "round": 1,
            },
        )

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertEqual(len(context.hazard_records), before + 1)
        record = context.hazard_records[-1]
        self.assertEqual(record["责任人"], "王五")
        self.assertEqual(record["风险等级"], "中")
        self.assertEqual(result.approvals[0]["action"], "modify")

    def test_modify_cannot_escalate_to_a_major_risk_write(self) -> None:
        context = make_context()
        before = len(context.hazard_records)
        first = run_workflow(CREATE_TEXT, context, auto_approve=False)

        calls = [dict(item) for item in first.pending_approval["calls"]]
        calls[0]["arguments"]["risk_level"] = "重大"

        result = resume_workflow(
            first.thread_id,
            context,
            {"gate_id": gate_id_of(first), "action": "modify", "calls": calls, "round": 1},
        )

        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(len(context.hazard_records), before)
        self.assertIn("major_risk_write_requires_approval",
                      [item["code"] for item in result.guardrails])

    def test_update_hazard_is_not_executed_without_a_decision(self) -> None:
        context = make_context()
        target = next(
            item for item in context.hazard_records if item["隐患编号"] == "DEMO-HZ-005"
        )
        before = target["状态"]

        result = run_workflow(UPDATE_TEXT, context, auto_approve=False)

        self.assertEqual(result.status, STATUS_AWAITING_APPROVAL)
        self.assertEqual(result.tool_results, ())
        self.assertEqual(target["状态"], before)
        self.assertEqual(result.pending_approval["operation"], "update_hazard")

    def test_approving_an_update_applies_it(self) -> None:
        context = make_context()
        first = run_workflow(UPDATE_TEXT, context, auto_approve=False)

        result = resume_workflow(
            first.thread_id,
            context,
            {"gate_id": gate_id_of(first), "action": "approve", "round": 1},
        )

        self.assertEqual(result.status, STATUS_COMPLETED)
        target = next(
            item for item in context.hazard_records if item["隐患编号"] == "DEMO-HZ-005"
        )
        self.assertEqual(target["状态"], "整改中")

    def test_closing_a_hazard_needs_its_own_approval(self) -> None:
        context = make_context()
        result = run_workflow(
            "更新隐患 DEMO-HZ-005 的状态为已关闭", context, auto_approve=False
        )
        codes = result.pending_approval["guard_codes"]
        self.assertIn("hazard_close_requires_approval", codes)
        self.assertEqual(result.pending_approval["operation"], "close_hazard")

    def test_downgrading_a_risk_level_needs_approval(self) -> None:
        record = create_hazard_record(
            hazard_id="HZ-901",
            description="高温管线保温层破损",
            hazard_type="设备安全",
            risk_level="高",
            owner="张三",
            found_on=date(2026, 9, 1),
            due_on=date(2026, 9, 20),
            corrective_action="修复保温层",
            status="待整改",
        )
        context = make_context(hazard_records=[record])

        result = run_workflow(
            "更新隐患 HZ-901 的风险等级为低", context, auto_approve=False
        )

        self.assertEqual(result.pending_approval["operation"], "risk_downgrade")
        self.assertIn("risk_downgrade_requires_approval",
                      result.pending_approval["guard_codes"])
        self.assertEqual(record["风险等级"], "高")

    def test_a_stale_gate_id_is_treated_as_a_rejection(self) -> None:
        context = make_context()
        before = len(context.hazard_records)
        first = run_workflow(CREATE_TEXT, context, auto_approve=False)

        result = resume_workflow(
            first.thread_id,
            context,
            {"gate_id": "plan:not-the-right-gate", "action": "approve", "round": 1},
        )

        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(len(context.hazard_records), before)

    def test_auto_approve_keeps_phase_one_behaviour(self) -> None:
        context = make_context()
        before = len(context.hazard_records)

        result = run_workflow(CREATE_TEXT, context)

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertEqual(len(context.hazard_records), before + 1)
        self.assertEqual(result.approvals[0]["auto"], True)


# --------------------------------------------------------------------------- #
# 2. Guardrails (pure functions)
# --------------------------------------------------------------------------- #


class ChemicalGuardrailTests(unittest.TestCase):
    def test_a_question_about_another_chemical_is_blocked(self) -> None:
        violation = guardrails.chemical_mismatch(
            "查询浓硫酸泄漏的应急处置要求",
            loaded_files=(DEMO_FILE,),
            document_aliases=(DEMO_ALIAS, "demo"),
        )
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertEqual(violation.code, guardrails.GUARD_CHEMICAL_MISMATCH)
        self.assertEqual(violation.severity, guardrails.SEVERITY_BLOCK)

    def test_a_covered_chemical_is_not_a_mismatch(self) -> None:
        self.assertIsNone(
            guardrails.chemical_mismatch(
                "查询氢氟酸的个体防护要求",
                loaded_files=("HF_SDS_2024.pdf",),
            )
        )

    def test_nothing_is_claimed_when_no_sds_is_loaded(self) -> None:
        self.assertIsNone(
            guardrails.chemical_mismatch("查询浓硫酸的处置要求", loaded_files=())
        )

    def test_overlapping_chemical_names_prefer_the_longest_alias(self) -> None:
        self.assertEqual(guardrails.chemical_mentions("甲苯泄漏"), ("甲苯",))
        self.assertEqual(guardrails.chemical_mentions("HF 酸洗作业"), ("氢氟酸",))
        self.assertEqual(guardrails.chemical_mentions("查询该 SDS 中关于 PPE 的要求"), ())


class EmergencyGuardrailTests(unittest.TestCase):
    def test_a_live_event_triggers_the_emergency_notice(self) -> None:
        violation = guardrails.emergency_notice(
            "现场发生大量氢氟酸泄漏，有人员灼伤，怎么处理"
        )
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertEqual(violation.severity, guardrails.SEVERITY_NOTICE)
        self.assertIn("应急预案", violation.detail)
        self.assertIn("专业人员", violation.detail)
        self.assertIn("119", violation.detail)

    def test_a_question_about_a_topic_is_not_an_emergency(self) -> None:
        for text in (
            "查询浓硫酸泄漏的应急处置要求",
            "查询该 SDS 中关于火灾的消防措施",
            "SDS 第 4 节皮肤接触急救怎么做",
        ):
            with self.subTest(text=text):
                self.assertIsNone(guardrails.emergency_notice(text))

    def test_a_short_live_event_phrase_triggers_it(self) -> None:
        self.assertIsNotNone(guardrails.emergency_notice("氢气泄漏了怎么办"))


class RiskGuardrailTests(unittest.TestCase):
    def test_risk_arguments_must_be_integers_within_range(self) -> None:
        plan = [{"tool": "calculate_risk", "arguments": {"likelihood": 9, "severity": 5}}]
        violations = guardrails.risk_argument_violations(plan)
        self.assertEqual([item.code for item in violations], [guardrails.GUARD_RISK_ARGUMENTS])

        plan = [{"tool": "calculate_risk", "arguments": {"likelihood": "四", "severity": 5}}]
        self.assertTrue(guardrails.risk_argument_violations(plan))

    def test_a_plan_may_not_carry_a_precomputed_score(self) -> None:
        plan = [
            {
                "tool": "calculate_risk",
                "arguments": {"likelihood": 4, "severity": 5, "risk_score": 20},
            }
        ]
        violations = guardrails.risk_argument_violations(plan)
        self.assertEqual([item.code for item in violations], [guardrails.GUARD_RISK_BY_CODE])
        self.assertEqual(violations[0].severity, guardrails.SEVERITY_BLOCK)

    def test_a_payload_score_is_recrossed_against_the_code(self) -> None:
        good = {
            "tool": "calculate_risk",
            "status": "ok",
            "likelihood": 4,
            "severity": 5,
            "risk_score": 20,
            "risk_level": "重大风险",
        }
        self.assertIsNone(guardrails.risk_payload_violation(good))

        tampered = dict(good, risk_score=10, risk_level="低风险")
        violation = guardrails.risk_payload_violation(tampered)
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertEqual(violation.code, guardrails.GUARD_RISK_BY_CODE)

    def test_a_score_without_recomputable_arguments_is_blocked(self) -> None:
        payload = {
            "tool": "calculate_risk",
            "status": "ok",
            "risk_score": 20,
            "risk_level": "重大风险",
        }
        violation = guardrails.risk_payload_violation(payload)
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertEqual(violation.severity, guardrails.SEVERITY_BLOCK)

    def test_a_residual_score_is_verified_too(self) -> None:
        record = jsa.create_jsa_record(
            job_name="HF 酸洗",
            job_step="HF 酸洗",
            hazard="HF 飞溅",
            consequence="化学灼伤",
            likelihood=4,
            severity=5,
            existing_controls="全封闭输送",
            suggested_controls="加强局部排风",
            residual_likelihood=3,
            residual_severity=5,
        )
        payload = {
            "tool": "draft_jsa",
            "status": "ok",
            "likelihood": 4,
            "severity": 5,
            "risk_score": record["风险值R"],
            "risk_level": record["风险等级"],
            "residual_risk_score": record["残余风险R"],
            "residual_risk_level": record["残余风险等级"],
            "record": record,
        }
        self.assertIsNone(guardrails.risk_payload_violation(payload))
        self.assertIsNotNone(
            guardrails.risk_payload_violation(dict(payload, residual_risk_score=1))
        )

    def test_risk_scores_come_from_jsa_not_from_a_model(self) -> None:
        for likelihood in range(1, 6):
            for severity in range(1, 6):
                with self.subTest(likelihood=likelihood, severity=severity):
                    expected = jsa.calculate_risk(likelihood, severity)
                    context = make_context()
                    payload = run_workflow(
                        f"计算风险值 L={likelihood} S={severity}", context
                    ).tool_results[0]
                    self.assertEqual(payload["risk_score"], expected[0])
                    self.assertEqual(payload["risk_level"], expected[1])


class EvidenceGuardrailTests(unittest.TestCase):
    def test_evidence_gaps_are_detected(self) -> None:
        self.assertEqual(
            guardrails.evidence_gaps({"evidence": [], "sources": []}),
            ["检索结果中没有返回任何证据片段。"],
        )
        gaps = guardrails.evidence_gaps(
            {"evidence": [{"source": "a.pdf", "page": 0, "snippet": "x"}], "sources": [["a.pdf", 0]]}
        )
        self.assertTrue(any("页码" in item for item in gaps))

    def test_a_section_from_two_files_is_a_conflict(self) -> None:
        payload = {
            "evidence": [
                {"source": "a.pdf", "page": 2, "sections": "8", "snippet": "x"},
                {"source": "b.pdf", "page": 5, "sections": "8", "snippet": "y"},
            ]
        }
        conflicts = guardrails.evidence_conflicts(payload)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["sources"], ["a.pdf", "b.pdf"])

    def test_clean_evidence_produces_no_violation(self) -> None:
        payload = {
            "tool": "search_sds",
            "status": "ok",
            "question": "该 SDS 中关于 PPE 的要求",
            "sources": [["demo.pdf", 2]],
            "evidence": [
                {"source": "demo.pdf", "page": 2, "sections": "8", "snippet": "个体防护：…"}
            ],
        }
        self.assertEqual(guardrails.sds_violations(payload, question=payload["question"]), [])


# --------------------------------------------------------------------------- #
# 3. Guardrails inside the graph
# --------------------------------------------------------------------------- #


class SdsEvidenceInGraphTests(unittest.TestCase):
    def test_a_conclusion_without_evidence_is_blocked(self) -> None:
        context = make_context(vector_store=StubVectorStore(documents=[]))

        result = run_workflow("查询该 SDS 中关于 PPE 的要求", context)

        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertEqual(result.tool_results[0]["status"], "blocked")
        self.assertFalse(result.citations_ok)
        self.assertIn("sds_evidence_required",
                      [item["code"] for item in result.guardrails])

    def test_a_conclusion_without_evidence_pauses_for_approval(self) -> None:
        context = make_context(vector_store=StubVectorStore(documents=[]))

        first = run_workflow("查询该 SDS 中关于 PPE 的要求", context, auto_approve=False)

        self.assertEqual(first.status, STATUS_AWAITING_APPROVAL)
        self.assertEqual(first.pending_approval["kind"], "evidence")
        self.assertEqual(first.pending_approval["operation"], "sds_insufficient_evidence")

        rejected = resume_workflow(
            first.thread_id,
            context,
            {"gate_id": gate_id_of(first), "action": "reject", "round": 1},
        )
        self.assertEqual(rejected.tool_results[0]["status"], "blocked")

    def test_approving_an_under_evidenced_conclusion_marks_it_human_confirmed(self) -> None:
        context = make_context(vector_store=StubVectorStore(documents=[]))
        first = run_workflow("查询该 SDS 中关于 PPE 的要求", context, auto_approve=False)

        result = resume_workflow(
            first.thread_id,
            context,
            {"gate_id": gate_id_of(first), "action": "approve", "round": 1},
        )

        self.assertEqual(result.tool_results[0]["status"], "ok")
        self.assertTrue(result.tool_results[0]["human_confirmed"])
        self.assertEqual(result.approvals[0]["phase"], "evidence")

    def test_an_evidence_gap_asks_for_approval_even_when_a_snippet_exists(self) -> None:
        context = make_context(
            vector_store=StubVectorStore(documents=[ppe_document(page=None)])
        )

        result = run_workflow("查询该 SDS 中关于 PPE 的要求", context, auto_approve=False)

        self.assertEqual(result.status, STATUS_AWAITING_APPROVAL)
        self.assertEqual(result.pending_approval["operation"], "sds_insufficient_evidence")

    def test_a_source_conflict_asks_for_approval(self) -> None:
        context = make_context(
            vector_store=StubVectorStore(
                documents=[
                    ppe_document(source="a.pdf", page=2, aliases="demo"),
                    ppe_document(source="b.pdf", page=5, aliases="demo"),
                ]
            )
        )

        result = run_workflow("查询该 SDS 中关于 PPE 的要求", context, auto_approve=False)

        self.assertEqual(result.status, STATUS_AWAITING_APPROVAL)
        self.assertEqual(result.pending_approval["operation"], "sds_source_conflict")
        self.assertIn("sds_source_conflict",
                      result.pending_approval["guard_codes"])

    def test_a_mismatched_chemical_stops_the_sds_conclusion(self) -> None:
        context = make_context()

        result = run_workflow("查询浓硫酸泄漏的应急处置要求", context)

        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertEqual(result.tool_results[0]["status"], "blocked")
        self.assertIn("chemical_sds_mismatch",
                      [item["code"] for item in result.guardrails])

    def test_an_emergency_adds_the_protocol_notice_to_the_answer(self) -> None:
        context = make_context()

        result = run_workflow("现场发生大量氢氟酸泄漏，有人员灼伤", context)

        self.assertIn("emergency_protocol", [item["code"] for item in result.guardrails])
        self.assertIn("应急预案", result.final_answer)
        self.assertIn("专业人员", result.final_answer)


# --------------------------------------------------------------------------- #
# 4. Timeline
# --------------------------------------------------------------------------- #


REQUIRED_LABELS = (
    KEY_TASK,
    KEY_PLAN,
    KEY_TOOL,
    KEY_EVIDENCE,
    KEY_JSA,
    KEY_RISK,
    KEY_APPROVAL_PENDING,
    KEY_APPROVAL_DONE,
    KEY_WRITE,
    KEY_COMPLETED,
)


class TimelineTests(unittest.TestCase):
    def test_the_timeline_covers_every_required_event(self) -> None:
        context = make_context()
        text = (
            "查询该 SDS 中关于 PPE 的要求，并为该作业做 JSA 风险评估，"
            "同时新增一条隐患：现场堆放杂物"
        )

        first = run_workflow(text, context, auto_approve=False)
        suspended_keys = {item["key"] for item in first.timeline}
        self.assertIn(KEY_TASK, suspended_keys)
        self.assertIn(KEY_PLAN, suspended_keys)
        self.assertIn(KEY_APPROVAL_PENDING, suspended_keys)
        self.assertEqual(first.timeline[-1]["key"], KEY_APPROVAL_PENDING)

        result = resume_workflow(
            first.thread_id,
            context,
            {"gate_id": gate_id_of(first), "action": "approve", "round": 1},
        )
        resumed_keys = {item["key"] for item in result.timeline}
        self.assertIn(KEY_APPROVAL_DONE, resumed_keys)
        self.assertNotIn(KEY_APPROVAL_PENDING, resumed_keys)
        self.assertEqual(result.timeline[-1]["key"], KEY_COMPLETED)

        # Across the pause and the resume, every required event is shown.
        for key in REQUIRED_LABELS:
            self.assertIn(key, suspended_keys | resumed_keys, f"missing event: {key}")

    def test_the_timeline_is_ordered_and_marks_the_final_state(self) -> None:
        context = make_context()
        result = run_workflow("给我看看仪表盘的整体情况", context)

        orders = [item["order"] for item in result.timeline]
        self.assertEqual(orders, list(range(1, len(orders) + 1)))
        self.assertEqual(result.timeline[-1]["key"], KEY_COMPLETED)
        self.assertEqual(result.timeline[-1]["status"], "done")

    def test_rejection_is_reflected_in_the_timeline(self) -> None:
        context = make_context()
        first = run_workflow(CREATE_TEXT, context, auto_approve=False)
        result = resume_workflow(
            first.thread_id,
            context,
            {"gate_id": gate_id_of(first), "action": "reject", "round": 1},
        )

        final = result.timeline[-1]
        self.assertEqual(final["status"], "rejected")
        self.assertNotIn(KEY_WRITE, {item["key"] for item in result.timeline})
        self.assertTrue(any(item["key"] == KEY_APPROVAL_DONE for item in result.timeline))

    def test_a_blocked_run_marks_the_evidence_event(self) -> None:
        context = make_context(vector_store=StubVectorStore(documents=[]))
        result = run_workflow("查询该 SDS 中关于 PPE 的要求", context)

        evidence = next(
            item for item in result.timeline if item["key"] == KEY_EVIDENCE
        )
        self.assertEqual(evidence["status"], "blocked")


# --------------------------------------------------------------------------- #
# 5. Streamlit page end-to-end
# --------------------------------------------------------------------------- #


class WorkflowPageApprovalTests(unittest.TestCase):
    def test_the_page_pauses_for_approval_and_continues_after_approving(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=600).run()
        self.assertEqual(len(app.exception), 0)

        app.radio[0].set_value("AI工作流控制台").run(timeout=600)
        self.assertEqual(len(app.exception), 0)

        before = len(app.session_state["hazard_records"])
        next(
            button for button in app.button if button.label == "新增隐患（需审批）"
        ).click().run(timeout=600)
        self.assertEqual(len(app.exception), 0)

        pending = app.session_state["workflow_result"]
        self.assertEqual(pending["status"], STATUS_AWAITING_APPROVAL)
        self.assertIsNotNone(pending["pending_approval"])
        self.assertEqual(len(app.session_state["hazard_records"]), before)

        labels = [button.label for button in app.button]
        self.assertIn("✅ 批准", labels)
        self.assertIn("✏️ 修改参数后批准", labels)
        self.assertIn("⛔ 拒绝", labels)

        next(button for button in app.button if button.label == "✅ 批准").click().run(
            timeout=600
        )
        self.assertEqual(len(app.exception), 0)

        completed = app.session_state["workflow_result"]
        self.assertEqual(completed["status"], STATUS_COMPLETED)
        self.assertEqual(len(app.session_state["hazard_records"]), before + 1)

        markdown = "\n".join(item.value for item in app.markdown)
        for section in (
            "识别出的任务类型",
            "计划调用的工具",
            "当前执行步骤",
            "工具执行结果",
            "最终回答",
            "执行时间线",
            "已识别任务",
            "已执行写操作",
        ):
            self.assertIn(section, markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)

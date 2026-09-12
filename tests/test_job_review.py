"""Tests for the P3 job review flow: evidence, JSA draft, confirmation, approval."""

from __future__ import annotations

import copy
import unittest
from datetime import date, datetime

import jsa
from demo_cases import (
    HF_JOB_ID,
    HFCaseSourceError,
    create_demo_hf_case,
    require_hf_sds_source,
)
from job_review import (
    EVIDENCE_TRACK_LABELS,
    EVIDENCE_TRACK_PUBLIC,
    EVIDENCE_TRACK_SDS,
    OPEN_ITEM_PREFIX,
    attach_public_evidence,
    attach_sds_evidence,
    build_job_approval_request,
    confirm_jsa,
    decide_job_approval,
    draft_jsa,
    start_job_execution,
)
from jobs import (
    JOB_STATUS_APPROVED,
    JOB_STATUS_AWAITING_APPROVAL,
    JOB_STATUS_AWAITING_EHS,
    JOB_STATUS_DRAFT,
    JOB_STATUS_EXECUTING,
    JOB_STATUS_REJECTED,
    create_job,
)
from workflow.hitl import (
    ACTION_APPROVE,
    ACTION_MODIFY,
    ACTION_REJECT,
    ApprovalDecision,
    ApprovalRequest,
)


FIXED_TODAY = date(2026, 9, 12)
FIXED_NOW = datetime(2026, 9, 12, 10, 0, 0)

CONFIRM_KWARGS: dict[str, object] = {
    "confirmed_by": "EHS张",
    "job_step": "HF酸洗作业（EHS人工确认）",
    "hazard": "HF 接触可致严重灼伤（人工确认）",
    "consequence": "皮肤/眼睛损伤及系统性影响（人工确认）",
    "suggested_controls": "按公开来源证据配置工程控制与 PPE（人工确认）",
    "existing_controls": "局部排风（模拟现有措施）",
    "likelihood": 4,
    "severity": 5,
    "residual_likelihood": 2,
    "residual_severity": 5,
    "change_note": "按现场条件确认 L/S 并修改控制措施",
}

SDS_SNAPSHOT: dict[str, object] = {
    "source": "HF_SDS.pdf",
    "page": 4,
    "sections": "4",
    "snippet": "First-aid instructions quoted from the uploaded SDS (test fixture).",
}


def make_case_records() -> tuple[list[dict[str, object]], dict[str, object]]:
    case = create_demo_hf_case(today=FIXED_TODAY, now=FIXED_NOW)
    return [case["job"]], case


def draft_and_confirm(records: list[dict[str, object]]) -> dict[str, object]:
    draft_jsa(records, HF_JOB_ID, now=FIXED_NOW)
    return confirm_jsa(records, HF_JOB_ID, now=FIXED_NOW, **CONFIRM_KWARGS)


class EvidenceAttachmentTests(unittest.TestCase):
    def test_demo_case_attaches_public_evidence_to_the_job(self) -> None:
        records, case = make_case_records()
        job = records[0]
        self.assertGreater(len(job["public_evidence"]), 0)
        self.assertEqual(
            len(job["public_evidence"]), len(case["public_evidence"]["items"])
        )
        self.assertEqual(job["sds_evidence"], [])
        self.assertTrue(
            str(job["public_evidence"][0]["source_url"]).startswith("https://")
        )

    def test_track_labels_keep_public_evidence_away_from_sds(self) -> None:
        self.assertEqual(
            EVIDENCE_TRACK_LABELS[EVIDENCE_TRACK_PUBLIC], "公开来源证据（非 SDS）"
        )
        self.assertIn("非 SDS", EVIDENCE_TRACK_LABELS[EVIDENCE_TRACK_PUBLIC])
        self.assertIn("SDS", EVIDENCE_TRACK_LABELS[EVIDENCE_TRACK_SDS])
        self.assertEqual(EVIDENCE_TRACK_SDS, "sds")

    def test_sds_snapshot_attaches_only_to_the_sds_track(self) -> None:
        records, _ = make_case_records()
        job = records[0]
        public_before = copy.deepcopy(job["public_evidence"])
        attach_sds_evidence(job, [SDS_SNAPSHOT])
        self.assertEqual(len(job["sds_evidence"]), 1)
        self.assertEqual(job["sds_evidence"][0]["source"], "HF_SDS.pdf")
        self.assertEqual(job["public_evidence"], public_before)

    def test_sds_track_rejects_public_source_items(self) -> None:
        records, case = make_case_records()
        with self.assertRaises(ValueError):
            attach_sds_evidence(records[0], [case["public_evidence"]["items"][0]])

    def test_public_track_rejects_sds_shaped_items(self) -> None:
        records, _ = make_case_records()
        with self.assertRaises(ValueError):
            attach_public_evidence(records[0], [SDS_SNAPSHOT])

    def test_sds_evidence_requires_an_uploaded_pdf(self) -> None:
        records, _ = make_case_records()
        with self.assertRaises(ValueError):
            attach_sds_evidence(
                records[0],
                [{"source": "notes.txt", "page": 1, "snippet": "x"}],
            )

    def test_no_sds_citation_without_an_uploaded_pdf(self) -> None:
        records, _ = make_case_records()
        job = records[0]
        self.assertEqual(job["sds_evidence"], [])
        with self.assertRaises(HFCaseSourceError):
            require_hf_sds_source()

        draft_jsa(records, HF_JOB_ID, now=FIXED_NOW)
        tracks = {
            item["evidence_track"]
            for item in job["jsa_draft"]["hazard_candidates"]
            + job["jsa_draft"]["control_candidates"]
        }
        self.assertSetEqual(tracks, {EVIDENCE_TRACK_PUBLIC})


class DraftTests(unittest.TestCase):
    def test_draft_moves_the_job_to_awaiting_ehs_confirmation(self) -> None:
        records, _ = make_case_records()
        job = draft_jsa(records, HF_JOB_ID, now=FIXED_NOW)
        self.assertEqual(job["status"], JOB_STATUS_AWAITING_EHS)
        self.assertEqual(job["status_history"][-1]["from"], JOB_STATUS_DRAFT)
        self.assertTrue(str(job["status_history"][-1]["actor"]).strip())

    def test_draft_is_built_from_steps_and_public_evidence(self) -> None:
        records, _ = make_case_records()
        job = draft_jsa(records, HF_JOB_ID, now=FIXED_NOW)
        draft = job["jsa_draft"]
        self.assertEqual(len(draft["steps"]), 6)
        self.assertTrue(draft["hazard_candidates"])
        self.assertTrue(draft["consequence_candidates"])
        self.assertTrue(draft["control_candidates"])
        for candidate in (
            draft["hazard_candidates"]
            + draft["consequence_candidates"]
            + draft["control_candidates"]
        ):
            with self.subTest(evidence_id=candidate["evidence_id"]):
                self.assertEqual(candidate["evidence_track"], EVIDENCE_TRACK_PUBLIC)
                self.assertTrue(candidate["source_url"])
                self.assertEqual(candidate["status"], "draft_from_evidence")
                self.assertTrue(candidate["awaiting_confirmation"])
        self.assertEqual(draft["evidence_summary"]["sds"], 0)

    def test_draft_never_computes_risk(self) -> None:
        records, _ = make_case_records()
        job = draft_jsa(records, HF_JOB_ID, now=FIXED_NOW)
        draft = job["jsa_draft"]
        self.assertIsNone(draft["risk_inputs"])
        for key in ("风险值R", "风险等级", "残余风险R", "风险_score", "risk_score"):
            self.assertNotIn(key, draft)
        self.assertIsNone(draft["suggested"]["likelihood"])
        self.assertIsNone(draft["suggested"]["severity"])

    def test_open_items_flag_missing_sds_track_and_risk_inputs(self) -> None:
        records, _ = make_case_records()
        job = draft_jsa(records, HF_JOB_ID, now=FIXED_NOW)
        open_items = job["jsa_draft"]["open_items"]
        self.assertTrue(open_items)
        self.assertTrue(all(item.startswith(OPEN_ITEM_PREFIX) for item in open_items))
        self.assertTrue(any("SDS" in item for item in open_items))
        self.assertTrue(any("L/S" in item for item in open_items))

    def test_draft_without_evidence_guesses_nothing(self) -> None:
        job = create_job(
            job_name="无证据作业（测试）",
            created_by="申请人（测试）",
            now=FIXED_NOW,
        )
        records = [job]
        draft_jsa(records, str(job["job_id"]), now=FIXED_NOW)
        draft = job["jsa_draft"]
        self.assertEqual(draft["hazard_candidates"], [])
        self.assertEqual(draft["consequence_candidates"], [])
        self.assertEqual(draft["control_candidates"], [])
        self.assertTrue(
            any("尚无任何安全证据" in item for item in draft["open_items"])
        )
        self.assertEqual(job["status"], JOB_STATUS_AWAITING_EHS)

    def test_draft_requires_draft_status(self) -> None:
        records, _ = make_case_records()
        draft_jsa(records, HF_JOB_ID, now=FIXED_NOW)
        with self.assertRaises(ValueError):
            draft_jsa(records, HF_JOB_ID, now=FIXED_NOW)


class ConfirmationTests(unittest.TestCase):
    def test_confirmation_requires_awaiting_ehs_status(self) -> None:
        records, _ = make_case_records()
        with self.assertRaises(ValueError):
            confirm_jsa(records, HF_JOB_ID, now=FIXED_NOW, **CONFIRM_KWARGS)
        self.assertEqual(records[0]["status"], JOB_STATUS_DRAFT)

    def test_confirmation_requires_a_confirmer(self) -> None:
        records, _ = make_case_records()
        draft_jsa(records, HF_JOB_ID, now=FIXED_NOW)
        values = dict(CONFIRM_KWARGS, confirmed_by="   ")
        with self.assertRaises(ValueError):
            confirm_jsa(records, HF_JOB_ID, now=FIXED_NOW, **values)
        self.assertEqual(records[0]["status"], JOB_STATUS_AWAITING_EHS)

    def test_confirmation_computes_risk_with_existing_code(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        final = job["jsa_confirmation"]["final"]
        expected_initial = jsa.calculate_risk(4, 5)
        expected_residual = jsa.calculate_risk(2, 5)
        self.assertEqual(final["风险值R"], expected_initial[0])
        self.assertEqual(final["风险等级"], expected_initial[1])
        self.assertEqual(final["残余风险R"], expected_residual[0])
        self.assertEqual(final["残余风险等级"], expected_residual[1])
        self.assertEqual(
            job["jsa_confirmation"]["risk_source"], "jsa.calculate_risk"
        )

    def test_confirmation_rejects_invalid_risk_inputs(self) -> None:
        records, _ = make_case_records()
        draft_jsa(records, HF_JOB_ID, now=FIXED_NOW)
        values = dict(CONFIRM_KWARGS, likelihood=6)
        with self.assertRaises(ValueError):
            confirm_jsa(records, HF_JOB_ID, now=FIXED_NOW, **values)
        self.assertEqual(records[0]["status"], JOB_STATUS_AWAITING_EHS)

    def test_confirmation_preserves_ai_draft_and_human_final(self) -> None:
        records, _ = make_case_records()
        draft_jsa(records, HF_JOB_ID, now=FIXED_NOW)
        job = records[0]
        ai_draft_before = copy.deepcopy(job["jsa_draft"])

        confirm_jsa(
            records,
            HF_JOB_ID,
            now=FIXED_NOW,
            confirmed_by="EHS张",
            job_step="HF酸洗（人工步骤）",
            hazard="人工修改后的危害描述",
            consequence="人工修改后的后果描述",
            suggested_controls="人工修改后的控制措施",
            existing_controls="人工填写现有措施",
            likelihood=4,
            severity=5,
            residual_likelihood=2,
            residual_severity=5,
            change_note="按现场条件调整危害与控制措施",
        )

        self.assertEqual(job["jsa_draft"], ai_draft_before)
        confirmation = job["jsa_confirmation"]
        self.assertEqual(confirmation["ai_draft"], ai_draft_before)
        self.assertEqual(confirmation["final"]["危害因素"], "人工修改后的危害描述")
        self.assertEqual(confirmation["final"]["作业步骤"], "HF酸洗（人工步骤）")
        self.assertEqual(confirmation["confirmed_by"], "EHS张")
        self.assertEqual(confirmation["confirmed_at"], FIXED_NOW.isoformat())
        self.assertEqual(
            confirmation["change_note"], "按现场条件调整危害与控制措施"
        )
        for field in ("hazard", "consequence", "suggested_controls", "likelihood"):
            with self.subTest(field=field):
                self.assertIn(field, confirmation["changed_fields"])
                self.assertNotEqual(
                    confirmation["changed_fields"][field]["ai_draft"],
                    confirmation["changed_fields"][field]["final"],
                )

    def test_confirmation_moves_the_job_to_awaiting_approval(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        self.assertEqual(job["status"], JOB_STATUS_AWAITING_APPROVAL)
        last = job["status_history"][-1]
        self.assertEqual(last["from"], JOB_STATUS_AWAITING_EHS)
        self.assertEqual(last["to"], JOB_STATUS_AWAITING_APPROVAL)
        self.assertEqual(last["actor"], "EHS张")


class ApprovalTests(unittest.TestCase):
    def test_cannot_approve_before_ehs_confirmation(self) -> None:
        records, _ = make_case_records()
        with self.assertRaises(ValueError):
            build_job_approval_request(records[0])
        with self.assertRaises(ValueError):
            decide_job_approval(
                records,
                HF_JOB_ID,
                ApprovalDecision(gate_id="job:x:approval", action=ACTION_APPROVE),
                actor="审批人李",
            )
        draft_jsa(records, HF_JOB_ID, now=FIXED_NOW)
        with self.assertRaises(ValueError):
            decide_job_approval(
                records,
                HF_JOB_ID,
                ApprovalDecision(
                    gate_id=f"job:{HF_JOB_ID}:approval", action=ACTION_APPROVE
                ),
                actor="审批人李",
            )
        self.assertEqual(records[0]["status"], JOB_STATUS_AWAITING_EHS)

    def test_request_carries_risk_values_and_confirmer(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        request = build_job_approval_request(job)
        self.assertEqual(request.gate_id, f"job:{HF_JOB_ID}:approval")
        self.assertEqual(request.operation_label, "批准作业单")
        self.assertEqual(request.guard_codes, ("job_approval_required",))
        self.assertEqual(request.current["风险值R"], 20)
        self.assertEqual(request.current["确认人"], "EHS张")
        restored = ApprovalRequest.from_payload(request.to_payload())
        self.assertEqual(restored.gate_id, request.gate_id)
        self.assertEqual(restored.operation, request.operation)

    def test_approve_moves_the_job_to_approved_and_records_actor(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        request = build_job_approval_request(job)
        decide_job_approval(
            records,
            HF_JOB_ID,
            ApprovalDecision(
                gate_id=request.gate_id, action=ACTION_APPROVE, note="同意执行"
            ),
            actor="审批人李",
            now=FIXED_NOW,
        )
        self.assertEqual(job["status"], JOB_STATUS_APPROVED)
        entry = job["approvals"][-1]
        self.assertEqual(entry["action"], ACTION_APPROVE)
        self.assertEqual(entry["actor"], "审批人李")
        self.assertEqual(entry["decided_at"], FIXED_NOW.isoformat())
        self.assertEqual(entry["operation_label"], "批准作业单")

    def test_reject_moves_the_job_to_a_terminal_rejected_state(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        request = build_job_approval_request(job)
        decide_job_approval(
            records,
            HF_JOB_ID,
            ApprovalDecision(
                gate_id=request.gate_id,
                action=ACTION_REJECT,
                note="控制措施不足，退回",
            ),
            actor="审批人李",
            now=FIXED_NOW,
        )
        self.assertEqual(job["status"], JOB_STATUS_REJECTED)
        self.assertEqual(job["approvals"][-1]["action"], ACTION_REJECT)
        self.assertEqual(job["approvals"][-1]["note"], "控制措施不足，退回")

    def test_modify_can_only_change_the_approval_note(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        request = build_job_approval_request(job)
        decide_job_approval(
            records,
            HF_JOB_ID,
            ApprovalDecision(
                gate_id=request.gate_id,
                action=ACTION_MODIFY,
                calls=(
                    {
                        "call_id": request.gate_id,
                        "tool": "approve_job",
                        "arguments": {"note": "附加条件：执行前双人复核"},
                    },
                ),
            ),
            actor="审批人李",
            now=FIXED_NOW,
        )
        self.assertEqual(job["status"], JOB_STATUS_APPROVED)
        entry = job["approvals"][-1]
        self.assertEqual(entry["action"], ACTION_MODIFY)
        self.assertEqual(entry["note"], "附加条件：执行前双人复核")

    def test_modify_cannot_change_risk_or_status(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        request = build_job_approval_request(job)
        with self.assertRaises(ValueError):
            decide_job_approval(
                records,
                HF_JOB_ID,
                ApprovalDecision(
                    gate_id=request.gate_id,
                    action=ACTION_MODIFY,
                    calls=(
                        {
                            "call_id": request.gate_id,
                            "tool": "approve_job",
                            "arguments": {"likelihood": 1, "status": "已批准"},
                        },
                    ),
                ),
                actor="审批人李",
                now=FIXED_NOW,
            )
        self.assertEqual(job["status"], JOB_STATUS_AWAITING_APPROVAL)

    def test_stale_gate_id_is_refused(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        with self.assertRaises(ValueError):
            decide_job_approval(
                records,
                HF_JOB_ID,
                ApprovalDecision(
                    gate_id="job:OTHER:approval", action=ACTION_APPROVE
                ),
                actor="审批人李",
                now=FIXED_NOW,
            )
        self.assertEqual(job["status"], JOB_STATUS_AWAITING_APPROVAL)

    def test_approval_requires_an_actor(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        request = build_job_approval_request(job)
        with self.assertRaises(ValueError):
            decide_job_approval(
                records,
                HF_JOB_ID,
                ApprovalDecision(gate_id=request.gate_id, action=ACTION_APPROVE),
                actor="   ",
                now=FIXED_NOW,
            )
        self.assertEqual(job["status"], JOB_STATUS_AWAITING_APPROVAL)


class ExecutionGateTests(unittest.TestCase):
    def test_execute_requires_approval(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        with self.assertRaises(ValueError):
            start_job_execution(records, HF_JOB_ID, actor="执行人王")
        self.assertEqual(job["status"], JOB_STATUS_AWAITING_APPROVAL)

    def test_approved_job_can_execute(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        request = build_job_approval_request(job)
        decide_job_approval(
            records,
            HF_JOB_ID,
            ApprovalDecision(gate_id=request.gate_id, action=ACTION_APPROVE),
            actor="审批人李",
            now=FIXED_NOW,
        )
        start_job_execution(
            records, HF_JOB_ID, actor="执行人王", note="开始酸洗作业", now=FIXED_NOW
        )
        self.assertEqual(job["status"], JOB_STATUS_EXECUTING)
        last = job["status_history"][-1]
        self.assertEqual(last["from"], JOB_STATUS_APPROVED)
        self.assertEqual(last["to"], JOB_STATUS_EXECUTING)
        self.assertEqual(last["actor"], "执行人王")

    def test_rejected_job_cannot_execute(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        request = build_job_approval_request(job)
        decide_job_approval(
            records,
            HF_JOB_ID,
            ApprovalDecision(
                gate_id=request.gate_id, action=ACTION_REJECT, note="驳回"
            ),
            actor="审批人李",
            now=FIXED_NOW,
        )
        with self.assertRaises(ValueError):
            start_job_execution(records, HF_JOB_ID, actor="执行人王")
        self.assertEqual(job["status"], JOB_STATUS_REJECTED)

    def test_every_status_change_goes_through_the_state_machine(self) -> None:
        records, _ = make_case_records()
        job = draft_and_confirm(records)
        request = build_job_approval_request(job)
        decide_job_approval(
            records,
            HF_JOB_ID,
            ApprovalDecision(gate_id=request.gate_id, action=ACTION_APPROVE),
            actor="审批人李",
            now=FIXED_NOW,
        )
        start_job_execution(records, HF_JOB_ID, actor="执行人王", now=FIXED_NOW)

        transitions = [
            (entry["from"], entry["to"]) for entry in job["status_history"]
        ]
        self.assertEqual(
            transitions,
            [
                ("", JOB_STATUS_DRAFT),
                (JOB_STATUS_DRAFT, JOB_STATUS_AWAITING_EHS),
                (JOB_STATUS_AWAITING_EHS, JOB_STATUS_AWAITING_APPROVAL),
                (JOB_STATUS_AWAITING_APPROVAL, JOB_STATUS_APPROVED),
                (JOB_STATUS_APPROVED, JOB_STATUS_EXECUTING),
            ],
        )
        self.assertTrue(
            all(str(entry["actor"]).strip() for entry in job["status_history"])
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

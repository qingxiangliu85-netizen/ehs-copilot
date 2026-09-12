"""Tests for the P4A closure flow: execution → hazards → rectification → closure."""

from __future__ import annotations

import unittest
from datetime import date, datetime

import hazards
from closure import (
    RECTIFICATION_EVIDENCE_TYPES,
    close_hazard,
    close_job,
    complete_job_execution,
    create_job_hazard,
    draft_hazards_from_jsa,
    review_hazard,
    submit_rectification_evidence,
    update_hazard_rectification,
)
from dashboard import (
    calculate_dashboard_metrics,
    count_overdue_hazards,
    get_job_metrics,
)
from demo_cases import hf_public_evidence
from job_review import (
    attach_public_evidence,
    build_job_approval_request,
    confirm_jsa,
    decide_job_approval,
    draft_jsa,
    start_job_execution,
)
from jobs import (
    JOB_STATUS_AWAITING_REVIEW,
    JOB_STATUS_CLOSED,
    JOB_STATUS_EXECUTING,
    create_job,
    get_job,
)
from workflow.hitl import ACTION_APPROVE, ApprovalDecision


FIXED_TODAY = date(2026, 9, 12)
FIXED_NOW = datetime(2026, 9, 12, 10, 0, 0)
FIXED_STAMP = "2026-09-12T10:00:00"
JOB_ID = "JOB-001"

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

DUE_ON = date(2026, 9, 30)


def confirmed_job() -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    job = create_job(
        job_name="HF酸洗作业（P4A测试）",
        steps=[{"order": 1, "name": "浸洗", "note": "模拟步骤"}],
        created_by="作业申请人（测试）",
        now=FIXED_NOW,
    )
    job_records = [job]
    hazard_records: list[dict[str, object]] = []
    attach_public_evidence(job, hf_public_evidence())
    draft_jsa(job_records, JOB_ID, now=FIXED_NOW)
    confirm_jsa(job_records, JOB_ID, now=FIXED_NOW, **CONFIRM_KWARGS)
    return job_records, hazard_records, job


def approved_job() -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    job_records, hazard_records, job = confirmed_job()
    request = build_job_approval_request(job)
    decide_job_approval(
        job_records,
        JOB_ID,
        ApprovalDecision(gate_id=request.gate_id, action=ACTION_APPROVE),
        actor="审批人李",
        now=FIXED_NOW,
    )
    return job_records, hazard_records, job


def executing_job() -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    job_records, hazard_records, job = approved_job()
    start_job_execution(job_records, JOB_ID, actor="执行人王", now=FIXED_NOW)
    return job_records, hazard_records, job


def add_hazard(
    job_records: list[dict[str, object]],
    hazard_records: list[dict[str, object]],
    *,
    description: str = "HF作业区发现隐患（测试）",
    risk_level: str = "高",
    owner: str = "整改责任人甲",
    approved_by: str = "EHS张",
) -> dict[str, object]:
    return create_job_hazard(
        job_records,
        hazard_records,
        JOB_ID,
        description=description,
        risk_level=risk_level,
        hazard_type="危化品管理",
        owner=owner,
        due_on=DUE_ON,
        corrective_action="完成整改并复核",
        actor="执行人王",
        approved_by=approved_by,
        now=FIXED_NOW,
    )


def fully_rectified_hazard(
    job_records: list[dict[str, object]],
    hazard_records: list[dict[str, object]],
) -> dict[str, object]:
    hazard = add_hazard(job_records, hazard_records)
    hazard_id = str(hazard["隐患编号"])
    submit_rectification_evidence(
        job_records,
        hazard_records,
        JOB_ID,
        hazard_id,
        file_name="整改照片.jpg",
        evidence_type="照片",
        note="整改完成照片",
        uploaded_by="整改责任人甲",
        now=FIXED_NOW,
    )
    review_hazard(
        job_records,
        hazard_records,
        JOB_ID,
        hazard_id,
        reviewer="复查人赵",
        review_note="现场复查通过，整改有效",
        now=FIXED_NOW,
    )
    close_hazard(
        job_records,
        hazard_records,
        JOB_ID,
        hazard_id,
        closed_by="复查人赵",
        now=FIXED_NOW,
    )
    return hazard


class ExecutionManagementTests(unittest.TestCase):
    def test_execute_requires_approval(self) -> None:
        job_records, _, _ = confirmed_job()
        with self.assertRaises(ValueError):
            start_job_execution(job_records, JOB_ID, actor="执行人王")

    def test_start_records_executor_and_start_time(self) -> None:
        _, _, job = executing_job()
        self.assertEqual(job["status"], JOB_STATUS_EXECUTING)
        info = job["execution_info"]
        self.assertEqual(info["executor"], "执行人王")
        self.assertEqual(info["started_at"], FIXED_STAMP)

    def test_complete_records_end_time_and_moves_to_review(self) -> None:
        job_records, _, job = executing_job()
        complete_job_execution(
            job_records,
            JOB_ID,
            actor="执行人王",
            completed_at=FIXED_NOW,
            note="酸洗作业完成",
            now=FIXED_NOW,
        )
        self.assertEqual(job["status"], JOB_STATUS_AWAITING_REVIEW)
        info = job["execution_info"]
        self.assertEqual(info["started_at"], FIXED_STAMP)
        self.assertEqual(info["completed_at"], FIXED_STAMP)
        self.assertEqual(info["completed_by"], "执行人王")
        self.assertEqual(info["completion_note"], "酸洗作业完成")
        last = job["status_history"][-1]
        self.assertEqual(last["from"], JOB_STATUS_EXECUTING)
        self.assertEqual(last["to"], JOB_STATUS_AWAITING_REVIEW)

    def test_complete_requires_executing_status(self) -> None:
        job_records, _, job = approved_job()
        with self.assertRaises(ValueError):
            complete_job_execution(job_records, JOB_ID, actor="执行人王")
        self.assertEqual(job["status"], "已批准")

    def test_complete_cannot_be_repeated(self) -> None:
        job_records, _, _ = executing_job()
        complete_job_execution(job_records, JOB_ID, actor="执行人王", now=FIXED_NOW)
        with self.assertRaises(ValueError):
            complete_job_execution(job_records, JOB_ID, actor="执行人王", now=FIXED_NOW)


class LinkedHazardTests(unittest.TestCase):
    def test_drafts_from_jsa_are_anchored_and_unassigned(self) -> None:
        _, _, job = executing_job()
        drafts = draft_hazards_from_jsa(job)
        self.assertTrue(drafts)
        for draft in drafts:
            with self.subTest(description=draft["description"][:30]):
                self.assertTrue(draft["description"])
                self.assertEqual(draft["risk_level"], "")
                self.assertEqual(draft["owner"], "")
                self.assertEqual(draft["due_on"], "")
                self.assertTrue(draft["needs_review"])
                self.assertEqual(draft["status"], "draft")
                if draft["source_evidence"]["evidence_track"] == "public_sources":
                    self.assertTrue(draft["source_evidence"]["source_url"])

    def test_create_hazard_requires_human_approval(self) -> None:
        job_records, hazard_records, _ = executing_job()
        with self.assertRaises(ValueError):
            create_job_hazard(
                job_records,
                hazard_records,
                JOB_ID,
                description="未审批的隐患",
                risk_level="高",
                actor="执行人王",
                approved_by="",
                now=FIXED_NOW,
            )
        self.assertEqual(hazard_records, [])
        self.assertEqual(job_records[0]["linked_hazard_ids"], [])

    def test_create_hazard_links_both_sides(self) -> None:
        job_records, hazard_records, job = executing_job()
        hazard = add_hazard(job_records, hazard_records)
        self.assertEqual(len(hazard_records), 1)
        self.assertEqual(hazard["related_job_id"], JOB_ID)
        self.assertEqual(job["linked_hazard_ids"], [hazard["隐患编号"]])
        self.assertEqual(hazard["状态"], "待整改")
        entry = job["approvals"][-1]
        self.assertEqual(entry["action"], ACTION_APPROVE)
        self.assertEqual(entry["actor"], "EHS张")
        self.assertIn("write_requires_approval", entry["guard_codes"])

    def test_major_risk_hazard_keeps_major_guardrail_code(self) -> None:
        job_records, hazard_records, job = executing_job()
        add_hazard(job_records, hazard_records, risk_level="重大")
        codes = job["approvals"][-1]["guard_codes"]
        self.assertIn("major_risk_write_requires_approval", codes)

    def test_create_hazard_requires_a_working_job_status(self) -> None:
        job_records, hazard_records, job = approved_job()
        with self.assertRaises(ValueError):
            add_hazard(job_records, hazard_records)
        self.assertEqual(job["status"], "已批准")
        self.assertEqual(hazard_records, [])

    def test_risk_level_must_be_valid(self) -> None:
        job_records, hazard_records, _ = executing_job()
        with self.assertRaises(ValueError):
            add_hazard(job_records, hazard_records, risk_level="特高")

    def test_generated_from_is_preserved(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = create_job_hazard(
            job_records,
            hazard_records,
            JOB_ID,
            description="由 JSA 危害生成的隐患",
            risk_level="高",
            actor="执行人王",
            approved_by="EHS张",
            generated_from={
                "source": "jsa_draft",
                "evidence_id": "hf-hazard-symptoms",
            },
            now=FIXED_NOW,
        )
        self.assertEqual(hazard["generated_from"]["source"], "jsa_draft")
        self.assertEqual(
            hazard["generated_from"]["evidence_id"], "hf-hazard-symptoms"
        )


class RectificationTests(unittest.TestCase):
    def test_assign_owner_due_and_note(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = add_hazard(job_records, hazard_records)
        hazard_id = str(hazard["隐患编号"])
        updated = update_hazard_rectification(
            job_records,
            hazard_records,
            JOB_ID,
            hazard_id,
            actor="EHS张",
            owner="整改责任人乙",
            due_on=date(2026, 10, 10),
            corrective_action="更换防泄漏托盘并验收",
            rectification_note="已明确整改方案",
            now=FIXED_NOW,
        )
        self.assertEqual(updated["责任人"], "整改责任人乙")
        self.assertEqual(updated["整改期限"], "2026-10-10")
        self.assertEqual(updated["整改措施"], "更换防泄漏托盘并验收")
        self.assertEqual(updated["rectification_note"], "已明确整改方案")
        self.assertEqual(updated["rectification_updated_by"], "EHS张")
        self.assertEqual(updated["rectification_updated_at"], FIXED_STAMP)

    def test_update_requires_at_least_one_field(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = add_hazard(job_records, hazard_records)
        with self.assertRaises(ValueError):
            update_hazard_rectification(
                job_records,
                hazard_records,
                JOB_ID,
                str(hazard["隐患编号"]),
                actor="EHS张",
            )

    def test_submit_evidence_appends_and_moves_to_in_progress(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = add_hazard(job_records, hazard_records)
        hazard_id = str(hazard["隐患编号"])
        updated = submit_rectification_evidence(
            job_records,
            hazard_records,
            JOB_ID,
            hazard_id,
            file_name="托盘更换验收照片.jpg",
            evidence_type="照片",
            note="更换完成",
            uploaded_by="整改责任人甲",
            now=FIXED_NOW,
        )
        evidence = updated["rectification_evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["file_name"], "托盘更换验收照片.jpg")
        self.assertEqual(evidence[0]["evidence_type"], "照片")
        self.assertEqual(evidence[0]["note"], "更换完成")
        self.assertEqual(evidence[0]["uploaded_by"], "整改责任人甲")
        self.assertEqual(evidence[0]["uploaded_at"], FIXED_STAMP)
        self.assertEqual(updated["状态"], "整改中")

    def test_evidence_requires_file_and_uploader(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = add_hazard(job_records, hazard_records)
        hazard_id = str(hazard["隐患编号"])
        with self.assertRaises(ValueError):
            submit_rectification_evidence(
                job_records, hazard_records, JOB_ID, hazard_id,
                file_name="  ", uploaded_by="整改责任人甲", now=FIXED_NOW,
            )
        with self.assertRaises(ValueError):
            submit_rectification_evidence(
                job_records, hazard_records, JOB_ID, hazard_id,
                file_name="照片.jpg", uploaded_by="", now=FIXED_NOW,
            )

    def test_evidence_rejects_unknown_type(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = add_hazard(job_records, hazard_records)
        with self.assertRaises(ValueError):
            submit_rectification_evidence(
                job_records,
                hazard_records,
                JOB_ID,
                str(hazard["隐患编号"]),
                file_name="视频.mp4",
                evidence_type="视频",
                uploaded_by="整改责任人甲",
                now=FIXED_NOW,
            )
        self.assertIn("照片", RECTIFICATION_EVIDENCE_TYPES)

    def test_hazard_must_belong_to_the_job(self) -> None:
        job_records, hazard_records, _ = executing_job()
        foreign = hazards.create_hazard_record(
            hazard_id="HZ-900",
            description="其他作业的隐患（测试）",
            hazard_type="其他",
            risk_level="中",
            owner="责任人",
            found_on=FIXED_TODAY,
            due_on=DUE_ON,
            corrective_action="整改",
            status="待整改",
            related_job_id="JOB-OTHER",
        )
        hazard_records.append(foreign)
        with self.assertRaises(ValueError):
            update_hazard_rectification(
                job_records,
                hazard_records,
                JOB_ID,
                "HZ-900",
                actor="EHS张",
                owner="不应生效",
                now=FIXED_NOW,
            )


class ReviewCloseTests(unittest.TestCase):
    def test_review_requires_reviewer_and_note(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = add_hazard(job_records, hazard_records)
        hazard_id = str(hazard["隐患编号"])
        with self.assertRaises(ValueError):
            review_hazard(
                job_records, hazard_records, JOB_ID, hazard_id,
                reviewer="  ", review_note="意见", now=FIXED_NOW,
            )
        with self.assertRaises(ValueError):
            review_hazard(
                job_records, hazard_records, JOB_ID, hazard_id,
                reviewer="复查人赵", review_note="  ", now=FIXED_NOW,
            )

    def test_close_without_evidence_is_refused(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = add_hazard(job_records, hazard_records)
        hazard_id = str(hazard["隐患编号"])
        review_hazard(
            job_records, hazard_records, JOB_ID, hazard_id,
            reviewer="复查人赵", review_note="现场复查通过", now=FIXED_NOW,
        )
        with self.assertRaises(ValueError):
            close_hazard(job_records, hazard_records, JOB_ID, hazard_id, now=FIXED_NOW)
        self.assertEqual(hazard["状态"], "待整改")

    def test_close_without_reviewer_is_refused(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = add_hazard(job_records, hazard_records)
        hazard_id = str(hazard["隐患编号"])
        submit_rectification_evidence(
            job_records, hazard_records, JOB_ID, hazard_id,
            file_name="照片.jpg", uploaded_by="整改责任人甲", now=FIXED_NOW,
        )
        with self.assertRaises(ValueError):
            close_hazard(job_records, hazard_records, JOB_ID, hazard_id, now=FIXED_NOW)

    def test_close_without_review_note_is_refused(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = add_hazard(job_records, hazard_records)
        hazard_id = str(hazard["隐患编号"])
        submit_rectification_evidence(
            job_records, hazard_records, JOB_ID, hazard_id,
            file_name="照片.jpg", uploaded_by="整改责任人甲", now=FIXED_NOW,
        )
        hazards.update_hazard_record(hazard_records, hazard_id, {"reviewer": "复查人赵"})
        with self.assertRaises(ValueError):
            close_hazard(job_records, hazard_records, JOB_ID, hazard_id, now=FIXED_NOW)

    def test_close_records_reviewer_and_time(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = fully_rectified_hazard(job_records, hazard_records)
        self.assertEqual(hazard["状态"], "已关闭")
        self.assertEqual(hazard["reviewer"], "复查人赵")
        self.assertEqual(hazard["review_date"], "2026-09-12")
        self.assertEqual(hazard["review_note"], "现场复查通过，整改有效")
        self.assertEqual(hazard["closed_by"], "复查人赵")
        self.assertEqual(hazard["closed_at"], "2026-09-12")
        self.assertEqual(hazard["closed_at_timestamp"], FIXED_STAMP)
        self.assertGreaterEqual(len(hazard["rectification_evidence"]), 1)

    def test_closed_hazard_cannot_be_closed_again(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = fully_rectified_hazard(job_records, hazard_records)
        with self.assertRaises(ValueError):
            close_hazard(
                job_records, hazard_records, JOB_ID,
                str(hazard["隐患编号"]), now=FIXED_NOW,
            )
        with self.assertRaises(ValueError):
            update_hazard_rectification(
                job_records, hazard_records, JOB_ID,
                str(hazard["隐患编号"]), actor="EHS张", owner="X", now=FIXED_NOW,
            )


class JobClosureTests(unittest.TestCase):
    def test_cannot_close_job_while_executing(self) -> None:
        job_records, hazard_records, job = executing_job()
        with self.assertRaises(ValueError):
            close_job(job_records, hazard_records, JOB_ID, actor="EHS张", now=FIXED_NOW)
        self.assertEqual(job["status"], JOB_STATUS_EXECUTING)

    def test_cannot_close_job_with_open_hazards(self) -> None:
        job_records, hazard_records, job = executing_job()
        first = add_hazard(job_records, hazard_records, description="隐患一")
        fully_rectified_hazard(job_records, hazard_records)
        complete_job_execution(job_records, JOB_ID, actor="执行人王", now=FIXED_NOW)
        with self.assertRaises(ValueError) as context:
            close_job(job_records, hazard_records, JOB_ID, actor="EHS张", now=FIXED_NOW)
        self.assertIn(str(first["隐患编号"]), str(context.exception))
        self.assertEqual(job["status"], JOB_STATUS_AWAITING_REVIEW)

    def test_can_close_job_after_all_hazards_closed(self) -> None:
        job_records, hazard_records, job = executing_job()
        first = fully_rectified_hazard(job_records, hazard_records)
        second = fully_rectified_hazard(job_records, hazard_records)
        complete_job_execution(job_records, JOB_ID, actor="执行人王", now=FIXED_NOW)
        close_job(
            job_records, hazard_records, JOB_ID,
            actor="EHS张", note="全部整改闭环", now=FIXED_NOW,
        )
        self.assertEqual(job["status"], JOB_STATUS_CLOSED)
        self.assertEqual(
            set(job["linked_hazard_ids"]),
            {first["隐患编号"], second["隐患编号"]},
        )
        self.assertEqual(job["closure_info"]["closed_by"], "EHS张")
        self.assertEqual(job["closure_info"]["closed_at"], FIXED_STAMP)
        self.assertTrue(job["closure_info"]["all_hazards_closed"])

    def test_job_without_linked_hazards_can_still_close(self) -> None:
        job_records, hazard_records, job = executing_job()
        complete_job_execution(job_records, JOB_ID, actor="执行人王", now=FIXED_NOW)
        close_job(job_records, hazard_records, JOB_ID, actor="EHS张", now=FIXED_NOW)
        self.assertEqual(job["status"], JOB_STATUS_CLOSED)

    def test_missing_linked_hazard_blocks_closure(self) -> None:
        job_records, hazard_records, _ = executing_job()
        hazard = add_hazard(job_records, hazard_records)
        hazard_records.remove(hazard)
        complete_job_execution(job_records, JOB_ID, actor="执行人王", now=FIXED_NOW)
        with self.assertRaises(ValueError):
            close_job(job_records, hazard_records, JOB_ID, actor="EHS张", now=FIXED_NOW)


class DashboardSyncTests(unittest.TestCase):
    def test_job_metrics_follow_the_lifecycle(self) -> None:
        job_records, _, job = executing_job()
        metrics = get_job_metrics(job_records)
        self.assertEqual(metrics["job_active"], 1)
        self.assertEqual(metrics["job_executing"], 1)
        self.assertEqual(metrics["job_awaiting_review"], 0)
        self.assertEqual(metrics["job_closed"], 0)
        self.assertEqual(metrics["job_completion_rate"], 0.0)

        complete_job_execution(job_records, JOB_ID, actor="执行人王", now=FIXED_NOW)
        metrics = get_job_metrics(job_records)
        self.assertEqual(metrics["job_executing"], 0)
        self.assertEqual(metrics["job_awaiting_review"], 1)

        close_job(job_records, [], JOB_ID, actor="EHS张", now=FIXED_NOW)
        metrics = get_job_metrics(job_records)
        self.assertEqual(metrics["job_awaiting_review"], 0)
        self.assertEqual(metrics["job_closed"], 1)
        self.assertEqual(metrics["job_completion_rate"], 100.0)

    def test_job_metrics_count_awaiting_approval(self) -> None:
        job_records, _, _ = confirmed_job()
        metrics = get_job_metrics(job_records)
        self.assertEqual(metrics["job_awaiting_approval"], 1)
        self.assertEqual(metrics["job_active"], 1)

    def test_overdue_hazard_count(self) -> None:
        rows = [
            {"隐患编号": "HZ-1", "整改期限": "2026-09-01", "状态": "待整改"},
            {"隐患编号": "HZ-2", "整改期限": "2026-09-20", "状态": "整改中"},
            {"隐患编号": "HZ-3", "整改期限": "2026-09-01", "状态": "已关闭"},
            {"隐患编号": "HZ-4", "整改期限": "", "状态": "待整改"},
        ]
        self.assertEqual(count_overdue_hazards(rows, today=FIXED_TODAY), 1)

    def test_metrics_keep_legacy_keys_and_default_zero_jobs(self) -> None:
        metrics = calculate_dashboard_metrics([], [], [])
        for key in (
            "jsa_high_major",
            "hazard_pending",
            "hazard_closed",
            "completion_rate",
            "jsa_total",
            "hazard_total",
            "hazard_overdue",
            "job_active",
            "job_awaiting_approval",
            "job_executing",
            "job_awaiting_review",
            "job_closed",
            "job_completion_rate",
        ):
            self.assertIn(key, metrics)
        legacy = calculate_dashboard_metrics([], [])
        self.assertEqual(legacy["job_total"], 0)
        self.assertEqual(legacy["job_closed"], 0)

    def test_full_flow_updates_dashboard(self) -> None:
        job_records, hazard_records, _ = executing_job()
        fully_rectified_hazard(job_records, hazard_records)
        fully_rectified_hazard(job_records, hazard_records)
        complete_job_execution(job_records, JOB_ID, actor="执行人王", now=FIXED_NOW)
        close_job(job_records, hazard_records, JOB_ID, actor="EHS张", now=FIXED_NOW)

        metrics = calculate_dashboard_metrics(
            [], hazard_records, job_records, today=FIXED_TODAY
        )
        self.assertEqual(metrics["job_closed"], 1)
        self.assertEqual(metrics["job_completion_rate"], 100.0)
        self.assertEqual(metrics["hazard_total"], 2)
        self.assertEqual(metrics["hazard_closed"], 2)
        self.assertEqual(metrics["completion_rate"], 100.0)
        self.assertEqual(metrics["hazard_overdue"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

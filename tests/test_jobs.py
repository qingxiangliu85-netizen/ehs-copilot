"""Unit tests for the V4 job (作业单) domain core and its status machine."""

from __future__ import annotations

import copy
import unittest
from datetime import date, datetime

from hazards import (
    HAZARD_OPTIONAL_FIELDS,
    create_hazard_record,
    hazards_to_csv,
    update_hazard_record,
    validate_hazard_record,
)
from jobs import (
    DEFAULT_JOB_TYPE,
    JOB_FIELDS,
    JOB_STATUSES,
    JOB_STATUS_APPROVED,
    JOB_STATUS_AWAITING_APPROVAL,
    JOB_STATUS_AWAITING_EHS,
    JOB_STATUS_AWAITING_REVIEW,
    JOB_STATUS_CLOSED,
    JOB_STATUS_DRAFT,
    JOB_STATUS_EXECUTING,
    JOB_STATUS_REJECTED,
    can_transition,
    create_job,
    get_job,
    job_summary,
    next_job_id,
    transition_job,
)


FIXED_NOW = datetime(2026, 9, 12, 10, 0, 0)
FIXED_STAMP = "2026-09-12T10:00:00"

LIFECYCLE: tuple[tuple[str, str, str], ...] = (
    (JOB_STATUS_AWAITING_EHS, "EHS张", "提交EHS确认"),
    (JOB_STATUS_AWAITING_APPROVAL, "EHS张", "确认风险与控制措施"),
    (JOB_STATUS_APPROVED, "审批人李", "批准作业"),
    (JOB_STATUS_EXECUTING, "执行人王", "开始执行"),
    (JOB_STATUS_AWAITING_REVIEW, "执行人王", "执行完成待复查"),
    (JOB_STATUS_CLOSED, "复查人赵", "复查通过并关闭"),
)


def make_job(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "job_name": "HF酸洗作业（测试）",
        "created_by": "EHS张",
        "now": FIXED_NOW,
    }
    values.update(overrides)
    return create_job(**values)  # type: ignore[arg-type]


def make_hazard(
    hazard_id: str = "HZ-001",
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "hazard_id": hazard_id,
        "description": "HF储罐区未设置专用冲淋装置",
        "hazard_type": "危化品管理",
        "risk_level": "高",
        "owner": "EHS张",
        "found_on": date(2026, 9, 1),
        "due_on": date(2026, 9, 15),
        "corrective_action": "加装冲淋装置并验收",
        "status": "待整改",
    }
    values.update(overrides)
    return create_hazard_record(**values)  # type: ignore[arg-type]


class JobIdTests(unittest.TestCase):
    def test_next_job_id_starts_at_001(self) -> None:
        self.assertEqual(next_job_id([]), "JOB-001")

    def test_next_job_id_uses_the_highest_existing_number(self) -> None:
        records = [{"job_id": "JOB-001"}, {"job_id": "JOB-007"}]
        self.assertEqual(next_job_id(records), "JOB-008")

    def test_next_job_id_ignores_unrelated_identifiers(self) -> None:
        records = [{"job_id": "HZ-009"}, {"job_id": "JOB-A"}, {"job_id": ""}]
        self.assertEqual(next_job_id(records), "JOB-001")


class JobCreationTests(unittest.TestCase):
    def test_create_job_builds_the_documented_record_shape(self) -> None:
        record = make_job()
        self.assertEqual(set(JOB_FIELDS), set(record))
        self.assertEqual(record["job_id"], "JOB-001")
        self.assertEqual(record["job_name"], "HF酸洗作业（测试）")
        self.assertEqual(record["job_type"], DEFAULT_JOB_TYPE)
        self.assertEqual(record["status"], JOB_STATUS_DRAFT)
        self.assertEqual(record["created_at"], FIXED_STAMP)
        self.assertEqual(record["updated_at"], FIXED_STAMP)
        self.assertEqual(record["jsa_draft"], None)
        self.assertEqual(record["jsa_confirmation"], None)
        self.assertEqual(record["chemicals"], [])
        self.assertEqual(record["sds_evidence"], [])
        self.assertEqual(record["approvals"], [])
        self.assertEqual(record["execution_info"], {})
        self.assertEqual(record["linked_hazard_ids"], [])
        self.assertEqual(record["attachments"], [])
        self.assertEqual(record["closure_info"], {})

    def test_create_job_records_the_creator_in_status_history(self) -> None:
        record = make_job()
        history = record["status_history"]
        self.assertEqual(len(history), 1)
        entry = history[0]
        self.assertEqual(entry["from"], "")
        self.assertEqual(entry["to"], JOB_STATUS_DRAFT)
        self.assertEqual(entry["actor"], "EHS张")
        self.assertEqual(entry["at"], FIXED_STAMP)

    def test_create_job_generates_sequential_ids(self) -> None:
        jobs = [
            make_job(existing_ids=("JOB-001", "JOB-002")),
            make_job(
                job_name="第二个作业",
                existing_ids=("JOB-001", "JOB-002", "JOB-003"),
            ),
        ]
        self.assertEqual(jobs[0]["job_id"], "JOB-003")
        self.assertEqual(jobs[1]["job_id"], "JOB-004")

    def test_create_job_rejects_duplicate_id(self) -> None:
        with self.assertRaises(ValueError):
            make_job(job_id="JOB-001", existing_ids=("JOB-001",))

    def test_create_job_rejects_missing_name_or_type(self) -> None:
        with self.assertRaises(ValueError):
            make_job(job_name="   ")
        with self.assertRaises(ValueError):
            make_job(job_type="")

    def test_create_job_normalises_chemicals(self) -> None:
        record = make_job(
            chemicals=[
                {
                    "name": " 氢氟酸 ",
                    "sds_file": "HF_SDS.pdf",
                    "aliases": ["HF", "", "hydrofluoric"],
                }
            ]
        )
        self.assertEqual(
            record["chemicals"],
            [
                {
                    "name": "氢氟酸",
                    "sds_file": "HF_SDS.pdf",
                    "aliases": ["HF", "hydrofluoric"],
                }
            ],
        )

    def test_create_job_rejects_invalid_chemicals(self) -> None:
        with self.assertRaises(ValueError):
            make_job(chemicals=[{"sds_file": "HF_SDS.pdf"}])
        with self.assertRaises(ValueError):
            make_job(chemicals=["氢氟酸"])
        with self.assertRaises(ValueError):
            make_job(chemicals=[{"name": "氢氟酸", "aliases": "HF"}])

    def test_create_job_copies_input_collections(self) -> None:
        chemicals = [{"name": "氢氟酸", "aliases": ["HF"]}]
        evidence = [{"source": "HF_SDS.pdf", "page": 2}]
        record = make_job(chemicals=chemicals, sds_evidence=evidence)
        chemicals[0]["name"] = "被外部修改"
        evidence[0]["page"] = 99
        self.assertEqual(record["chemicals"][0]["name"], "氢氟酸")
        self.assertEqual(record["sds_evidence"][0]["page"], 2)

    def test_create_job_rejects_non_mapping_jsa_draft(self) -> None:
        with self.assertRaises(ValueError):
            make_job(jsa_draft=["not-a-mapping"])

    def test_get_job_returns_the_live_record_or_none(self) -> None:
        jobs = [make_job()]
        self.assertIs(get_job(jobs, "JOB-001"), jobs[0])
        self.assertIsNone(get_job(jobs, "JOB-999"))
        self.assertIsNone(get_job(jobs, "  "))


class JobTransitionTests(unittest.TestCase):
    def test_the_full_legal_lifecycle_is_allowed(self) -> None:
        jobs = [make_job()]
        for status, actor, note in LIFECYCLE:
            with self.subTest(status=status):
                transition_job(
                    jobs, "JOB-001", status, actor=actor, note=note, now=FIXED_NOW
                )
                self.assertEqual(jobs[0]["status"], status)
        self.assertEqual(jobs[0]["status"], JOB_STATUS_CLOSED)
        self.assertEqual(len(jobs[0]["status_history"]), len(LIFECYCLE) + 1)

    def test_can_transition_is_pure_and_exact(self) -> None:
        self.assertTrue(can_transition(JOB_STATUS_DRAFT, JOB_STATUS_AWAITING_EHS))
        self.assertTrue(can_transition(JOB_STATUS_AWAITING_EHS, JOB_STATUS_DRAFT))
        self.assertTrue(
            can_transition(JOB_STATUS_AWAITING_APPROVAL, JOB_STATUS_REJECTED)
        )
        self.assertFalse(can_transition(JOB_STATUS_DRAFT, JOB_STATUS_APPROVED))
        self.assertFalse(can_transition(JOB_STATUS_CLOSED, JOB_STATUS_DRAFT))
        self.assertFalse(can_transition("未知状态", JOB_STATUS_DRAFT))
        self.assertFalse(can_transition(JOB_STATUS_DRAFT, "未知状态"))
        self.assertEqual(len(JOB_STATUSES), 8)

    def test_transition_records_actor_note_and_time(self) -> None:
        jobs = [make_job()]
        transition_job(
            jobs,
            "JOB-001",
            JOB_STATUS_AWAITING_EHS,
            actor="EHS张",
            note="字段已核对",
            now=FIXED_NOW,
        )
        entry = jobs[0]["status_history"][-1]
        self.assertEqual(
            entry,
            {
                "from": JOB_STATUS_DRAFT,
                "to": JOB_STATUS_AWAITING_EHS,
                "actor": "EHS张",
                "note": "字段已核对",
                "at": FIXED_STAMP,
            },
        )
        self.assertEqual(jobs[0]["updated_at"], FIXED_STAMP)

    def test_return_to_draft_is_allowed_before_approval(self) -> None:
        jobs = [make_job()]
        transition_job(
            jobs, "JOB-001", JOB_STATUS_AWAITING_EHS, actor="EHS张", now=FIXED_NOW
        )
        transition_job(
            jobs,
            "JOB-001",
            JOB_STATUS_DRAFT,
            actor="EHS张",
            note="补充化学品清单后重提",
            now=FIXED_NOW,
        )
        self.assertEqual(jobs[0]["status"], JOB_STATUS_DRAFT)
        self.assertTrue(
            can_transition(jobs[0]["status"], JOB_STATUS_AWAITING_EHS)
        )

    def test_rejection_is_allowed_and_terminal(self) -> None:
        jobs = [make_job()]
        transition_job(
            jobs, "JOB-001", JOB_STATUS_AWAITING_EHS, actor="EHS张", now=FIXED_NOW
        )
        transition_job(
            jobs, "JOB-001", JOB_STATUS_AWAITING_APPROVAL, actor="EHS张", now=FIXED_NOW
        )
        transition_job(
            jobs,
            "JOB-001",
            JOB_STATUS_REJECTED,
            actor="审批人李",
            note="控制措施不足",
            now=FIXED_NOW,
        )
        self.assertEqual(jobs[0]["status"], JOB_STATUS_REJECTED)
        self.assertFalse(can_transition(JOB_STATUS_REJECTED, JOB_STATUS_APPROVED))
        self.assertFalse(can_transition(JOB_STATUS_REJECTED, JOB_STATUS_DRAFT))

    def test_closed_is_terminal(self) -> None:
        jobs = [make_job()]
        for status, actor, note in LIFECYCLE:
            transition_job(jobs, "JOB-001", status, actor=actor, note=note)
        self.assertFalse(can_transition(JOB_STATUS_CLOSED, JOB_STATUS_EXECUTING))
        self.assertFalse(can_transition(JOB_STATUS_CLOSED, JOB_STATUS_AWAITING_REVIEW))

    def test_illegal_transitions_are_rejected(self) -> None:
        cases = (
            (JOB_STATUS_DRAFT, JOB_STATUS_AWAITING_APPROVAL),
            (JOB_STATUS_DRAFT, JOB_STATUS_APPROVED),
            (JOB_STATUS_DRAFT, JOB_STATUS_CLOSED),
            (JOB_STATUS_AWAITING_EHS, JOB_STATUS_APPROVED),
            (JOB_STATUS_AWAITING_EHS, JOB_STATUS_REJECTED),
            (JOB_STATUS_AWAITING_APPROVAL, JOB_STATUS_DRAFT),
            (JOB_STATUS_AWAITING_APPROVAL, JOB_STATUS_EXECUTING),
            (JOB_STATUS_APPROVED, JOB_STATUS_CLOSED),
            (JOB_STATUS_EXECUTING, JOB_STATUS_CLOSED),
            (JOB_STATUS_AWAITING_REVIEW, JOB_STATUS_AWAITING_REVIEW),
        )
        for start, target in cases:
            with self.subTest(start=start, target=target):
                jobs = [make_job()]
                if start != JOB_STATUS_DRAFT:
                    start_index = next(
                        index
                        for index, item in enumerate(LIFECYCLE)
                        if item[0] == start
                    )
                    for status, actor, note in LIFECYCLE[: start_index + 1]:
                        transition_job(
                            jobs, "JOB-001", status, actor=actor, note=note
                        )
                self.assertEqual(jobs[0]["status"], start)
                before = copy.deepcopy(jobs[0])
                with self.assertRaises(ValueError):
                    transition_job(jobs, "JOB-001", target, actor="测试人")
                self.assertEqual(jobs[0], before)

    def test_transition_rejects_unknown_status(self) -> None:
        jobs = [make_job()]
        with self.assertRaises(ValueError):
            transition_job(jobs, "JOB-001", "已验收", actor="测试人")
        self.assertEqual(jobs[0]["status"], JOB_STATUS_DRAFT)

    def test_transition_requires_an_actor(self) -> None:
        jobs = [make_job()]
        with self.assertRaises(ValueError):
            transition_job(
                jobs, "JOB-001", JOB_STATUS_AWAITING_EHS, actor="   "
            )
        self.assertEqual(jobs[0]["status"], JOB_STATUS_DRAFT)
        self.assertEqual(len(jobs[0]["status_history"]), 1)

    def test_transition_requires_an_existing_job(self) -> None:
        jobs = [make_job()]
        with self.assertRaises(KeyError):
            transition_job(
                jobs, "JOB-999", JOB_STATUS_AWAITING_EHS, actor="测试人"
            )


class JobSummaryTests(unittest.TestCase):
    def test_empty_records_summary_is_zeroed(self) -> None:
        summary = job_summary([])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["active"], 0)
        self.assertEqual(summary["closed"], 0)
        self.assertEqual(summary["rejected"], 0)
        self.assertEqual(summary["completion_rate"], 0.0)
        self.assertEqual(summary["linked_hazard_total"], 0)
        self.assertEqual(
            summary["status_distribution"],
            {status: 0 for status in JOB_STATUSES},
        )

    def test_summary_counts_statuses_and_completion_rate(self) -> None:
        jobs = [
            make_job(job_id=f"JOB-{index:03d}")
            for index in range(1, 5)
        ]

        closed = jobs[0]
        for status, actor, note in LIFECYCLE:
            transition_job(
                [closed], closed["job_id"], status, actor=actor, note=note
            )

        rejected = jobs[1]
        transition_job(
            [rejected],
            rejected["job_id"],
            JOB_STATUS_AWAITING_EHS,
            actor="EHS张",
        )
        transition_job(
            [rejected],
            rejected["job_id"],
            JOB_STATUS_AWAITING_APPROVAL,
            actor="EHS张",
        )
        transition_job(
            [rejected],
            rejected["job_id"],
            JOB_STATUS_REJECTED,
            actor="审批人李",
        )

        transition_job(
            [jobs[2]],
            jobs[2]["job_id"],
            JOB_STATUS_AWAITING_EHS,
            actor="EHS张",
        )
        transition_job(
            [jobs[2]],
            jobs[2]["job_id"],
            JOB_STATUS_AWAITING_APPROVAL,
            actor="EHS张",
        )

        summary = job_summary(jobs)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["closed"], 1)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["active"], 2)
        self.assertEqual(summary["awaiting_approval"], 1)
        self.assertEqual(summary["awaiting_review"], 0)
        self.assertEqual(summary["completion_rate"], 25.0)
        self.assertEqual(summary["status_distribution"][JOB_STATUS_DRAFT], 1)

    def test_summary_counts_linked_hazard_ids(self) -> None:
        first = make_job()
        second = make_job(job_name="第二个作业")
        first["linked_hazard_ids"] = ["HZ-001", "HZ-002"]
        second["linked_hazard_ids"] = ["HZ-003"]
        summary = job_summary([first, second])
        self.assertEqual(summary["linked_hazard_total"], 3)


class HazardJobCompatibilityTests(unittest.TestCase):
    def test_optional_hazard_fields_are_declared(self) -> None:
        self.assertEqual(
            HAZARD_OPTIONAL_FIELDS,
            (
                "related_job_id",
                "rectification_evidence",
                "reviewer",
                "review_date",
                "review_note",
                "closed_by",
                "closed_at",
            ),
        )

    def test_create_hazard_with_link_evidence_and_review(self) -> None:
        record = make_hazard(
            related_job_id="JOB-001",
            rectification_evidence=[
                {"file_name": "冲淋装置验收照片.jpg", "kind": "照片"}
            ],
            reviewer="复查人赵",
            review_date=date(2026, 9, 14),
            review_note="整改有效",
            closed_by="复查人赵",
            closed_at=datetime(2026, 9, 14, 16, 30, 0),
            status="已关闭",
        )
        self.assertEqual(record["related_job_id"], "JOB-001")
        self.assertEqual(record["reviewer"], "复查人赵")
        self.assertEqual(record["review_date"], "2026-09-14")
        self.assertEqual(record["closed_at"], "2026-09-14")
        self.assertEqual(record["review_note"], "整改有效")
        self.assertEqual(len(record["rectification_evidence"]), 1)
        self.assertEqual(validate_hazard_record(record), ())

    def test_create_hazard_without_optional_fields_stays_v3_shaped(self) -> None:
        record = make_hazard()
        for field in HAZARD_OPTIONAL_FIELDS:
            self.assertNotIn(field, record)

    def test_create_hazard_rejects_invalid_evidence(self) -> None:
        with self.assertRaises(ValueError):
            make_hazard(rectification_evidence=["照片.jpg"])

    def test_update_hazard_records_review_and_closure(self) -> None:
        records = [make_hazard()]
        updated = update_hazard_record(
            records,
            "HZ-001",
            {
                "related_job_id": "JOB-001",
                "rectification_evidence": [{"file_name": "验收单.pdf"}],
                "reviewer": "复查人赵",
                "review_date": date(2026, 9, 14),
                "review_note": "现场复查通过",
                "closed_by": "复查人赵",
                "closed_at": "2026-09-14",
                "状态": "已关闭",
            },
        )
        self.assertEqual(updated["状态"], "已关闭")
        self.assertEqual(updated["related_job_id"], "JOB-001")
        self.assertEqual(updated["review_date"], "2026-09-14")
        self.assertEqual(updated["closed_at"], "2026-09-14")
        self.assertEqual(validate_hazard_record(records[0]), ())
        self.assertIn("rectification_evidence", records[0])

    def test_hazard_csv_includes_optional_columns_only_when_present(self) -> None:
        plain_payload = hazards_to_csv([make_hazard()]).decode("utf-8-sig")
        self.assertNotIn("related_job_id", plain_payload)

        linked = make_hazard(related_job_id="JOB-001")
        linked_payload = hazards_to_csv([linked]).decode("utf-8-sig")
        self.assertIn("related_job_id", linked_payload)
        self.assertIn("JOB-001", linked_payload)

    def test_job_linked_hazard_ids_match_hazard_related_job_id(self) -> None:
        jobs = [make_job()]
        hazard = make_hazard(related_job_id=jobs[0]["job_id"])
        jobs[0]["linked_hazard_ids"].append(hazard["隐患编号"])
        jobs[0]["linked_hazard_ids"].append("HZ-002")

        self.assertEqual(hazard["related_job_id"], get_job(jobs, "JOB-001")["job_id"])
        self.assertEqual(job_summary(jobs)["linked_hazard_total"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

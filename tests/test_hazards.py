"""Unit tests for session-only hazard corrective-action helpers."""

from __future__ import annotations

import unittest
from datetime import date

from hazards import (
    DEMO_DATA_LABEL,
    calculate_completion_rate,
    calculate_hazard_summary,
    create_demo_hazard_records,
    create_hazard_record,
    hazards_to_csv,
    update_hazard_record,
    validate_hazard_record,
)


def make_record(hazard_id: str = "HZ-001", status: str = "待整改") -> dict[str, object]:
    return create_hazard_record(
        hazard_id=hazard_id,
        description="测试隐患",
        hazard_type="作业现场",
        risk_level="中",
        owner="责任岗位",
        found_on=date(2026, 9, 1),
        due_on=date(2026, 9, 8),
        corrective_action="完成整改并复核",
        status=status,
    )


class HazardManagementTests(unittest.TestCase):
    def test_create_and_validate_record(self) -> None:
        record = make_record()
        self.assertEqual(record["发现日期"], "2026-09-01")
        self.assertEqual(record["整改期限"], "2026-09-08")
        self.assertEqual(validate_hazard_record(record), ())

    def test_validation_rejects_duplicate_and_invalid_deadline(self) -> None:
        record = make_record()
        record["整改期限"] = "2026-08-31"
        errors = validate_hazard_record(record, existing_ids=("HZ-001",))
        self.assertIn("隐患编号已存在。", errors)
        self.assertIn("整改期限不能早于发现日期。", errors)

    def test_update_record_changes_status_and_measure(self) -> None:
        records = [make_record()]
        updated = update_hazard_record(
            records,
            "HZ-001",
            {"状态": "已关闭", "整改措施": "整改完成并复核"},
        )
        self.assertEqual(updated["状态"], "已关闭")
        self.assertEqual(records[0]["整改措施"], "整改完成并复核")
        self.assertEqual(records[0]["隐患编号"], "HZ-001")

    def test_summary_is_dashboard_ready(self) -> None:
        records = [
            make_record("HZ-001", "待整改"),
            make_record("HZ-002", "整改中"),
            make_record("HZ-003", "已关闭"),
            make_record("HZ-004", "已关闭"),
        ]
        summary = calculate_hazard_summary(records)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["in_progress"], 1)
        self.assertEqual(summary["closed"], 2)
        self.assertEqual(summary["completion_rate"], 50.0)
        self.assertEqual(summary["status_distribution"]["已关闭"], 2)
        self.assertEqual(summary["risk_distribution"]["中"], 4)
        self.assertEqual(summary["type_distribution"]["作业现场"], 4)
        self.assertEqual(calculate_completion_rate([]), 0.0)

    def test_demo_records_are_clearly_synthetic(self) -> None:
        records = create_demo_hazard_records(date(2026, 9, 1))
        self.assertEqual(len(records), 8)
        self.assertTrue(all(record["数据性质"] == DEMO_DATA_LABEL for record in records))
        self.assertEqual({record["责任人"] for record in records}, {"待分配（Demo）"})

    def test_csv_export_contains_fields_and_demo_label(self) -> None:
        payload = hazards_to_csv(
            create_demo_hazard_records(date(2026, 9, 1))[:1]
        ).decode("utf-8-sig")
        self.assertIn("隐患编号,隐患描述,隐患类型", payload)
        self.assertIn(DEMO_DATA_LABEL, payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)

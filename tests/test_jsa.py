"""Unit tests for the session-only JSA risk calculation helpers."""

from __future__ import annotations

import unittest

from jsa import (
    calculate_jsa_record_risks,
    calculate_risk,
    create_jsa_record,
    records_to_csv,
)


class JSARiskTests(unittest.TestCase):
    def test_risk_level_boundaries(self) -> None:
        cases = (
            (1, 1, 1, "低风险"),
            (2, 2, 4, "低风险"),
            (1, 5, 5, "中风险"),
            (3, 3, 9, "中风险"),
            (2, 5, 10, "高风险"),
            (4, 4, 16, "高风险"),
            (4, 5, 20, "重大风险"),
            (5, 5, 25, "重大风险"),
        )
        for likelihood, severity, score, level in cases:
            with self.subTest(likelihood=likelihood, severity=severity):
                self.assertEqual(calculate_risk(likelihood, severity), (score, level))

    def test_risk_inputs_must_be_between_one_and_five(self) -> None:
        for likelihood, severity in ((0, 1), (1, 0), (6, 1), (1, 6)):
            with self.subTest(likelihood=likelihood, severity=severity):
                with self.assertRaises(ValueError):
                    calculate_risk(likelihood, severity)

    def test_required_risk_examples(self) -> None:
        self.assertEqual(calculate_risk(4, 5), (20, "重大风险"))
        self.assertEqual(calculate_risk(2, 5), (10, "高风险"))
        self.assertEqual(calculate_risk(1, 4), (4, "低风险"))
        self.assertEqual(calculate_risk(3, 3), (9, "中风险"))

    def test_record_initial_and_residual_risk_share_one_calculation(self) -> None:
        record = create_jsa_record(
            job_name="HF酸洗（模拟演示）",
            job_step="模拟步骤",
            hazard="模拟危害",
            consequence="模拟后果",
            likelihood=4,
            severity=5,
            existing_controls="模拟现有措施",
            suggested_controls="模拟建议措施",
            residual_likelihood=2,
            residual_severity=5,
        )
        self.assertEqual(record["风险值R"], 20)
        self.assertEqual(record["风险等级"], "重大风险")
        self.assertEqual(record["残余风险R"], 10)
        self.assertEqual(record["残余风险等级"], "高风险")
        self.assertEqual(
            calculate_jsa_record_risks(record),
            {
                "风险值R": 20,
                "风险等级": "重大风险",
                "残余风险R": 10,
                "残余风险等级": "高风险",
            },
        )

    def test_csv_export_contains_headers_and_values(self) -> None:
        payload = records_to_csv(
            [
                {
                    "作业名称": "HF酸洗（模拟演示）",
                    "风险值R": 20,
                    "风险等级": "重大风险",
                }
            ]
        ).decode("utf-8-sig")
        self.assertIn("作业名称,风险值R,风险等级", payload)
        self.assertIn("HF酸洗（模拟演示）,20,重大风险", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)

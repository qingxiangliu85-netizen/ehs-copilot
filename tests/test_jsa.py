"""Unit tests for the session-only JSA risk calculation helpers."""

from __future__ import annotations

import unittest

from jsa import calculate_risk, records_to_csv


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

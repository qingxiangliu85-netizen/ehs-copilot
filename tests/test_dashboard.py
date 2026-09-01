"""Tests for EHS Dashboard calculations."""

from __future__ import annotations

import unittest

from dashboard import (
    calculate_dashboard_metrics,
    get_hazard_distributions,
    get_jsa_risk_distribution,
    get_priority_items,
)


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.jsa_records = [
            {
                "作业名称": "模拟作业A",
                "作业步骤": "步骤A",
                "风险等级": "重大风险",
                "风险值R": 20,
                "可能性L": 4,
                "严重度S": 5,
                "残余风险等级": "高风险",
                "残余风险R": 10,
                "控制后可能性L": 2,
                "控制后严重度S": 5,
            },
            {
                "作业名称": "模拟作业B",
                "作业步骤": "步骤B",
                "风险等级": "高风险",
                "风险值R": 12,
                "可能性L": 3,
                "严重度S": 4,
                "残余风险等级": "中风险",
                "残余风险R": 6,
                "控制后可能性L": 2,
                "控制后严重度S": 3,
            },
            {
                "作业名称": "模拟作业C",
                "作业步骤": "步骤C",
                "风险等级": "重大风险",
                "风险值R": 20,
                "可能性L": 4,
                "严重度S": 5,
                "控制后可能性L": 4,
                "控制后严重度S": 5,
            },
        ]
        self.hazard_records = [
            {"隐患编号": "HZ-001", "隐患描述": "模拟1", "隐患类型": "PPE", "风险等级": "重大", "状态": "待整改", "整改期限": "2026-09-10"},
            {"隐患编号": "HZ-002", "隐患描述": "模拟2", "隐患类型": "消防", "风险等级": "高", "状态": "整改中", "整改期限": "2026-09-11"},
            {"隐患编号": "HZ-003", "隐患描述": "模拟3", "隐患类型": "环境管理", "风险等级": "中", "状态": "已关闭", "整改期限": "2026-09-12"},
            {"隐患编号": "HZ-004", "隐患描述": "模拟4", "隐患类型": "消防", "风险等级": "低", "状态": "已关闭", "整改期限": "2026-09-13"},
        ]

    def test_core_metrics_and_jsa_high_risk_count(self) -> None:
        metrics = calculate_dashboard_metrics(self.jsa_records, self.hazard_records)
        self.assertEqual(metrics["jsa_high_major"], 2)
        self.assertEqual(metrics["hazard_pending"], 1)
        self.assertEqual(metrics["hazard_closed"], 2)
        self.assertEqual(metrics["jsa_total"], 3)
        self.assertEqual(metrics["hazard_total"], 4)

    def test_completion_rate(self) -> None:
        metrics = calculate_dashboard_metrics(self.jsa_records, self.hazard_records)
        self.assertEqual(metrics["completion_rate"], 50.0)

    def test_risk_status_and_type_distributions(self) -> None:
        jsa = get_jsa_risk_distribution(self.jsa_records)
        hazards = get_hazard_distributions(self.hazard_records)
        self.assertEqual(jsa, {"低": 0, "中": 1, "高": 1, "重大": 1})
        self.assertEqual(hazards["risk"], {"低": 1, "中": 1, "高": 1, "重大": 1})
        self.assertEqual(hazards["status"]["已关闭"], 2)
        self.assertEqual(hazards["type"]["消防"], 2)

    def test_priority_items_use_residual_risk_and_open_hazards(self) -> None:
        priority = get_priority_items(self.jsa_records, self.hazard_records)
        self.assertEqual(len(priority["jsa"]), 2)
        self.assertEqual(len(priority["hazards"]), 2)
        self.assertEqual(priority["jsa"][0]["最终风险等级"], "高")
        self.assertEqual(priority["jsa"][0]["最终风险值R"], 10)

    def test_dashboard_recalculates_stale_record_scores_from_inputs(self) -> None:
        stale_record = {
            "作业名称": "模拟作业",
            "作业步骤": "模拟步骤",
            "可能性L": 4,
            "严重度S": 5,
            "风险值R": 15,
            "风险等级": "高风险",
            "控制后可能性L": 2,
            "控制后严重度S": 5,
            "残余风险R": 8,
            "残余风险等级": "中风险",
        }
        self.assertEqual(
            get_jsa_risk_distribution([stale_record]),
            {"低": 0, "中": 0, "高": 1, "重大": 0},
        )
        priority = get_priority_items([stale_record], [])
        self.assertEqual(priority["jsa"][0]["最终风险值R"], 10)

    def test_empty_data_is_safe(self) -> None:
        metrics = calculate_dashboard_metrics([], [])
        self.assertEqual(metrics["completion_rate"], 0.0)
        self.assertEqual(metrics["jsa_high_major"], 0)
        self.assertEqual(get_jsa_risk_distribution([]), {"低": 0, "中": 0, "高": 0, "重大": 0})
        distributions = get_hazard_distributions([])
        self.assertTrue(all(value == 0 for value in distributions["status"].values()))


if __name__ == "__main__":
    unittest.main()

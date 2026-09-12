"""Tests for the V3 workflow layer: routing, tool calling and the graph run.

The tests deliberately use a stub vector store instead of a real FAISS index so
that routing and tool behaviour can be asserted without loading the embedding
model.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from langchain_core.documents import Document
from streamlit.testing.v1 import AppTest

from hazards import create_demo_hazard_records
from tools import TOOL_NAMES, ToolContext, run_tool
from workflow import router
from workflow.graph import run_workflow
from workflow.state import (
    STEP_OK,
    TASK_DASHBOARD_SUMMARY,
    TASK_HAZARD_MANAGEMENT,
    TASK_JSA_RISK,
    TASK_SDS_QUERY,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _StubIndex:
    ntotal = 2


class StubVectorStore:
    """Minimal stand-in for FAISS: only the two attributes ``rag`` touches."""

    index = _StubIndex()

    def similarity_search_with_score(self, question: str, k: int = 5):
        return [
            (
                Document(
                    page_content="8. 个体防护：操作时必须佩戴耐酸碱手套、护目镜与面屏。",
                    metadata={
                        "source": "demo.pdf",
                        "page": 2,
                        "sections": "8",
                        "product_aliases": "demo",
                    },
                ),
                0.1,
            ),
            (
                Document(
                    page_content="5. 消防：使用干粉或二氧化碳灭火器扑救。",
                    metadata={
                        "source": "demo.pdf",
                        "page": 6,
                        "sections": "5",
                        "product_aliases": "demo",
                    },
                ),
                0.2,
            ),
        ]


def make_context(**overrides) -> ToolContext:
    """Build a session context backed by the stub store and demo hazards."""
    defaults = {
        "vector_store": StubVectorStore(),
        "jsa_records": [],
        "hazard_records": create_demo_hazard_records(),
    }
    defaults.update(overrides)
    return ToolContext(**defaults)


# --------------------------------------------------------------------------- #
# 1. Routing
# --------------------------------------------------------------------------- #


class RoutingTests(unittest.TestCase):
    ROUTING_CASES = (
        (
            "查询该 SDS 中关于 PPE 的要求，并给出证据来源",
            TASK_SDS_QUERY,
            ("search_sds",),
        ),
        (
            "为 HF 酸洗作业做 JSA 风险评估，可能性4，严重度5",
            TASK_JSA_RISK,
            ("calculate_risk", "draft_jsa"),
        ),
        ("计算风险值 L=4 S=5", TASK_JSA_RISK, ("calculate_risk",)),
        (
            "新增一条隐患：配电箱前堆放杂物，风险等级高，责任人张三",
            TASK_HAZARD_MANAGEMENT,
            ("create_hazard",),
        ),
        (
            "更新隐患 DEMO-HZ-005 的状态为整改中",
            TASK_HAZARD_MANAGEMENT,
            ("update_hazard",),
        ),
        (
            "给我看看仪表盘的整体情况",
            TASK_DASHBOARD_SUMMARY,
            ("get_dashboard_summary",),
        ),
    )

    def test_inputs_route_to_the_expected_tools(self) -> None:
        for text, expected_task, expected_tools in self.ROUTING_CASES:
            with self.subTest(text=text):
                decision = router.route(text)
                self.assertIn(expected_task, decision.task_types)
                self.assertEqual(
                    tuple(item.tool for item in decision.plan), expected_tools
                )

    def test_combined_task_is_recognised(self) -> None:
        text = "查询 HF 的 PPE 要求，并为该作业做 JSA 风险评估，同时新增一条隐患：现场堆放杂物"
        decision = router.route(text)
        self.assertEqual(len(decision.task_types), 3)
        self.assertTrue(decision.is_combined)
        self.assertIn("组合任务", decision.label)
        self.assertEqual(
            tuple(item.tool for item in decision.plan),
            ("search_sds", "draft_jsa", "create_hazard"),
        )

    def test_unknown_input_produces_no_plan(self) -> None:
        decision = router.route("帮我规划一下下周的差旅")
        self.assertEqual(decision.task_types, ())
        self.assertEqual(decision.plan, ())
        self.assertIn("未能识别", decision.label)

    def test_empty_input_is_handled(self) -> None:
        self.assertEqual(router.route("").task_types, ())
        self.assertEqual(router.route("   ").plan, ())

    def test_risk_pair_phrasings(self) -> None:
        for text, expected in (
            ("可能性4，严重度5", (4, 5)),
            ("L=4 S=5", (4, 5)),
            ("4×5", (4, 5)),
            ("L4 S2", (4, 2)),
        ):
            with self.subTest(text=text):
                self.assertEqual(router.extract_risk_pair(text), expected)

    def test_hazard_and_parameter_extraction(self) -> None:
        text = "新增一条隐患：配电箱前堆放杂物，风险等级高，责任人张三，整改期限2026-10-01"
        params = router.extract_parameters(text, (TASK_HAZARD_MANAGEMENT,))
        self.assertEqual(params["hazard_level"], "高")
        self.assertEqual(params["hazard_type"], "电气安全")
        self.assertEqual(params["hazard_owner"], "张三")
        self.assertEqual(params["due_on"], "2026-10-01")
        self.assertIn("配电箱", params["hazard_description"])

    def test_sds_question_picks_the_sds_clause(self) -> None:
        decision = router.route("查询 HF 的 PPE 要求，并为该作业做 JSA 风险评估")
        self.assertEqual(decision.parameters["sds_question"], "HF 的 PPE 要求")


# --------------------------------------------------------------------------- #
# 2. Tools
# --------------------------------------------------------------------------- #


class ToolRegistryTests(unittest.TestCase):
    def test_six_tools_are_registered(self) -> None:
        self.assertEqual(
            set(TOOL_NAMES),
            {
                "search_sds",
                "calculate_risk",
                "draft_jsa",
                "create_hazard",
                "update_hazard",
                "get_dashboard_summary",
            },
        )

    def test_unknown_tool_returns_an_error_payload(self) -> None:
        payload = run_tool("does_not_exist", {}, make_context())
        self.assertEqual(payload["status"], "error")
        self.assertIn("未知工具", str(payload["message"]))


class SdsToolTests(unittest.TestCase):
    def test_search_sds_returns_answer_and_evidence(self) -> None:
        payload = run_tool(
            "search_sds",
            {"question": "该 SDS 中关于 PPE 的要求"},
            make_context(),
        )
        self.assertEqual(payload["status"], "ok")
        self.assertIn("个体防护", payload["answer"])
        self.assertEqual(payload["sources"], [["demo.pdf", 2]])
        self.assertEqual(payload["evidence"][0]["page"], 2)

    def test_search_sds_is_blocked_without_a_knowledge_base(self) -> None:
        payload = run_tool(
            "search_sds",
            {"question": "该 SDS 中关于 PPE 的要求"},
            make_context(vector_store=None),
        )
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["evidence"], [])

    def test_search_sds_rejects_an_empty_question(self) -> None:
        payload = run_tool("search_sds", {"question": "  "}, make_context())
        self.assertEqual(payload["status"], "error")


class JsaToolTests(unittest.TestCase):
    def test_calculate_risk_tool(self) -> None:
        payload = run_tool(
            "calculate_risk", {"likelihood": 4, "severity": 5}, make_context()
        )
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["risk_score"], 20)
        self.assertEqual(payload["risk_level"], "重大风险")

    def test_calculate_risk_tool_reports_bad_input(self) -> None:
        payload = run_tool(
            "calculate_risk", {"likelihood": 9, "severity": 5}, make_context()
        )
        self.assertEqual(payload["status"], "error")

    def test_draft_jsa_appends_to_the_session_records(self) -> None:
        context = make_context()
        payload = run_tool(
            "draft_jsa",
            {
                "job_name": "HF 酸洗作业",
                "hazard": "HF 飞溅",
                "likelihood": 4,
                "severity": 5,
            },
            context,
        )
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(context.jsa_records), 1)
        record = context.jsa_records[0]
        self.assertEqual(record["作业名称"], "HF 酸洗作业")
        self.assertEqual(record["风险等级"], "重大风险")
        self.assertIn("残余风险等级", record)


class HazardToolTests(unittest.TestCase):
    def test_create_hazard_appends_and_normalises_arguments(self) -> None:
        context = make_context()
        before = len(context.hazard_records)
        payload = run_tool(
            "create_hazard",
            {
                "description": "配电箱前堆放杂物",
                "hazard_type": "电气",
                "risk_level": "高风险",
                "owner": "张三",
            },
            context,
        )
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(context.hazard_records), before + 1)
        record = context.hazard_records[-1]
        self.assertEqual(record["隐患编号"], "HZ-001")
        self.assertEqual(record["隐患类型"], "电气安全")
        self.assertEqual(record["风险等级"], "高")
        self.assertEqual(record["状态"], "待整改")

    def test_update_hazard_changes_status_in_place(self) -> None:
        context = make_context()
        payload = run_tool(
            "update_hazard",
            {"hazard_id": "DEMO-HZ-005", "status": "整改中"},
            context,
        )
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["changed_fields"], ["状态"])
        target = next(
            item
            for item in context.hazard_records
            if item["隐患编号"] == "DEMO-HZ-005"
        )
        self.assertEqual(target["状态"], "整改中")

    def test_update_hazard_reports_an_unknown_id(self) -> None:
        payload = run_tool(
            "update_hazard",
            {"hazard_id": "HZ-999", "status": "已关闭"},
            make_context(),
        )
        self.assertEqual(payload["status"], "error")
        self.assertIn("HZ-999", str(payload["message"]))

    def test_update_hazard_requires_a_field(self) -> None:
        payload = run_tool(
            "update_hazard", {"hazard_id": "DEMO-HZ-005"}, make_context()
        )
        self.assertEqual(payload["status"], "error")


class DashboardToolTests(unittest.TestCase):
    def test_summary_reuses_the_dashboard_metrics(self) -> None:
        payload = run_tool("get_dashboard_summary", {}, make_context())
        self.assertEqual(payload["status"], "ok")
        metrics = payload["metrics"]
        self.assertEqual(metrics["hazard_total"], 8)
        self.assertEqual(metrics["hazard_pending"], 4)
        self.assertIn("completion_rate", metrics)
        self.assertEqual(
            set(payload["hazard_risk_distribution"]), {"低", "中", "高", "重大"}
        )


# --------------------------------------------------------------------------- #
# 3. Graph
# --------------------------------------------------------------------------- #


class WorkflowGraphTests(unittest.TestCase):
    def test_steps_run_in_order_and_end_with_a_summary(self) -> None:
        result = run_workflow("查询该 SDS 中关于 PPE 的要求", make_context())
        self.assertEqual([step["order"] for step in result.steps], [1, 2, 3, 4])
        self.assertEqual(
            [step["node"] for step in result.steps],
            ["route", "plan", "tools", "summarize"],
        )
        self.assertTrue(all(step["status"] == STEP_OK for step in result.steps))
        self.assertEqual(result.engine, "langgraph")

    def test_workflow_records_planned_arguments_on_results(self) -> None:
        result = run_workflow("计算风险值 L=4 S=5", make_context())
        self.assertEqual(len(result.tool_results), 1)
        payload = result.tool_results[0]
        self.assertEqual(payload["tool"], "calculate_risk")
        self.assertEqual(payload["arguments"], {"likelihood": 4, "severity": 5})
        self.assertIn("R = 4 × 5 = 20", result.final_answer)

    def test_workflow_executes_tools_that_mutate_the_session(self) -> None:
        context = make_context()
        result = run_workflow(
            "新增一条隐患：现场固废分类不规范，风险等级中", context
        )
        self.assertEqual([item["tool"] for item in result.tool_results], ["create_hazard"])
        self.assertEqual(len(context.hazard_records), 9)
        self.assertIn("已新增隐患 HZ-001", result.final_answer)

    def test_combined_task_calls_every_planned_tool(self) -> None:
        context = make_context()
        result = run_workflow(
            "查询 HF 的 PPE 要求，并为该作业做 JSA 风险评估，同时新增一条隐患：现场堆放杂物",
            context,
        )
        self.assertEqual(
            [item["tool"] for item in result.tool_results],
            ["search_sds", "draft_jsa", "create_hazard"],
        )
        self.assertEqual(len(context.jsa_records), 1)
        self.assertEqual(len(context.hazard_records), 9)
        self.assertEqual(result.errors, ())

    def test_tool_error_is_reported_without_breaking_the_run(self) -> None:
        result = run_workflow("更新隐患 HZ-999 的状态为已关闭", make_context())
        self.assertEqual(len(result.tool_results), 1)
        self.assertEqual(result.tool_results[0]["status"], "error")
        self.assertTrue(result.errors)
        self.assertIn("失败", result.final_answer)

    def test_unknown_task_returns_guidance_instead_of_failing(self) -> None:
        result = run_workflow("帮我规划一下下周的差旅", make_context())
        self.assertEqual(result.tool_results, ())
        self.assertIn("未能把这条请求识别为已支持的任务", result.final_answer)


# --------------------------------------------------------------------------- #
# 4. Streamlit page
# --------------------------------------------------------------------------- #


class WorkflowPageTests(unittest.TestCase):
    def test_workflow_page_renders_and_runs_an_example_task(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=300).run()
        self.assertEqual(len(app.exception), 0)

        app.radio[0].set_value("AI工作流助手").run(timeout=300)
        self.assertEqual(len(app.exception), 0)
        labels = [item.label for item in app.button]
        self.assertIn("运行工作流", labels)

        next(button for button in app.button if button.label == "隐患统计").click().run(
            timeout=300
        )
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.error), 0)

        result = app.session_state["workflow_result"]
        self.assertIsNotNone(result)
        self.assertEqual(
            [item["tool"] for item in result["tool_results"]],
            ["get_dashboard_summary"],
        )
        self.assertIn("识别任务", result["final_answer"])

        markdown = "\n".join(item.value for item in app.markdown)
        for section in (
            "识别出的任务类型",
            "计划调用的工具",
            "当前执行步骤",
            "工具执行结果",
            "最终回答",
        ):
            self.assertIn(section, markdown)

    def test_existing_four_pages_are_still_available(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=300).run()
        options = list(app.radio[0].options)
        self.assertEqual(
            options,
            [
                "EHS仪表盘",
                "SDS智能检索",
                "JSA风险评估",
                "隐患整改管理",
                "AI工作流助手",
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

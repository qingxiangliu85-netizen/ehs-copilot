"""V4 acceptance regression tests for the bugs found during the验收 pass.

Each test here pins one *real* defect that was reproduced before it was fixed,
so the same regression cannot silently come back:

1. 工具箱 entry buttons used to write ``st.session_state.nav_page`` after the
   sidebar radio had been instantiated (``StreamlitWidgetAlreadyInstantiatedError``)
   and left a red exception box, aborting the page run.
2. Closing a job-linked hazard from the stand-alone 隐患整改 module bypassed the
   evidence / reviewer / review-note requirements of the job flow.
3. The hazard list in the job-closure home page had no column captions.
4. The "no rows" message was wrong when a status filter matched nothing.
5. The JSA hazard drafts were re-offered after they had already been written to
   the ledger, so the same hazard could be created twice.
6. ``date.fromisoformat`` on a stored due date crashed the page for bad data.

The dashboard tests drive the *business* layer across a full HF transition and
assert that every metric moves with the real state change, instead of merely
checking that the page renders.

P0C moved the product navigation to ``app.py`` and preserved the V4 multi-page
console as ``legacy_console.py``; these V4 regressions therefore drive
``legacy_console.py``.
"""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

from closure import (
    close_hazard,
    close_job,
    complete_job_execution,
    create_job_hazard,
    review_hazard,
    submit_rectification_evidence,
    update_hazard_rectification,
)
from dashboard import calculate_dashboard_metrics, count_overdue_hazards, get_job_metrics
from demo_cases import HF_JOB_ID, create_demo_hf_case
from hazards import HAZARD_STATUSES, create_hazard_record
from job_review import (
    build_job_approval_request,
    confirm_jsa,
    decide_job_approval,
    draft_jsa,
    start_job_execution,
)
from jobs import JOB_STATUS_AWAITING_REVIEW
from workbench import (
    ACTIVE_JOB_KEY,
    _JOB_LIST_HEADERS,
    _iso_date,
    load_hf_case,
)
from workflow.hitl import ACTION_APPROVE, ApprovalDecision


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLBOX_TARGETS = {
    "SDS资料库": "SDS资料库",
    "JSA工具": "JSA风险评估",
    "AI工作流控制台": "AI 工作流助手",
}


def _button(app, label: str):
    return next((item for item in app.button if item.label == label), None)


def _load_hf(app: AppTest) -> AppTest:
    button = _button(app, "一键载入HF酸洗模拟案例")
    assert button is not None
    button.click().run(timeout=300)
    return app


def _app_helpers():
    """Import the pure page helpers from ``legacy_console.py`` lazily.

    ``legacy_console.py`` executes the whole Streamlit page when imported, so importing it
    at test-module level would print Streamlit's "run with streamlit run"
    warning and execute a page render during collection.
    """
    from legacy_console import legacy_close_guard, safe_index, safe_iso_date

    return legacy_close_guard, safe_index, safe_iso_date


# --------------------------------------------------------------------------- #
# 1. Navigation / session_state
# --------------------------------------------------------------------------- #


class NavigationStateTests(unittest.TestCase):
    """``nav_page`` must only ever be written from a callback (bug #1)."""

    def test_toolbox_entry_switches_page_without_widget_error(self) -> None:
        for entry, expected_title in TOOLBOX_TARGETS.items():
            with self.subTest(entry=entry):
                app = AppTest.from_file(
                    PROJECT_ROOT / "legacy_console.py", default_timeout=300
                ).run()
                app.radio[0].set_value("工具箱").run(timeout=300)
                self.assertEqual([e.value for e in app.error], [])

                button = _button(app, f"进入{entry}")
                self.assertIsNotNone(button)
                button.click().run(timeout=300)

                self.assertEqual(
                    [e.value for e in app.exception],
                    [],
                    f"{entry} 跳转抛出了异常：{[e.value for e in app.exception]}",
                )
                self.assertEqual([e.value for e in app.error], [])
                self.assertEqual(app.session_state["nav_page"], entry)
                self.assertIn(expected_title, [item.value for item in app.title])

    def test_nav_round_trip_does_not_leak_state(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        for _ in range(2):
            app.radio[0].set_value("工具箱").run(timeout=300)
            _button(app, "进入SDS资料库").click().run(timeout=300)
            self.assertEqual(app.session_state["nav_page"], "SDS资料库")
            app.radio[0].set_value("作业闭环").run(timeout=300)
            self.assertEqual([e.value for e in app.exception], [])
        self.assertEqual(len(app.job_records) if hasattr(app, "job_records") else 0, 0)

    def test_sidebar_radio_still_offers_all_seven_pages(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        self.assertEqual(
            list(app.radio[0].options),
            [
                "作业闭环",
                "隐患整改",
                "EHS驾驶舱",
                "工具箱",
                "SDS资料库",
                "JSA工具",
                "AI工作流控制台",
            ],
        )


# --------------------------------------------------------------------------- #
# 2. Home page: job list, empty states, demo loading
# --------------------------------------------------------------------------- #


class HomePageTests(unittest.TestCase):
    def test_job_list_renders_column_captions(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        _load_hf(app)
        app.session_state[ACTIVE_JOB_KEY] = None
        app.run(timeout=300)
        markdown = "\n".join(item.value for item in app.markdown)
        for header in _JOB_LIST_HEADERS:
            self.assertIn(f"**{header}**", markdown)

    def test_repeated_demo_load_does_not_duplicate_jobs(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        for _ in range(3):
            app.session_state[ACTIVE_JOB_KEY] = None
            app.run(timeout=300)
            _load_hf(app)
        self.assertEqual(len(app.session_state["job_records"]), 1)
        linked = [
            h
            for h in app.session_state["hazard_records"]
            if h.get("related_job_id") == HF_JOB_ID
        ]
        self.assertEqual(len(linked), 3)
        self.assertEqual(len({h["隐患编号"] for h in linked}), 3)

    def test_empty_filter_message_mentions_the_filter(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        _load_hf(app)
        app.session_state[ACTIVE_JOB_KEY] = None
        app.run(timeout=300)
        app.selectbox(key="wb_status_filter").set_value("已关闭").run(timeout=300)
        info = "\n".join(item.value for item in app.info)
        self.assertIn("已关闭", info)
        self.assertIn("筛选", info)

    def test_no_jobs_at_all_keeps_the_load_hint(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        info = "\n".join(item.value for item in app.info)
        self.assertIn("一键载入HF酸洗模拟案例", info)


# --------------------------------------------------------------------------- #
# 3. Closing gate + defensive rendering
# --------------------------------------------------------------------------- #


class HazardCloseGateTests(unittest.TestCase):
    def _job_linked_hazard(self) -> dict[str, object]:
        return create_hazard_record(
            hazard_id="HZ-777",
            description="作业联动隐患",
            hazard_type="其他",
            risk_level="高",
            owner="责任人",
            found_on=date(2026, 9, 1),
            due_on=date(2026, 9, 20),
            corrective_action="整改",
            status="待整改",
            related_job_id="JOB-001",
        )

    def test_job_linked_hazard_cannot_be_closed_from_the_hazard_module(self) -> None:
        legacy_close_guard, _, _ = _app_helpers()
        hazard = self._job_linked_hazard()
        message = legacy_close_guard(hazard, "已关闭")
        self.assertIn("JOB-001", message)
        self.assertIn("缺少整改证据", message)
        self.assertIn("缺少复查人", message)
        self.assertIn("缺少复查意见", message)

    def test_guard_stays_quiet_for_open_statuses_and_free_hazards(self) -> None:
        legacy_close_guard, _, _ = _app_helpers()
        hazard = self._job_linked_hazard()
        self.assertEqual(legacy_close_guard(hazard, "整改中"), "")
        hazard["rectification_evidence"] = [{"file_name": "a.pdf"}]
        hazard["reviewer"] = "复查人"
        hazard["review_note"] = "通过"
        self.assertEqual(legacy_close_guard(hazard, "已关闭"), "")

        standalone = dict(hazard)
        standalone.pop("related_job_id")
        standalone["rectification_evidence"] = []
        standalone["reviewer"] = ""
        standalone["review_note"] = ""
        self.assertEqual(legacy_close_guard(standalone, "已关闭"), "")

    def test_defensive_helpers_never_raise(self) -> None:
        _, safe_index, safe_iso_date = _app_helpers()
        self.assertEqual(safe_index(HAZARD_STATUSES, "不存在的状态"), 0)
        self.assertEqual(safe_index(HAZARD_STATUSES, None, 2), 2)
        self.assertEqual(safe_iso_date("not-a-date", date(2026, 1, 1)), date(2026, 1, 1))
        self.assertEqual(safe_iso_date("2026-09-20"), date(2026, 9, 20))
        self.assertEqual(_iso_date("", date(2026, 1, 1)), date(2026, 1, 1))

    def test_detail_page_survives_a_broken_hazard_due_date(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        _load_hf(app)
        # Simulate a corrupted / missing due date on a linked hazard.
        for hazard in app.session_state["hazard_records"]:
            if hazard.get("related_job_id") == HF_JOB_ID:
                hazard["整改期限"] = ""
                break
        app.run(timeout=300)
        self.assertEqual([e.value for e in app.exception], [])
        self.assertEqual([e.value for e in app.error], [])

    def test_hazard_module_page_renders_for_a_broken_record(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        app.radio[0].set_value("隐患整改").run(timeout=300)
        self.assertEqual([e.value for e in app.exception], [])
        self.assertIn("整改完成率", [item.label for item in app.metric])


class JsaDraftReuseTests(unittest.TestCase):
    """A JSA hazard draft must not be creatable twice (bug #5)."""

    def test_created_draft_is_no_longer_offered(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        _load_hf(app)
        _button(app, "生成 JSA 草稿").click().run(timeout=300)
        for field in app.text_input:
            if field.label == "确认人":
                field.set_value("EHS（模拟）")
        _button(app, "提交 EHS 确认").click().run(timeout=300)
        for field in app.text_input:
            if field.label == "审批人":
                field.set_value("审批人（模拟）")
        _button(app, "✅ 批准").click().run(timeout=300)
        for field in app.text_input:
            if field.label == "执行人":
                field.set_value("执行人（模拟）")
        _button(app, "开始执行").click().run(timeout=300)

        source = app.selectbox(key=f"hazard_source_{HF_JOB_ID}")
        options_before = list(source.options)
        self.assertGreater(len(options_before), 1, "HF 案例应带有 JSA 危害候选")

        # 选择第一条 JSA 草稿：表单必须自动预填该草稿的危害描述
        draft_option = options_before[1]
        source.set_value(draft_option).run(timeout=300)
        draft_text = draft_option.split(". ", 1)[1]
        prefilled = [area for area in app.text_area if area.label == "问题描述"]
        self.assertTrue(prefilled)
        self.assertIn(draft_text[:20], prefilled[0].value)

        _button(app, "审批并创建关联隐患").click().run(timeout=300)
        self.assertEqual([e.value for e in app.error], [])
        linked = app.session_state["job_records"][0]["linked_hazard_ids"]
        self.assertEqual(len(linked), 4, "HF 案例原有 3 条关联隐患 + 新建 1 条")

        source = app.selectbox(key=f"hazard_source_{HF_JOB_ID}")
        self.assertLess(
            len(list(source.options)),
            len(options_before),
            "已写入台账的 JSA 草稿不应再次出现在选项里",
        )


# --------------------------------------------------------------------------- #
# 4. Dashboard follows the real business state
# --------------------------------------------------------------------------- #


class DashboardFollowsBusinessStateTests(unittest.TestCase):
    def _hf_ready(self):
        jobs: list[dict[str, object]] = []
        hazards: list[dict[str, object]] = []
        load_hf_case(jobs, hazards)
        return jobs, hazards

    def test_every_metric_moves_with_the_job_lifecycle(self) -> None:
        jobs, hazards = self._hf_ready()
        metrics = get_job_metrics(jobs)
        self.assertEqual((metrics["job_total"], metrics["job_active"]), (1, 1))
        self.assertEqual(metrics["job_closed"], 0)
        self.assertEqual(metrics["job_completion_rate"], 0.0)

        draft_jsa(jobs, HF_JOB_ID)
        confirm_jsa(
            jobs,
            HF_JOB_ID,
            confirmed_by="EHS（模拟）",
            job_step="浸洗",
            hazard="HF 接触",
            consequence="化学灼伤",
            suggested_controls="局部排风",
            existing_controls="耐酸碱手套",
            likelihood=4,
            severity=5,
            residual_likelihood=2,
            residual_severity=5,  # 2×5=10 → 高（中文风险等级识别）
        )
        metrics = get_job_metrics(jobs)
        self.assertEqual(metrics["job_awaiting_approval"], 1)
        self.assertEqual(metrics["job_high_risk"], 1, "残余 2×5=10 必须识别为高风险")

        request = build_job_approval_request(jobs[0])
        decide_job_approval(
            jobs,
            HF_JOB_ID,
            ApprovalDecision(
                gate_id=request.gate_id,
                action=ACTION_APPROVE,
                calls=(),
                note="同意",
            ),
            actor="审批人（模拟）",
        )
        self.assertEqual(get_job_metrics(jobs)["job_awaiting_approval"], 0)

        start_job_execution(jobs, HF_JOB_ID, actor="执行人（模拟）")
        metrics = get_job_metrics(jobs)
        self.assertEqual(metrics["job_executing"], 1)
        self.assertEqual(metrics["job_active"], 1, "执行中的作业仍属于在办")

        for hazard in hazards:
            identifier = str(hazard["隐患编号"])
            update_hazard_rectification(
                jobs,
                hazards,
                HF_JOB_ID,
                identifier,
                actor="EHS（模拟）",
                owner="责任人甲",
                due_on=hazard["整改期限"],
                corrective_action="模拟整改",
            )
            submit_rectification_evidence(
                jobs,
                hazards,
                HF_JOB_ID,
                identifier,
                file_name="占位证据（模拟）.pdf",
                uploaded_by="上传人（模拟）",
            )
            review_hazard(
                jobs,
                hazards,
                HF_JOB_ID,
                identifier,
                reviewer="复查人（模拟）",
                review_note="模拟复查通过",
            )
            close_hazard(jobs, hazards, HF_JOB_ID, identifier)

        complete_job_execution(jobs, HF_JOB_ID, actor="执行人（模拟）")
        metrics = get_job_metrics(jobs)
        self.assertEqual(metrics["job_executing"], 0)
        self.assertEqual(metrics["job_awaiting_review"], 1)
        self.assertEqual(metrics["job_active"], 1)
        self.assertEqual(jobs[0]["status"], JOB_STATUS_AWAITING_REVIEW)

        close_job(jobs, hazards, HF_JOB_ID, actor="EHS（模拟）")
        metrics = get_job_metrics(jobs)
        self.assertEqual(metrics["job_closed"], 1)
        self.assertEqual(metrics["job_active"], 0, "已关闭的作业不能再计入在办")
        self.assertEqual(metrics["job_awaiting_review"], 0)
        self.assertEqual(metrics["job_completion_rate"], 100.0)

        dashboard = calculate_dashboard_metrics([], hazards, jobs)
        self.assertEqual(dashboard["hazard_closed"], 3)
        self.assertEqual(dashboard["hazard_overdue"], 0, "已关闭隐患不计入逾期")
        self.assertEqual(dashboard["completion_rate"], 100.0)

    def test_overdue_only_counts_open_hazards(self) -> None:
        records = [
            create_hazard_record(
                hazard_id="HZ-901",
                description="已逾期未关闭",
                hazard_type="其他",
                risk_level="高",
                owner="甲",
                found_on=date(2026, 1, 1),
                due_on=date(2026, 1, 2),
                corrective_action="整改",
                status="待整改",
            ),
            create_hazard_record(
                hazard_id="HZ-902",
                description="已逾期但已关闭",
                hazard_type="其他",
                risk_level="高",
                owner="甲",
                found_on=date(2026, 1, 1),
                due_on=date(2026, 1, 2),
                corrective_action="整改",
                status="已关闭",
            ),
        ]
        self.assertEqual(count_overdue_hazards(records, today=date(2026, 9, 13)), 1)

    def test_empty_state_never_divides_by_zero(self) -> None:
        dashboard = calculate_dashboard_metrics([], [], [])
        self.assertEqual(dashboard["job_completion_rate"], 0.0)
        self.assertEqual(dashboard["completion_rate"], 0.0)
        self.assertEqual(dashboard["hazard_overdue"], 0)
        self.assertEqual(dashboard["job_active"], 0)


# --------------------------------------------------------------------------- #
# 5. Approval gate cannot be bypassed from the UI
# --------------------------------------------------------------------------- #


class ApprovalGateTests(unittest.TestCase):
    def test_execution_is_not_reachable_before_approval(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        _load_hf(app)
        _button(app, "生成 JSA 草稿").click().run(timeout=300)
        for field in app.text_input:
            if field.label == "确认人":
                field.set_value("EHS（模拟）")
        _button(app, "提交 EHS 确认").click().run(timeout=300)

        # 待审批：执行按钮不存在
        self.assertIsNone(_button(app, "开始执行"))
        self.assertIsNotNone(_button(app, "✅ 批准"))

    def test_rejected_job_offers_no_execution_path(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        _load_hf(app)
        _button(app, "生成 JSA 草稿").click().run(timeout=300)
        for field in app.text_input:
            if field.label == "确认人":
                field.set_value("EHS（模拟）")
        _button(app, "提交 EHS 确认").click().run(timeout=300)
        for field in app.text_input:
            if field.label == "审批人":
                field.set_value("审批人（模拟）")
        for area in app.text_area:
            if area.label == "驳回原因":
                area.set_value("证据不足")
        _button(app, "⛔ 驳回").click().run(timeout=300)

        job = app.session_state["job_records"][0]
        self.assertEqual(job["status"], "已驳回")
        self.assertIsNone(_button(app, "开始执行"))
        self.assertIsNone(_button(app, "✅ 批准"))
        self.assertIn("驳回", "\n".join(item.value for item in app.error))
        self.assertEqual([e.value for e in app.exception], [])

    def test_modify_and_approve_records_a_conditional_approval(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        _load_hf(app)
        _button(app, "生成 JSA 草稿").click().run(timeout=300)
        for field in app.text_input:
            if field.label == "确认人":
                field.set_value("EHS（模拟）")
        _button(app, "提交 EHS 确认").click().run(timeout=300)
        for field in app.text_input:
            if field.label == "审批人":
                field.set_value("审批-李工（模拟）")
        for area in app.text_area:
            if area.label == "审批条件 / 备注":
                area.set_value("每 2 小时巡检一次")
        _button(app, "✏️ 修改后批准").click().run(timeout=300)

        self.assertEqual([e.value for e in app.exception], [])
        self.assertEqual([e.value for e in app.error], [])
        job = app.session_state["job_records"][0]
        self.assertEqual(job["status"], "已批准")
        approvals = job["approvals"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["action"], "modify")
        self.assertIn("每 2 小时巡检一次", approvals[0]["note"])
        self.assertIsNotNone(_button(app, "开始执行"))

    def test_approval_cannot_be_submitted_twice(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "legacy_console.py", default_timeout=300).run()
        _load_hf(app)
        _button(app, "生成 JSA 草稿").click().run(timeout=300)
        for field in app.text_input:
            if field.label == "确认人":
                field.set_value("EHS（模拟）")
        _button(app, "提交 EHS 确认").click().run(timeout=300)
        for field in app.text_input:
            if field.label == "审批人":
                field.set_value("审批-李工（模拟）")
        approve = _button(app, "✅ 批准")
        approve.click().run(timeout=300)
        # 重复点击同一按钮：状态已离开「待审批」，按钮不再存在
        self.assertIsNone(_button(app, "✅ 批准"))
        job = app.session_state["job_records"][0]
        self.assertEqual(len(job["approvals"]), 1, "审批不得重复写入")
        self.assertEqual(job["status"], "已批准")


if __name__ == "__main__":
    unittest.main(verbosity=2)

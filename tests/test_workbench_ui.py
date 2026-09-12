"""P4B UI regression tests: the job-closure workbench and the seven-item nav.

These tests protect the product-level guarantees of P4B:

* the seven top-level navigation entries and the preserved legacy tools;
* the ``作业闭环`` home page (six pipeline metrics, two entry actions, job list);
* the ten business stages of one job, with public-source evidence and SDS
  evidence kept strictly apart and the AI draft kept apart from the EHS
  confirmation;
* the HF pickling demo case being walkable from 草稿 to 已关闭;
* technical traces collapsed by default.

The Streamlit tests drive the real ``app.py`` with ``AppTest``; the helper tests
are pure-function checks that keep the stage badges honest.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

from demo_cases import HF_JOB_ID, create_demo_hf_case
from job_review import attach_sds_evidence
from workbench import (
    ACTIVE_JOB_KEY,
    JOB_FLOW,
    close_blockers,
    evidence_track_status,
    job_close_blockers,
    load_hf_case,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

NAV_PAGES = (
    "作业闭环",
    "隐患整改",
    "EHS驾驶舱",
    "工具箱",
    "SDS资料库",
    "JSA工具",
    "AI工作流控制台",
)

HF_HAZARD_IDS = ("DEMO-HZ-HF-001", "DEMO-HZ-HF-002", "DEMO-HZ-HF-003")


def _stage_values(app) -> list[str]:
    return [
        item.value
        for item in app.markdown
        if item.value.startswith("#### ") and "进度" not in item.value
    ]


def _buttons(app, label: str, index: int = 0):
    items = [button for button in app.button if button.label == label]
    return items[index] if index < len(items) else None


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


class WorkbenchHelperTests(unittest.TestCase):
    def test_job_flow_matches_the_business_pipeline(self) -> None:
        self.assertEqual(
            JOB_FLOW,
            ("草稿", "待EHS确认", "待审批", "已批准", "执行中", "待复查", "已关闭"),
        )

    def test_evidence_track_status_keeps_tracks_separate(self) -> None:
        job = {"public_evidence": [{"source_url": "https://example.org"}], "sds_evidence": []}
        self.assertEqual(evidence_track_status(job), (True, False))
        job["sds_evidence"] = [{"source": "sds.pdf", "page": 2, "snippet": "x"}]
        self.assertEqual(evidence_track_status(job), (True, True))
        self.assertEqual(evidence_track_status({}), (False, False))

    def test_public_evidence_cannot_be_attached_to_the_sds_track(self) -> None:
        job = {"public_evidence": [], "sds_evidence": []}
        with self.assertRaises(ValueError):
            attach_sds_evidence(job, [{"source": "a.pdf", "page": 1, "snippet": "x",
                                       "source_url": "https://example.org"}])

    def test_hazard_close_blockers_list_missing_requirements(self) -> None:
        hazard = {"状态": "待整改"}
        blockers = close_blockers(hazard)
        self.assertIn("缺少整改证据", blockers)
        self.assertIn("缺少复查人", blockers)
        self.assertIn("缺少复查意见", blockers)

        hazard.update(
            {
                "rectification_evidence": [{"file_name": "a.pdf"}],
                "reviewer": "复查人",
                "review_note": "通过",
            }
        )
        self.assertEqual(close_blockers(hazard), [])

    def test_job_close_blockers_require_review_stage_and_closed_hazards(self) -> None:
        hazards: list[dict[str, object]] = [
            {"隐患编号": "HZ-1", "状态": "待整改"},
        ]
        job = {"status": "执行中", "linked_hazard_ids": ["HZ-1"]}
        self.assertTrue(job_close_blockers(job, hazards))

        job["status"] = "待复查"
        blockers = job_close_blockers(job, hazards)
        self.assertTrue(any("HZ-1" in item for item in blockers))

        hazards[0]["状态"] = "已关闭"
        self.assertEqual(job_close_blockers(job, hazards), [])
        self.assertEqual(job_close_blockers({"status": "已关闭"}, hazards), [])

    def test_load_hf_case_is_idempotent(self) -> None:
        jobs: list[dict[str, object]] = []
        hazards: list[dict[str, object]] = []
        first = load_hf_case(jobs, hazards)
        second = load_hf_case(jobs, hazards)
        self.assertEqual(first, second)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(hazards), len(HF_HAZARD_IDS))


# --------------------------------------------------------------------------- #
# Streamlit UI
# --------------------------------------------------------------------------- #


class WorkbenchHomeTests(unittest.TestCase):
    def test_home_page_shows_pipeline_metrics_actions_and_job_list(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=300).run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(list(app.radio[0].options), list(NAV_PAGES))
        self.assertIn("作业闭环", [item.value for item in app.title])

        labels = [item.label for item in app.metric]
        for label in ("在办作业", "待审批", "执行中", "待复查", "高风险作业", "逾期隐患"):
            self.assertIn(label, labels)

        buttons = [item.label for item in app.button]
        self.assertIn("一键载入HF酸洗模拟案例", buttons)
        self.assertIn("新建危化品作业", buttons)

        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("作业列表", markdown + "\n".join(
            item.value for item in app.subheader
        ))

    def test_new_job_action_reveals_the_creation_form(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=300).run()
        _buttons(app, "新建危化品作业").click().run(timeout=300)
        self.assertEqual(len(app.exception), 0)
        labels = [item.label for item in app.text_input]
        self.assertIn("作业名称", labels)
        self.assertIsNotNone(_buttons(app, "创建作业"))


class JobDetailStageTests(unittest.TestCase):
    def setUp(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=300).run()
        _buttons(app, "一键载入HF酸洗模拟案例").click().run(timeout=300)
        self.app = app

    def test_detail_page_renders_all_ten_stages(self) -> None:
        app = self.app
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state[ACTIVE_JOB_KEY], HF_JOB_ID)
        stages = _stage_values(app)
        self.assertEqual(len(stages), 10)
        for number, title in enumerate(
            (
                "基础信息",
                "安全证据",
                "JSA草稿",
                "EHS人工确认",
                "审批",
                "执行",
                "关联隐患",
                "整改证据",
                "EHS复查",
                "作业关闭",
            ),
            start=1,
        ):
            self.assertTrue(
                any(f"#### {number}. {title}" in value for value in stages),
                f"missing stage {number} {title}",
            )

    def test_public_and_sds_evidence_are_rendered_apart(self) -> None:
        app = self.app
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("【公开安全证据】非 SDS", markdown)
        self.assertIn("【SDS 证据】", markdown)
        captions = "\n".join(item.value for item in app.caption)
        self.assertIn("证据轨道状态", captions)
        # 无 SDS 时必须明确告知不得称为 SDS 结论。
        warnings = "\n".join(item.value for item in app.warning)
        self.assertIn("公开安全证据不能替代 SDS", warnings)

    def test_technical_details_are_collapsed_by_default(self) -> None:
        app = self.app
        technical = [
            item for item in app.expander if item.label == "技术详情 / 执行轨迹"
        ]
        self.assertEqual(len(technical), 1)
        self.assertFalse(technical[0].proto.expanded)
        # 技术信息（状态流转 / Guardrail / 原始 JSON）挂在折叠区内部。
        self.assertTrue(technical[0].code)
        self.assertFalse(bool(app.session_state["job_records"][0]["jsa_draft"]))

    def test_demo_data_is_labelled(self) -> None:
        app = self.app
        job = app.session_state["job_records"][0]
        self.assertIn("模拟", str(job["data_label"]))
        captions = "\n".join(item.value for item in app.caption)
        self.assertIn("数据性质", captions)

    def test_ai_draft_and_human_confirmation_are_shown_separately(self) -> None:
        app = self.app
        _buttons(app, "生成 JSA 草稿").click().run(timeout=300)
        self.assertEqual(len(app.exception), 0)
        captions = "\n".join(item.value for item in app.caption)
        self.assertIn("未经 EHS 确认不得作为正式 JSA", captions)

        for field in app.text_input:
            if field.label == "确认人":
                field.set_value("EHS-张工（模拟）")
        _buttons(app, "提交 EHS 确认").click().run(timeout=300)
        self.assertEqual(len(app.exception), 0)

        job = app.session_state["job_records"][0]
        confirmation = job["jsa_confirmation"]
        self.assertIn("ai_draft", confirmation)
        self.assertIn("final", confirmation)
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("AI原始内容（草稿）", markdown)
        self.assertIn("人工最终内容", markdown)
        # 人工确认后进入审批阶段，风险值必须来自 jsa.calculate_risk。
        self.assertEqual(job["status"], "待审批")
        self.assertEqual(confirmation["risk_source"], "jsa.calculate_risk")


class HfCaseEndToEndTests(unittest.TestCase):
    """The HF demo case must be walkable from 草稿 to 已关闭 through the UI."""

    def test_hf_case_can_be_walked_to_job_closure(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=300).run()
        _buttons(app, "一键载入HF酸洗模拟案例").click().run(timeout=300)

        # 3 → EHS 确认
        _buttons(app, "生成 JSA 草稿").click().run(timeout=300)
        for field in app.text_input:
            if field.label == "确认人":
                field.set_value("EHS-张工（模拟）")
        _buttons(app, "提交 EHS 确认").click().run(timeout=300)

        # 5 → 审批
        for field in app.text_input:
            if field.label == "审批人":
                field.set_value("审批-李工（模拟）")
        _buttons(app, "✅ 批准").click().run(timeout=300)

        # 6 → 执行
        for field in app.text_input:
            if field.label == "执行人":
                field.set_value("执行-王工（模拟）")
        _buttons(app, "开始执行").click().run(timeout=300)

        # 8/9 → 整改证据 + EHS 复查 + 关闭隐患
        demo_placeholder = next(
            (
                box
                for box in app.checkbox
                if "模拟证据占位" in box.label
            ),
            None,
        )
        self.assertIsNotNone(demo_placeholder)
        demo_placeholder.set_value(True)
        app.run(timeout=300)

        for _ in range(len(HF_HAZARD_IDS)):
            if not [b for b in app.button if b.label == "关闭隐患"]:
                break
            _buttons(app, "上传整改证据").click().run(timeout=300)
            self.assertEqual([e.value for e in app.error], [])
            reviewers = [t for t in app.text_input if t.label == "复查人"]
            notes = [t for t in app.text_area if t.label == "复查意见"]
            reviewers[0].set_value("复查-赵工（模拟）")
            notes[0].set_value("模拟复查：整改证据已核对，同意关闭。")
            app.run(timeout=300)
            _buttons(app, "提交复查").click().run(timeout=300)
            _buttons(app, "关闭隐患").click().run(timeout=300)
            self.assertEqual(len(app.exception), 0)

        hazards = app.session_state["hazard_records"]
        linked = [h for h in hazards if h["隐患编号"] in HF_HAZARD_IDS]
        self.assertTrue(all(h["状态"] == "已关闭" for h in linked))
        self.assertTrue(all(h["rectification_evidence"] for h in linked))

        stages = _stage_values(app)
        self.assertTrue(any("#### 7. 关联隐患\u3000✅" in s for s in stages))
        self.assertTrue(any("#### 8. 整改证据\u3000✅" in s for s in stages))
        self.assertTrue(any("#### 9. EHS复查\u3000✅" in s for s in stages))

        # 6 → 执行完成 → 待复查
        _buttons(app, "执行完成").click().run(timeout=300)
        job = app.session_state["job_records"][0]
        self.assertEqual(job["status"], "待复查")

        # 10 → 关闭作业
        for field in app.text_input:
            if field.label == "关闭人":
                field.set_value("EHS-张工（模拟）")
        close_button = _buttons(app, "关闭作业")
        self.assertIsNotNone(close_button)
        self.assertFalse(close_button.disabled)
        close_button.click().run(timeout=300)
        self.assertEqual(len(app.exception), 0)

        job = app.session_state["job_records"][0]
        self.assertEqual(job["status"], "已关闭")
        self.assertTrue(job["closure_info"]["all_hazards_closed"])
        self.assertEqual(
            [entry["to"] for entry in job["status_history"]],
            ["草稿", "待EHS确认", "待审批", "已批准", "执行中", "待复查", "已关闭"],
        )
        stages = _stage_values(app)
        self.assertEqual(len(stages), 10)
        for value in stages:
            self.assertIn("✅ 已完成", value)


class ToolboxTests(unittest.TestCase):
    def test_toolbox_links_to_the_three_preserved_tools(self) -> None:
        app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=300).run()
        app.radio[0].set_value("工具箱").run(timeout=300)
        self.assertEqual(len(app.exception), 0)
        self.assertIn("工具箱", [item.value for item in app.title])
        labels = [item.label for item in app.button]
        for name in ("SDS资料库", "JSA工具", "AI工作流控制台"):
            self.assertIn(f"进入{name}", labels)


class DemoCaseFixtureTests(unittest.TestCase):
    def test_demo_case_starts_from_a_safe_state(self) -> None:
        case = create_demo_hf_case()
        job = case["job"]
        self.assertEqual(job["status"], "草稿")
        self.assertEqual(job["sds_evidence"], [])
        self.assertIsNone(job["jsa_draft"])
        self.assertTrue(job["public_evidence"])
        self.assertEqual(
            [item["隐患编号"] for item in case["linked_hazards"]],
            list(HF_HAZARD_IDS),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

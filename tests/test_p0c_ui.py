"""P0C tests: the V5 product UI (``app.py``) and the preserved V4 console.

What this suite protects:

* the first-level navigation is exactly the five business pages — the technical
  tools (standalone SDS library, JSA tool, AI workflow console, toolbox) are
  hidden from the business nav but still exist in ``legacy_console.py``;
* switching the Demo persona changes 「我的待办」 and the actions on offer,
  while the service layer keeps enforcing permission;
* the permit detail page answers 「当前需要你做什么」 from status + persona +
  permission, and JSA/SDS live inside it;
* SDS evidence and public-source evidence stay visually and structurally apart,
  and a synthetic SDS is labelled as such;
* the SDS library has explicit 未加载 / 加载中 / 已加载 / 加载失败 states instead
  of an endless "auto loading" spinner;
* rectification completion is not the same thing as an EHS verification;
* no technical vocabulary leaks into the business screens.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

import db
import demo_seed
from services import hazard_service, permit_service, persona_service
from ui import common, hazards, overview, permits, records, worklist

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_NAV = ("今日工作台", "作业许可", "隐患与整改", "风险看板", "资料与审计")
HIDDEN_FROM_NAV = ("SDS资料库", "JSA工具", "AI工作流控制台", "工具箱")
FORBIDDEN_TECHNICAL_TERMS = (
    "LangGraph",
    "Router",
    "Tool Calling",
    "tool calling",
    "Agent节点",
    "Supervisor",
    "guardrail",
    "Guardrail",
    "workflow",
    "JSON",
)
DETAIL_SECTIONS = (
    "基本信息",
    "化学品与 SDS 证据",
    "JSA 风险评估",
    "审核与审批记录",
    "开工前检查",
    "执行 / 暂停 / 恢复 / 交还",
    "关联隐患",
    "操作记录（Activity Timeline）",
)


def _page_text(app: AppTest) -> str:
    """Return every visible business string of one rendered page."""
    parts: list[str] = []
    for collection in (app.markdown, app.caption, app.text, app.title, app.subheader):
        parts.extend(str(item.value) for item in collection)
    return "\n".join(parts)


def _button(app: AppTest, label: str):
    return next((item for item in app.button if item.label == label), None)


def _nav(app: AppTest, page: str) -> AppTest:
    return app.radio(key="nav_page").set_value(page).run(timeout=300)


class BusinessVocabularyTests(unittest.TestCase):
    """Pure checks that do not need Streamlit."""

    def test_navigation_is_exactly_the_five_business_pages(self) -> None:
        self.assertEqual(common.NAV_PAGES, EXPECTED_NAV)
        for hidden in HIDDEN_FROM_NAV:
            self.assertNotIn(hidden, common.NAV_PAGES)

    def test_the_technical_console_is_preserved_but_hidden(self) -> None:
        text = (PROJECT_ROOT / "legacy_console.py").read_text(encoding="utf-8")
        for page in ("作业闭环", "隐患整改", "EHS驾驶舱", *HIDDEN_FROM_NAV):
            self.assertIn(page, text)

    def test_sds_library_has_four_explicit_states(self) -> None:
        self.assertEqual(
            set(records.STATE_LABELS),
            {
                records.STATE_NOT_LOADED,
                records.STATE_LOADING,
                records.STATE_LOADED,
                records.STATE_ERROR,
            },
        )
        self.assertEqual(records.STATE_LABELS[records.STATE_NOT_LOADED], "未加载")
        self.assertEqual(records.STATE_LABELS[records.STATE_ERROR], "加载失败")

    def test_every_permit_status_has_a_business_next_action(self) -> None:
        from workflow import permit_state

        for status in permit_state.PERMIT_STATUSES:
            self.assertNotEqual(
                permits.permit_next_action({"status": status}), "—", status
            )
        for status in permit_state.PERMIT_STATUSES:
            common.permit_stage_index(status)  # must not raise


class SeededDemoStoreTests(unittest.TestCase):
    """The Demo store must give every persona real work."""

    def setUp(self) -> None:
        self.connection = db.open_database(":memory:")
        self.addCleanup(self.connection.close)
        self.result = demo_seed.seed_demo_data(self.connection)

    def _queue(self, user_id: str) -> list[dict[str, object]]:
        user = persona_service.get_user(self.connection, user_id)
        assert user is not None
        return worklist.build_worklist(self.connection, user)

    def test_seeding_is_idempotent(self) -> None:
        again = demo_seed.seed_demo_data(self.connection)
        self.assertFalse(again["seeded"])
        self.assertEqual(again["permits"], self.result["permits"])

    def test_every_persona_has_at_least_one_work_item(self) -> None:
        for user_id, _ in common.PERSONA_CHOICES:
            with self.subTest(persona=user_id):
                self.assertGreaterEqual(len(self._queue(user_id)), 1)

    def test_personas_see_different_queues(self) -> None:
        applicant = {row["entity_id"] for row in self._queue("DEMO-APPLICANT-01")}
        approver = {row["entity_id"] for row in self._queue("DEMO-APPROVER-01")}
        owner = {row["entity_id"] for row in self._queue("DEMO-OWNER-01")}
        self.assertNotEqual(applicant, approver)
        self.assertNotEqual(owner, approver)
        # The applicant only ever sees their own drafts, never other people's work.
        self.assertEqual(applicant, {"PERMIT-DEMO-001", "PERMIT-DEMO-005"})

    def test_worklist_filters_partition_the_rows(self) -> None:
        rows = self._queue("DEMO-ADMIN-01")
        everything = worklist.filter_worklist(rows, worklist.FILTER_ALL)
        self.assertEqual(len(everything), len(rows))
        overdue = worklist.filter_worklist(rows, worklist.FILTER_OVERDUE)
        self.assertTrue(all(row["is_overdue"] for row in overdue))
        for choice in worklist.WORKLIST_FILTERS:
            with self.subTest(choice=choice):
                self.assertIsInstance(worklist.filter_worklist(rows, choice), list)

    def test_summary_matches_the_rows(self) -> None:
        rows = self._queue("DEMO-ADMIN-01")
        summary = worklist.worklist_summary(rows)
        self.assertEqual(summary["total"], len(rows))
        self.assertEqual(
            summary["overdue"], sum(1 for row in rows if row["is_overdue"])
        )
        self.assertGreaterEqual(summary["high_risk"], 1)

    def test_evidence_tracks_stay_separate_and_labelled(self) -> None:
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-001")
        assert permit is not None
        evidence = permit["evidence"]
        self.assertEqual(len(evidence["sds"]), 1)
        self.assertEqual(len(evidence["public_sources"]), 1)
        sds_row = evidence["sds"][0]
        public_row = evidence["public_sources"][0]
        self.assertEqual(sds_row["data_label"], "Demo / Synthetic SDS")
        self.assertTrue(sds_row["is_demo"])
        self.assertGreater(sds_row["page"], 0)
        # A public-source row can never be mistaken for an SDS: no page, no file.
        self.assertEqual(public_row["page"], 0)
        self.assertEqual(public_row["source"], "")
        self.assertTrue(public_row["source_url"])

    def test_jsa_draft_and_human_confirmation_are_distinguishable(self) -> None:
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-001")
        assert permit is not None
        items = permit["jsa_items"]
        drafts = [item for item in items if not item["confirmed_by"]]
        confirmed = [item for item in items if item["confirmed_by"]]
        self.assertTrue(drafts)
        self.assertTrue(confirmed)
        for item in confirmed:
            self.assertEqual(item["confirmed_by"], demo_seed.EHS_REVIEWER)
            self.assertTrue(item["confirmed_at"])

    def test_rectification_completion_is_not_a_closed_hazard(self) -> None:
        hazard = hazard_service.get_hazard(self.connection, "HZ-DEMO-002")
        assert hazard is not None
        self.assertEqual(hazard["status"], "verification_pending")
        self.assertTrue(hazard["evidence"])

        owner = persona_service.get_user(self.connection, "DEMO-OWNER-01")
        reviewer = persona_service.get_user(self.connection, "DEMO-EHS-01")
        assert owner is not None and reviewer is not None
        owner_actions = {a.code for a in worklist.actions_for("hazard", hazard, owner)}
        reviewer_actions = {
            a.code for a in worklist.actions_for("hazard", hazard, reviewer)
        }
        self.assertNotIn("verify_pass", owner_actions)
        self.assertNotIn("verify_fail", owner_actions)
        self.assertIn("verify_pass", reviewer_actions)
        self.assertIn("verify_fail", reviewer_actions)

    def test_kpi_values_match_the_store(self) -> None:
        values = overview.kpi_values(self.connection)
        permits_rows = permit_service.list_permits(self.connection, limit=1000)
        self.assertEqual(
            values["active_permits"],
            sum(1 for row in permits_rows if row["status"] == "active"),
        )
        self.assertEqual(
            values["pending_approval"],
            sum(1 for row in permits_rows if row["status"] == "approval_pending"),
        )
        self.assertGreaterEqual(values["overdue_rectification"], 1)
        self.assertGreaterEqual(values["pending_verification"], 1)
        self.assertGreaterEqual(values["high_open_hazards"], 1)


class PermitActionCommandTests(unittest.TestCase):
    """Clicks are forwarded to the services, which keep enforcing permission."""

    def setUp(self) -> None:
        self.connection = db.open_database(":memory:")
        self.addCleanup(self.connection.close)
        demo_seed.seed_demo_data(self.connection)
        self.applicant = persona_service.get_user(self.connection, "DEMO-APPLICANT-01")
        self.owner = persona_service.get_user(self.connection, "DEMO-OWNER-01")
        assert self.applicant is not None and self.owner is not None

    def test_a_reason_is_mandatory_before_a_return(self) -> None:
        from ui import commands

        ok, message = commands.execute(
            self.connection, "permit", "PERMIT-DEMO-002", "reject", user=self.applicant
        )
        self.assertFalse(ok)
        self.assertTrue(message)

    def test_permission_is_denied_even_if_the_page_offers_the_button(self) -> None:
        from ui import commands

        ok, message = commands.execute(
            self.connection,
            "permit",
            "PERMIT-DEMO-002",
            "approve",
            user=self.applicant,
        )
        self.assertFalse(ok)
        self.assertNotEqual(message, "")
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-002")
        assert permit is not None
        self.assertEqual(permit["status"], "approval_pending")

    def test_the_owner_can_start_rectification(self) -> None:
        from ui import commands

        ok, _ = commands.execute(
            self.connection, "hazard", "HZ-DEMO-001", "start", user=self.owner
        )
        self.assertTrue(ok)
        hazard = hazard_service.get_hazard(self.connection, "HZ-DEMO-001")
        assert hazard is not None
        self.assertEqual(hazard["status"], "in_progress")


class ProductUiTests(unittest.TestCase):
    """The real Streamlit app, driven through ``AppTest``."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._original = db.EHS_DB_PATH
        db.EHS_DB_PATH = str(Path(self._temp.name) / "p0c.db")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        db.EHS_DB_PATH = self._original
        self._temp.cleanup()

    def _app(self) -> AppTest:
        app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=300).run()
        if app.exception:
            self.fail(f"页面抛出异常：{[item.value for item in app.exception]}")
        return app

    def _connection(self):
        connection = db.open_database()
        self.addCleanup(connection.close)
        return connection

    # ---------------------------------------------------------------- nav --

    def test_first_level_navigation_has_only_the_five_business_pages(self) -> None:
        app = self._app()
        self.assertEqual(list(app.radio(key="nav_page").options), list(EXPECTED_NAV))
        self.assertEqual([item.value for item in app.title], ["今日工作台"])

    def test_every_page_renders_without_a_traceback(self) -> None:
        app = self._app()
        for page in EXPECTED_NAV:
            with self.subTest(page=page):
                _nav(app, page)
                self.assertEqual(
                    [item.value for item in app.exception],
                    [],
                    f"{page} 抛出异常",
                )
                self.assertEqual([item.value for item in app.title], [page])

    def test_the_demo_notice_appears_once_and_the_identity_is_labelled(self) -> None:
        app = self._app()
        captions = [item.value for item in app.caption]
        self.assertEqual(captions.count(common.DEMO_NOTICE), 1)
        sidebar_text = "\n".join(
            captions + [item.value for item in app.markdown]
        )
        self.assertIn("当前身份（Demo）", sidebar_text)
        self.assertTrue(
            any(common.DEMO_IDENTITY_NOTICE in value for value in captions)
        )

    def test_no_technical_vocabulary_on_the_business_pages(self) -> None:
        app = self._app()
        for page in EXPECTED_NAV:
            with self.subTest(page=page):
                _nav(app, page)
                text = _page_text(app)
                for term in FORBIDDEN_TECHNICAL_TERMS:
                    self.assertNotIn(term, text, f"{page} 出现技术词：{term}")

    # ------------------------------------------------------------ persona --

    def test_switching_persona_changes_the_worklist_and_the_actions(self) -> None:
        app = self._app()
        admin_total = int(
            next(
                item.value for item in app.metric if item.label == "我的待办"
            )
        )
        app.selectbox(key="demo_user_id").set_value("DEMO-APPROVER-01").run(timeout=300)
        approver_total = int(
            next(item.value for item in app.metric if item.label == "我的待办")
        )
        self.assertNotEqual(admin_total, approver_total)
        labels = [item.label for item in app.button]
        self.assertIn("批准作业", labels)
        self.assertNotIn("确认并提交审批", labels)

        app.selectbox(key="demo_user_id").set_value("DEMO-OWNER-01").run(timeout=300)
        labels = [item.label for item in app.button]
        self.assertIn("完成开工检查并开始作业", labels)
        self.assertNotIn("批准作业", labels)

    def test_the_owner_cannot_see_the_ehs_verification_action(self) -> None:
        app = self._app()
        app.selectbox(key="demo_user_id").set_value("DEMO-OWNER-01").run(timeout=300)
        self.assertNotIn("验证通过", [item.label for item in app.button])

        app.selectbox(key="demo_user_id").set_value("DEMO-EHS-01").run(timeout=300)
        self.assertIn("验证通过", [item.label for item in app.button])

    # ------------------------------------------------------------- permit --

    def test_today_card_opens_the_matching_permit_detail(self) -> None:
        app = self._app()
        app.selectbox(key="demo_user_id").set_value("DEMO-EHS-01").run(timeout=300)
        button = _button(app, "确认并提交审批")
        self.assertIsNotNone(button)
        button.click().run(timeout=300)
        self.assertEqual([item.value for item in app.exception], [])
        title = "\n".join(item.value for item in app.title)
        # The EHS reviewer's only pending review on a fresh store is PERMIT-DEMO-003.
        self.assertIn("PERMIT-DEMO-003", title)

    def test_permit_detail_shows_the_header_and_the_current_task(self) -> None:
        app = self._app()
        app.selectbox(key="demo_user_id").set_value("DEMO-EHS-01").run(timeout=300)
        _button(app, "确认并提交审批").click().run(timeout=300)

        text = _page_text(app)
        for label in ("编号", "作业名称", "状态", "风险等级", "区域", "申请人", "作业负责人"):
            self.assertIn(label, text)
        self.assertIn("当前需要你做什么", text)
        self.assertIn("审核 SDS 证据与 JSA", text)

        sections = [item.label for item in app.expander]
        for section in DETAIL_SECTIONS:
            self.assertIn(section, sections)

    def test_permit_detail_keeps_sds_and_public_evidence_apart(self) -> None:
        app = self._app()
        app.selectbox(key="demo_user_id").set_value("DEMO-EHS-01").run(timeout=300)
        _button(app, "确认并提交审批").click().run(timeout=300)

        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("【SDS 证据】", markdown)
        self.assertIn("【公开安全证据（非 SDS）】", markdown)
        self.assertIn("Demo / Synthetic", markdown)
        self.assertIn("公开来源证据不是 SDS", _page_text(app))

    def test_returning_a_permit_requires_a_reason_and_writes_audit(self) -> None:
        app = self._app()
        app.selectbox(key="demo_user_id").set_value("DEMO-EHS-01").run(timeout=300)
        _button(app, "确认并提交审批").click().run(timeout=300)

        _button(app, "退回申请人").click().run(timeout=300)
        self.assertEqual([item.value for item in app.exception], [])
        reason = app.text_area(key="reason_PERMIT-DEMO-003_return_draft")
        reason.set_value("SDS 证据链不完整，请补充后再提交。").run(timeout=300)
        _button(app, "确认执行").click().run(timeout=300)
        self.assertEqual([item.value for item in app.exception], [])

        connection = self._connection()
        permit = permit_service.get_permit(connection, "PERMIT-DEMO-003")
        assert permit is not None
        self.assertEqual(permit["status"], "draft")

    # ------------------------------------------------------------- hazard --

    def test_hazard_detail_separates_rectification_from_verification(self) -> None:
        app = self._app()
        app.selectbox(key="demo_user_id").set_value("DEMO-EHS-01").run(timeout=300)
        button = _button(app, "验证通过")
        self.assertIsNotNone(button)
        button.click().run(timeout=300)
        self.assertEqual([item.value for item in app.exception], [])

        title = "\n".join(item.value for item in app.title)
        self.assertIn("HZ-DEMO-002", title)
        text = _page_text(app)
        self.assertIn("当前需要你做什么", text)
        self.assertIn("整改负责人已提交整改证据", text)

    # ------------------------------------------------------------ records --

    def test_sds_library_starts_unloaded_and_never_auto_loads(self) -> None:
        app = self._app()
        _nav(app, common.PAGE_RECORDS)
        text = _page_text(app)
        self.assertIn("SDS 知识库：未加载", text)
        self.assertNotIn("正在自动加载", text)
        self.assertIsNotNone(_button(app, "加载 Synthetic SDS 知识库"))
        self.assertEqual(app.session_state[common.SDS_STATE_KEY], records.STATE_NOT_LOADED)

    def test_audit_tab_lists_operation_records(self) -> None:
        app = self._app()
        _nav(app, common.PAGE_RECORDS)
        self.assertEqual(
            [item.label for item in app.tabs], ["SDS知识库", "审计记录"]
        )
        self.assertGreaterEqual(len(app.dataframe), 1)
        text = _page_text(app)
        self.assertIn("资料与审计", text)

    def test_overview_answers_the_six_management_questions(self) -> None:
        app = self._app()
        _nav(app, common.PAGE_OVERVIEW)
        labels = [item.label for item in app.metric]
        self.assertEqual(
            labels,
            [
                "执行中许可",
                "待审批",
                "即将到期许可",
                "高/重大未关闭许可",
                "逾期隐患",
                "待EHS验证隐患",
            ],
        )

    def test_permit_list_renders_the_required_columns(self) -> None:
        app = self._app()
        _nav(app, common.PAGE_PERMITS)
        frames = app.dataframe
        self.assertGreaterEqual(len(frames), 1)
        columns = list(frames[0].value.columns)
        for column in ("编号", "名称", "区域", "风险", "状态", "负责人", "有效期", "当前下一动作", "是否逾期"):
            self.assertIn(column, columns)

    def test_hazard_list_renders_the_required_columns(self) -> None:
        app = self._app()
        _nav(app, common.PAGE_HAZARDS)
        frames = app.dataframe
        self.assertGreaterEqual(len(frames), 1)
        columns = list(frames[0].value.columns)
        for column in ("隐患编号", "来源Permit", "描述", "风险", "整改负责人", "截止日期", "状态", "是否逾期", "下一动作"):
            self.assertIn(column, columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)

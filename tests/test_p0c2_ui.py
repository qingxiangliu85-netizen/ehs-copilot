"""P0C.2 regression tests: product positioning, the visible main flow, and the
reworked Demo data.

What this suite protects (one class per acceptance item):

* the Demo keeps **exactly one** HF permit — the guided golden case — and the
  other records demonstrate different high-risk work types;
* the home page states what the product is and offers the 「体验3分钟完整流程」
  entry that opens the golden permit;
* the default Demo identity is the EHS reviewer, not the administrator;
* every permit detail shows the six-step main flow derived from the existing
  permit status, and the highlighted stage matches the status;
* a hazard shows the permit it came from and where it sits in the execution
  loop (发现隐患 → 指派整改 → 提交整改 → EHS验证 → 隐患关闭);
* the permission layer, the action queue and the SDS / public-evidence boundary
  are untouched by the Demo rework.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

import db
import demo_seed
from services import hazard_service, permit_service, persona_service
from ui import common, product, worklist
from workflow import permit_state

PROJECT_ROOT = Path(__file__).resolve().parents[1]

FLOW_STAGES = ("申请", "EHS审核", "审批", "开工前检查", "执行", "交还/关闭")

# status → expected step index in the six-step main flow
EXPECTED_FLOW_INDEX = {
    permit_state.PERMIT_DRAFT: 0,
    permit_state.PERMIT_RETURNED: 0,
    permit_state.PERMIT_EHS_REVIEW: 1,
    permit_state.PERMIT_APPROVAL_PENDING: 2,
    permit_state.PERMIT_APPROVED: 3,
    permit_state.PERMIT_ACTIVE: 4,
    permit_state.PERMIT_SUSPENDED: 4,
    permit_state.PERMIT_CLOSEOUT_REVIEW: 5,
    permit_state.PERMIT_CLOSED: 5,
    permit_state.PERMIT_CANCELLED: 5,
    permit_state.PERMIT_EXPIRED: 5,
}


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


# --------------------------------------------------------------------------- #
# 1 + 5 + 7 — pure vocabulary / mapping checks
# --------------------------------------------------------------------------- #


class ProductPositioningTests(unittest.TestCase):
    """The product says one consistent thing, and the flow is a display map."""

    def test_positioning_is_permit_centric_and_single(self) -> None:
        self.assertEqual(product.PRODUCT_NAME, "EHS Copilot")
        self.assertIn("作业许可", product.PRODUCT_CATEGORY)
        self.assertIn("隐患", product.PRODUCT_CATEGORY)
        # The one-liner must cover the whole loop, not one module.
        self.assertIn("作业申请", product.PRODUCT_ONE_LINER)
        self.assertIn("整改关闭", product.PRODUCT_ONE_LINER)
        self.assertIn("许可单", product.PRODUCT_ONE_LINER)
        # The hero headline is the end-to-end promise, not one module.
        self.assertIn("作业", product.PRODUCT_HERO_TITLE)
        self.assertIn("关闭", product.PRODUCT_HERO_TITLE)
        self.assertTrue(product.PRODUCT_HERO_DESCRIPTION)
        # The AI boundary must stay explicit.
        self.assertIn("人工", product.PRODUCT_AI_SCOPE)
        self.assertIn("人工风险评估", product.PRODUCT_BOUNDARY)

    def test_the_positioning_never_turns_into_a_full_ehs_platform_claim(self) -> None:
        blob = " ".join(
            [
                product.PRODUCT_CATEGORY,
                product.PRODUCT_HERO_DESCRIPTION,
                product.PRODUCT_ONE_LINER,
                product.PRODUCT_AI_SCOPE,
            ]
        )
        for claim in ("全功能", "SDS 搜索工具", "Agent Demo", "HF 管理系统"):
            self.assertNotIn(claim, blob)

    def test_demo_work_types_cover_more_than_four_categories(self) -> None:
        self.assertGreaterEqual(len(product.WORK_TYPES), 4)
        self.assertEqual(len(set(product.WORK_TYPES)), len(product.WORK_TYPES))
        self.assertIn("危化品作业", product.WORK_TYPES)
        self.assertIn("动火作业", product.WORK_TYPES)

    def test_default_persona_is_not_the_demo_admin(self) -> None:
        self.assertNotEqual(common.DEFAULT_USER_ID, "DEMO-ADMIN-01")
        self.assertEqual(common.DEFAULT_USER_ID, "DEMO-EHS-01")
        # The administrator is still available for global inspection.
        self.assertIn("DEMO-ADMIN-01", dict(common.PERSONA_CHOICES))

    def test_the_main_flow_has_six_stages(self) -> None:
        self.assertEqual(common.PERMIT_STAGE_ORDER, FLOW_STAGES)
        self.assertEqual(tuple(product.HAZARD_SUBFLOW[:5]), ("现场执行", "发现隐患", "指派整改", "提交整改", "EHS验证"))
        self.assertEqual(product.HAZARD_SUBFLOW[5], "隐患关闭")

    def test_every_status_maps_onto_exactly_one_flow_position(self) -> None:
        for status in permit_state.PERMIT_STATUSES:
            with self.subTest(status=status):
                flow = common.permit_flow({"status": status})
                self.assertEqual(flow["total"], 6)
                self.assertEqual(len(flow["stages"]), 6)
                self.assertEqual(flow["index"], EXPECTED_FLOW_INDEX[status])
                self.assertTrue(str(flow["position_text"]).strip())

    def test_the_highlighted_stage_matches_the_status(self) -> None:
        for status, expected in EXPECTED_FLOW_INDEX.items():
            with self.subTest(status=status):
                flow = common.permit_flow({"status": status})
                current = [
                    index
                    for index, stage in enumerate(flow["stages"])
                    if stage["state"] == common.STAGE_CURRENT
                ]
                done = [
                    index
                    for index, stage in enumerate(flow["stages"])
                    if stage["state"] == common.STAGE_DONE
                ]
                stopped = [
                    index
                    for index, stage in enumerate(flow["stages"])
                    if stage["state"] == common.STAGE_STOPPED
                ]
                if status == permit_state.PERMIT_CLOSED:
                    self.assertEqual(current, [])
                    self.assertEqual(stopped, [])
                    self.assertEqual(done, list(range(6)))
                    continue
                if status in (
                    permit_state.PERMIT_CANCELLED,
                    permit_state.PERMIT_EXPIRED,
                ):
                    # A terminated permit stops at its last stage: no stage is
                    # "current", and the stopping point is called out.
                    self.assertEqual(current, [])
                    self.assertEqual(stopped, [expected])
                    self.assertEqual(done, list(range(expected)))
                    continue
                self.assertEqual(current, [expected])
                self.assertEqual(stopped, [])
                self.assertEqual(done, list(range(expected)))

    def test_a_cancelled_permit_stops_at_its_last_stage(self) -> None:
        flow = common.permit_flow({"status": permit_state.PERMIT_CANCELLED})
        self.assertTrue(flow["terminated"])
        self.assertEqual(flow["stages"][5]["state"], common.STAGE_STOPPED)

    def test_the_flow_is_not_a_second_state_machine(self) -> None:
        # The display map may only re-label statuses the state machine already
        # knows; it must not add or remove any.
        self.assertEqual(
            set(common.PERMIT_STAGE_LABELS), set(permit_state.PERMIT_STATUSES)
        )
        for status in permit_state.PERMIT_STATUSES:
            self.assertIn(common.permit_stage_label(status), FLOW_STAGES)


# --------------------------------------------------------------------------- #
# 1 + 2 + 8 + 9 + 10 — the reworked Demo store
# --------------------------------------------------------------------------- #


class DemoStoreP0C2Tests(unittest.TestCase):
    """The Demo must look like a permit-to-work product, not an HF tool."""

    def setUp(self) -> None:
        self.connection = db.open_database(":memory:")
        self.addCleanup(self.connection.close)
        self.result = demo_seed.seed_demo_data(self.connection)

    def _permits(self) -> list[dict[str, object]]:
        return permit_service.list_permits(self.connection, limit=500)

    def test_exactly_one_hf_golden_case_exists(self) -> None:
        hf = [row for row in self._permits() if "HF" in str(row["title"])]
        self.assertEqual(len(hf), 1)
        self.assertEqual(str(hf[0]["id"]), demo_seed.GOLDEN_PERMIT_ID)
        self.assertEqual(str(hf[0]["permit_type"]), demo_seed.GOLDEN_WORK_TYPE)
        self.assertEqual(str(hf[0]["title"]), demo_seed.GOLDEN_PERMIT_TITLE)

        chemicals = self.connection.execute(
            "SELECT permit_id FROM permit_chemicals WHERE chemical_name = ?",
            ("氢氟酸",),
        ).fetchall()
        self.assertEqual(
            [str(row["permit_id"]) for row in chemicals],
            [demo_seed.GOLDEN_PERMIT_ID],
        )

    def test_the_demo_covers_at_least_four_work_types(self) -> None:
        types = demo_seed.work_types_seeded(self.connection)
        self.assertGreaterEqual(len(types), 4)
        for expected in ("危化品作业", "动火作业", "受限空间作业", "电气隔离作业", "高处作业", "开挖作业"):
            self.assertIn(expected, types)
        self.assertEqual(len(types), len(set(types)))

    def test_each_permit_carries_a_coherent_evidence_bundle(self) -> None:
        """A seeded permit must be able to pass the submit precondition."""
        for summary in self._permits():
            permit = permit_service.get_permit(self.connection, str(summary["id"]))
            assert permit is not None
            with self.subTest(permit=permit["id"]):
                self.assertTrue(permit["chemicals"])
                self.assertTrue(permit["evidence"]["sds"])
                self.assertTrue(permit["jsa_items"])

    def test_every_demo_record_is_labelled_as_simulated(self) -> None:
        for summary in self._permits():
            with self.subTest(permit=summary["id"]):
                self.assertTrue(summary["is_demo"])
                self.assertIn("Demo", str(summary["data_label"]))
        for hazard in hazard_service.list_hazards(self.connection, limit=500):
            with self.subTest(hazard=hazard["id"]):
                self.assertTrue(hazard["is_demo"])

    def test_hazards_stay_linked_to_the_permit_they_came_from(self) -> None:
        permits = {str(row["id"]) for row in self._permits()}
        hazards = hazard_service.list_hazards(self.connection, limit=500)
        self.assertGreaterEqual(len(hazards), 4)
        for hazard in hazards:
            with self.subTest(hazard=hazard["id"]):
                self.assertIn(str(hazard["permit_id"]), permits)

        golden = permit_service.get_permit(
            self.connection, demo_seed.GOLDEN_PERMIT_ID
        )
        assert golden is not None
        self.assertTrue(golden["linked_hazard_ids"])
        self.assertGreater(golden["open_hazard_count"], 0)

    def test_distinct_hazard_scenarios_are_seeded(self) -> None:
        statuses = {
            str(hazard["status"])
            for hazard in hazard_service.list_hazards(self.connection, limit=500)
        }
        self.assertIn("assigned", statuses)
        self.assertIn("verification_pending", statuses)
        self.assertIn("closed", statuses)
        self.assertIn("open", statuses)

    def test_permission_rules_survive_the_demo_rework(self) -> None:
        from ui import commands

        applicant = persona_service.get_user(self.connection, "DEMO-APPLICANT-01")
        owner = persona_service.get_user(self.connection, "DEMO-OWNER-01")
        reviewer = persona_service.get_user(self.connection, "DEMO-EHS-01")
        assert applicant is not None and owner is not None and reviewer is not None

        ok, _ = commands.execute(
            self.connection, "permit", "PERMIT-DEMO-002", "approve", user=applicant
        )
        self.assertFalse(ok)
        self.assertEqual(
            permit_service.get_permit(self.connection, "PERMIT-DEMO-002")["status"],
            permit_state.PERMIT_APPROVAL_PENDING,
        )

        hazard = hazard_service.get_hazard(self.connection, "HZ-DEMO-002")
        assert hazard is not None
        owner_codes = {
            action.code
            for action in worklist.actions_for("hazard", hazard, owner)
        }
        reviewer_codes = {
            action.code
            for action in worklist.actions_for("hazard", hazard, reviewer)
        }
        self.assertNotIn("verify_pass", owner_codes)
        self.assertIn("verify_pass", reviewer_codes)

    def test_the_action_queue_still_splits_by_persona(self) -> None:
        def queue(user_id: str) -> list[dict[str, object]]:
            user = persona_service.get_user(self.connection, user_id)
            assert user is not None
            return worklist.build_worklist(self.connection, user)

        approver = queue("DEMO-APPROVER-01")
        self.assertTrue(approver)
        self.assertTrue(all(row["entity_type"] == "permit" for row in approver))

        reviewer = queue("DEMO-EHS-01")
        categories = {row["category"] for row in reviewer}
        self.assertIn(worklist.CATEGORY_REVIEW, categories)

        owner = queue("DEMO-OWNER-01")
        owner_actions = {row["action_type"] for row in owner}
        self.assertTrue(owner_actions)
        self.assertNotIn("verify", owner_actions)
        self.assertNotIn("approve", owner_actions)

        applicant = queue("DEMO-APPLICANT-01")
        self.assertTrue(applicant)
        self.assertEqual(
            {str(row["entity_id"]) for row in applicant},
            {"PERMIT-DEMO-001", "PERMIT-DEMO-005"},
        )

    def test_sds_and_public_evidence_stay_apart(self) -> None:
        golden = permit_service.get_permit(
            self.connection, demo_seed.GOLDEN_PERMIT_ID
        )
        assert golden is not None
        evidence = golden["evidence"]
        self.assertEqual(len(evidence["sds"]), 1)
        self.assertEqual(len(evidence["public_sources"]), 1)
        self.assertEqual(evidence["sds"][0]["data_label"], "Demo / Synthetic SDS")
        self.assertTrue(evidence["sds"][0]["is_demo"])
        self.assertGreater(evidence["sds"][0]["page"], 0)
        # A public-source row can never be read as an SDS: no file, no page.
        self.assertEqual(evidence["public_sources"][0]["page"], 0)
        self.assertEqual(evidence["public_sources"][0]["source"], "")
        self.assertTrue(evidence["public_sources"][0]["source_url"])

        for summary in self._permits():
            permit = permit_service.get_permit(self.connection, str(summary["id"]))
            assert permit is not None
            for row in permit["evidence"]["public_sources"]:
                self.assertEqual(row["source"], "")

    def test_the_golden_demo_walks_the_full_flow_and_hazards_gate_the_close(
        self,
    ) -> None:
        """The golden story really runs: 申请 → 审核 → 审批 → 开工 → 执行 → 整改 → 关闭."""
        from ui import commands

        connection = self.connection
        golden = demo_seed.GOLDEN_PERMIT_ID
        applicant = persona_service.get_user(connection, "DEMO-APPLICANT-01")
        reviewer = persona_service.get_user(connection, "DEMO-EHS-01")
        approver = persona_service.get_user(connection, "DEMO-APPROVER-01")
        owner = persona_service.get_user(connection, "DEMO-OWNER-01")
        assert applicant and reviewer and approver and owner

        # Step 1 — the applicant submits the draft for EHS review.
        self.assertEqual(
            permit_service.get_permit(connection, golden)["status"],
            permit_state.PERMIT_DRAFT,
        )
        ok, message = commands.execute(
            connection, "permit", golden, "submit", user=applicant
        )
        self.assertTrue(ok, message)
        self.assertEqual(
            permit_service.get_permit(connection, golden)["status"],
            permit_state.PERMIT_EHS_REVIEW,
        )

        # Step 2 — EHS review of the SDS evidence and JSA.
        ok, message = commands.execute(
            connection, "permit", golden, "confirm", user=reviewer, reason="SDS 与 JSA 已核对"
        )
        self.assertTrue(ok, message)
        self.assertEqual(
            permit_service.get_permit(connection, golden)["status"],
            permit_state.PERMIT_APPROVAL_PENDING,
        )

        # Step 3 — approval.
        ok, message = commands.execute(
            connection, "permit", golden, "approve", user=approver, reason="同意作业"
        )
        self.assertTrue(ok, message)
        self.assertEqual(
            permit_service.get_permit(connection, golden)["status"],
            permit_state.PERMIT_APPROVED,
        )

        # Step 4 — pre-start check, then execution.
        checks = [
            {"item_code": "PPE", "item_text": "防护装备就位", "required": True, "result": "pass"},
            {"item_code": "GAS", "item_text": "气体检测合格", "required": True, "result": "pass"},
        ]
        ok, message = commands.execute(
            connection, "permit", golden, "prestart_confirm", user=owner, checks=checks
        )
        self.assertTrue(ok, message)
        self.assertEqual(
            permit_service.get_permit(connection, golden)["status"],
            permit_state.PERMIT_ACTIVE,
        )

        # Step 8 begins — but the linked hazard must close first.
        ok, message = commands.execute(
            connection, "permit", golden, "complete_work", user=owner, reason="作业完成并交还现场"
        )
        self.assertTrue(ok, message)
        self.assertEqual(
            permit_service.get_permit(connection, golden)["status"],
            permit_state.PERMIT_CLOSEOUT_REVIEW,
        )

        ok, message = commands.execute(
            connection, "permit", golden, "close", user=reviewer, reason="申请关闭"
        )
        self.assertFalse(ok, "存在未关闭隐患时不应允许关闭作业许可")

        # Step 5-7 — the hazard loop: 整改 → 提交 → EHS 验证。
        ok, message = commands.execute(
            connection, "hazard", "HZ-DEMO-005", "start", user=owner
        )
        self.assertTrue(ok, message)
        ok, message = commands.execute(
            connection,
            "hazard",
            "HZ-DEMO-005",
            "submit_rectification",
            user=owner,
            reason="已补齐应急冲洗设施点检记录",
            evidence=[{"file_name": "（占位）点检记录（模拟）.xlsx", "evidence_type": "记录"}],
        )
        self.assertTrue(ok, message)
        self.assertEqual(
            hazard_service.get_hazard(connection, "HZ-DEMO-005")["status"],
            "verification_pending",
        )
        ok, message = commands.execute(
            connection, "hazard", "HZ-DEMO-005", "verify_pass", user=reviewer, reason="现场复核通过"
        )
        self.assertTrue(ok, message)
        self.assertEqual(
            hazard_service.get_hazard(connection, "HZ-DEMO-005")["status"], "closed"
        )

        # Step 8 — now the permit may close.
        ok, message = commands.execute(
            connection, "permit", golden, "close", user=reviewer, reason="关联隐患已关闭"
        )
        self.assertTrue(ok, message)
        self.assertEqual(
            permit_service.get_permit(connection, golden)["status"],
            permit_state.PERMIT_CLOSED,
        )


# --------------------------------------------------------------------------- #
# 3 + 4 + 5 + 6 + 7 + 8 — the rendered pages
# --------------------------------------------------------------------------- #


class ProductUiP0C2Tests(unittest.TestCase):
    """The real Streamlit app, driven through ``AppTest``."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._original = db.EHS_DB_PATH
        db.EHS_DB_PATH = str(Path(self._temp.name) / "p0c2.db")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        db.EHS_DB_PATH = self._original
        self._temp.cleanup()

    def _app(self) -> AppTest:
        app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=300).run()
        if app.exception:
            self.fail(f"页面抛出异常：{[item.value for item in app.exception]}")
        return app

    def test_homepage_states_the_product_and_the_ai_boundary(self) -> None:
        app = self._app()
        text = _page_text(app)
        self.assertIn(product.PRODUCT_HERO_TITLE, text)
        self.assertIn(product.PRODUCT_HERO_DESCRIPTION, text)
        self.assertIn(product.PRODUCT_ONE_LINER, text)
        self.assertIn(product.PRODUCT_AI_SCOPE, text)
        self.assertIn(product.PRODUCT_CATEGORY, text)

    def test_homepage_offers_the_golden_demo_entry(self) -> None:
        app = self._app()
        button = _button(app, product.GOLDEN_ENTRY_LABEL)
        self.assertIsNotNone(button)
        text = _page_text(app)
        self.assertIn(product.GOLDEN_ENTRY_CAPTION, text)
        self.assertIn(product.GOLDEN_PERMIT_TITLE, text)

        button.click().run(timeout=300)
        self.assertEqual([item.value for item in app.exception], [])
        title = "\n".join(item.value for item in app.title)
        self.assertIn(demo_seed.GOLDEN_PERMIT_ID, title)
        self.assertIn(product.GOLDEN_PERMIT_TITLE, title)

    def test_the_ui_defaults_to_the_ehs_reviewer(self) -> None:
        app = self._app()
        self.assertEqual(
            app.selectbox(key=common.USER_KEY).value, common.DEFAULT_USER_ID
        )
        self.assertEqual(common.DEFAULT_USER_ID, "DEMO-EHS-01")

    def test_homepage_keeps_the_four_headline_numbers_and_an_alert_list(self) -> None:
        app = self._app()
        labels = [item.label for item in app.metric]
        self.assertEqual(labels, ["我的待办", "已逾期", "今日到期", "高/重大风险"])
        text = _page_text(app)
        self.assertIn("需要优先关注", text)
        self.assertIn("最近活动", text)

    def test_permit_detail_shows_the_six_step_main_flow(self) -> None:
        app = self._app()
        _button(app, product.GOLDEN_ENTRY_LABEL).click().run(timeout=300)
        text = _page_text(app)
        self.assertIn("主流程进度", text)
        for stage in FLOW_STAGES:
            self.assertIn(stage, text)
        # The golden Demo now starts as a draft, so the flow opens at step 1.
        self.assertIn("当前阶段：申请", text)
        self.assertIn("第 1 / 6 步", text)
        self.assertIn("当前需要你做什么", text)
        self.assertIn("下一步建议", text)

    def test_the_golden_demo_page_points_at_the_next_role(self) -> None:
        """The story is walkable: each step suggests the next role to switch to."""
        app = self._app()
        _button(app, product.GOLDEN_ENTRY_LABEL).click().run(timeout=300)
        self.assertIn("下一步建议切换为：**作业申请人**", _page_text(app))

        # 1 — switch to the applicant and submit the draft for EHS review.
        _button(app, "切换为：作业申请人（Demo）").click().run(timeout=300)
        _button(app, "提交EHS审核").click().run(timeout=300)
        self.assertEqual([item.value for item in app.exception], [])
        text = _page_text(app)
        self.assertIn("当前阶段：EHS审核", text)
        self.assertIn("第 2 / 6 步", text)
        self.assertIn("下一步建议切换为：**EHS审核人**", text)

        # 2 — switch to the EHS reviewer and confirm.
        _button(app, "切换为：EHS审核人（Demo）").click().run(timeout=300)
        _button(app, "确认并提交审批").click().run(timeout=300)
        self.assertEqual([item.value for item in app.exception], [])
        text = _page_text(app)
        self.assertIn("当前阶段：审批", text)
        self.assertIn("第 3 / 6 步", text)
        self.assertIn("下一步建议切换为：**作业审批人**", text)

        # The switch is offered, but it is only a Demo identity change and the
        # help text says so explicitly.
        switch = _button(app, "切换为：作业审批人（Demo）")
        self.assertIsNotNone(switch)
        self.assertIn("不会替你做出任何审批或验证决定", str(switch.help or ""))

    def test_permit_detail_shows_the_hazard_sub_flow(self) -> None:
        app = self._app()
        _button(app, product.GOLDEN_ENTRY_LABEL).click().run(timeout=300)
        text = _page_text(app)
        self.assertIn("执行阶段的隐患支线", text)
        for step in product.HAZARD_SUBFLOW:
            self.assertIn(step, text)

    def test_permit_list_shows_the_work_type_column_and_filter(self) -> None:
        app = self._app()
        _nav(app, common.PAGE_PERMITS)
        frames = app.dataframe
        self.assertGreaterEqual(len(frames), 1)
        frame = frames[0].value
        self.assertIn("作业类型", list(frame.columns))
        types = {str(value) for value in frame["作业类型"]}
        self.assertGreaterEqual(len(types), 4)
        self.assertIn("危化品作业", types)
        self.assertIn("动火作业", types)
        self.assertIn(
            "作业类型筛选", [item.label for item in app.selectbox]
        )

    def test_hazard_detail_shows_the_source_permit_relation(self) -> None:
        app = self._app()
        button = _button(app, "验证通过")
        self.assertIsNotNone(button)
        button.click().run(timeout=300)
        self.assertEqual([item.value for item in app.exception], [])
        text = _page_text(app)
        self.assertIn("这条隐患在作业流程中的位置", text)
        self.assertIn("来源作业许可", text)
        self.assertIn("PERMIT-DEMO-006", text)
        self.assertIn("屋面风机维护作业（模拟）", text)

    def test_the_rendered_position_text_matches_the_status(self) -> None:
        rows = [
            (permit_state.PERMIT_DRAFT, "当前阶段：申请"),
            (permit_state.PERMIT_APPROVAL_PENDING, "当前阶段：审批"),
            (permit_state.PERMIT_ACTIVE, "当前阶段：执行"),
            (permit_state.PERMIT_CLOSEOUT_REVIEW, "当前阶段：交还/关闭"),
            (permit_state.PERMIT_CLOSED, "主流程六步已全部完成"),
        ]
        for status, expected in rows:
            with self.subTest(status=status):
                flow = common.permit_flow({"status": status})
                self.assertIn(expected, flow["position_text"])

    def test_pages_still_render_without_technical_vocabulary(self) -> None:
        app = self._app()
        for page in common.NAV_PAGES:
            with self.subTest(page=page):
                _nav(app, page)
                self.assertEqual([item.value for item in app.exception], [])
                text = _page_text(app)
                for term in ("LangGraph", "Router", "Tool Calling", "Agent节点"):
                    self.assertNotIn(term, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

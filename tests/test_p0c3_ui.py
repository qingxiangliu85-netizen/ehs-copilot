"""P0C.3 regression tests — the final product convergence.

What this suite protects (one class per acceptance item of the P0C.3 brief):

* 许可有效期 is a *time* fact, kept apart from the work's risk level, and a
  lapsed window can never be started or resumed;
* 暂停作业 / 恢复作业 move the permit through a real state transition and both
  leave an audit trail, and a lapsed window blocks the resume;
* 提交作业完成 / 现场交还 / 确认关闭 is an explicit gate: work completion is not
  a closure, and an open linked hazard blocks the close;
* the golden Demo's action queue changes correctly at every one of its steps,
  for every persona;
* the six-step flow bar always agrees with the stored status;
* the next-role hint is present, correct and never switches the identity by
  itself;
* the pages show the responsible roles on the first screen and never print a
  raw status identifier such as ``closeout_review``.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from streamlit.testing.v1 import AppTest

import db
import demo_seed
from services import hazard_service, permit_service, persona_service
from ui import commands, common, product, worklist
from workflow import action_queue, audit, permit_state

PROJECT_ROOT = Path(__file__).resolve().parents[1]

FLOW_STAGES = ("申请", "EHS审核", "审批", "开工前检查", "执行", "交还/关闭")

PRESTART_CHECKS = [
    {"item_code": "PPE", "item_text": "防护装备就位", "required": True, "result": "pass"},
    {"item_code": "GAS", "item_text": "气体检测合格", "required": True, "result": "pass"},
]


def _page_text(app: AppTest) -> str:
    """Return every visible string of one rendered page, banners included."""
    parts: list[str] = []
    for collection in (
        app.markdown,
        app.caption,
        app.text,
        app.title,
        app.subheader,
        app.error,
        app.warning,
        app.info,
        app.success,
    ):
        parts.extend(str(item.value) for item in collection)
    return "\n".join(parts)


def _button(app: AppTest, label: str):
    return next((item for item in app.button if item.label == label), None)


def _nav(app: AppTest, page: str) -> AppTest:
    return app.radio(key="nav_page").set_value(page).run(timeout=300)


def _user(connection, user_id: str) -> dict:
    user = persona_service.get_user(connection, user_id)
    assert user is not None, f"未找到 Demo 用户 {user_id}"
    return user


def _queue(connection, user_id: str) -> set[tuple[str, str]]:
    """Return ``{(entity_id, action_type)}`` for one persona's worklist."""
    rows = worklist.build_worklist(connection, _user(connection, user_id))
    return {(str(row["entity_id"]), str(row["action_type"])) for row in rows}


# --------------------------------------------------------------------------- #
# 2 — 许可有效期 (time), never merged with 风险 (severity)
# --------------------------------------------------------------------------- #


class PermitValidityTests(unittest.TestCase):
    """``common.permit_validity`` is the single time view of a permit."""

    MOMENT = datetime(2026, 6, 15, 9, 0, 0)

    def _validity(self, valid_to: str, valid_from: str = "2026-06-01T00:00:00"):
        return common.permit_validity(
            {"valid_from": valid_from, "valid_to": valid_to}, self.MOMENT
        )

    def test_a_far_future_window_is_in_force(self) -> None:
        validity = self._validity("2026-07-01T00:00:00")
        self.assertEqual(validity["state"], common.VALIDITY_IN_FORCE)
        self.assertTrue(validity["in_force"])
        self.assertFalse(validity["is_expired"])

    def test_a_window_ending_tomorrow_is_expiring(self) -> None:
        validity = self._validity("2026-06-16T00:00:00")
        self.assertEqual(validity["state"], common.VALIDITY_EXPIRING)
        self.assertTrue(validity["is_expiring"])
        self.assertFalse(validity["is_expired"])

    def test_a_window_that_has_passed_is_expired(self) -> None:
        validity = self._validity("2026-06-10T00:00:00")
        self.assertEqual(validity["state"], common.VALIDITY_EXPIRED)
        self.assertTrue(validity["is_expired"])
        self.assertFalse(validity["in_force"])

    def test_a_permit_without_a_window_is_unset(self) -> None:
        validity = common.permit_validity({"valid_from": "", "valid_to": ""}, self.MOMENT)
        self.assertEqual(validity["state"], common.VALIDITY_UNSET)
        self.assertFalse(validity["is_expired"])
        self.assertFalse(validity["in_force"])

    def test_the_time_view_follows_the_state_machine_rule(self) -> None:
        """The page may never claim usable what the service would refuse."""
        past = "2026-06-10T00:00:00"
        validity = self._validity(past)
        result = permit_state.validate_permit_transition(
            permit_state.PERMIT_APPROVED,
            "prestart_confirm",
            actor_role="action_owner",
            context={"prestart_passed": True, "valid_to": past},
            now=self.MOMENT,
        )
        self.assertTrue(validity["is_expired"])
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, permit_state.CODE_PRECONDITION_FAILED)

    def test_time_and_risk_are_independent_dimensions(self) -> None:
        # A low-risk permit can be expired …
        low_expired = common.permit_validity(
            {"risk_level": "低", "valid_to": "2026-06-10T00:00:00"}, self.MOMENT
        )
        self.assertTrue(low_expired["is_expired"])
        # … and a high-risk permit can still be in force.
        high_valid = common.permit_validity(
            {"risk_level": "高", "valid_to": "2026-07-10T00:00:00"}, self.MOMENT
        )
        self.assertTrue(high_valid["in_force"])


# --------------------------------------------------------------------------- #
# 2 — 许可过期后不能开工
# --------------------------------------------------------------------------- #


class ExpiredPermitCannotStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = db.open_database(":memory:")
        self.addCleanup(self.connection.close)
        demo_seed.seed_demo_data(self.connection)
        self.owner = _user(self.connection, demo_seed.OWNER)

    def test_the_demo_carries_one_expired_approved_permit(self) -> None:
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-004")
        assert permit is not None
        self.assertEqual(permit["status"], permit_state.PERMIT_APPROVED)
        self.assertTrue(common.permit_validity(permit)["is_expired"])

    def test_an_expired_permit_cannot_start_work(self) -> None:
        ok, message = commands.execute(
            self.connection,
            "permit",
            "PERMIT-DEMO-004",
            "prestart_confirm",
            user=self.owner,
            checks=PRESTART_CHECKS,
        )
        self.assertFalse(ok)
        self.assertIn("有效期", message)
        # The status must not move on a refused start.
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-004")
        assert permit is not None
        self.assertEqual(permit["status"], permit_state.PERMIT_APPROVED)

    def test_the_service_gate_is_the_state_machine(self) -> None:
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-004")
        assert permit is not None
        result = permit_state.validate_permit_transition(
            permit_status := str(permit["status"]),
            "prestart_confirm",
            actor_role="action_owner",
            context={
                "prestart_passed": True,
                "valid_to": str(permit.get("valid_to", "")),
            },
        )
        self.assertEqual(permit_status, permit_state.PERMIT_APPROVED)
        self.assertFalse(result.allowed)
        self.assertIn("有效期", result.message)

    def test_an_overdue_permit_can_be_marked_expired(self) -> None:
        permit_service.expire_permit(self.connection, "PERMIT-DEMO-004")
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-004")
        assert permit is not None
        self.assertEqual(permit["status"], permit_state.PERMIT_EXPIRED)

    def test_a_valid_permit_may_still_start(self) -> None:
        """The gate refuses the lapsed window only — not every start."""
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-004")
        assert permit is not None
        future = (datetime.now() + timedelta(days=3)).isoformat(timespec="seconds")
        self.connection.execute(
            "UPDATE permits SET valid_to = ? WHERE id = ?",
            (future, "PERMIT-DEMO-004"),
        )
        self.connection.commit()
        ok, message = commands.execute(
            self.connection,
            "permit",
            "PERMIT-DEMO-004",
            "prestart_confirm",
            user=self.owner,
            checks=PRESTART_CHECKS,
        )
        self.assertTrue(ok, message)
        refreshed = permit_service.get_permit(self.connection, "PERMIT-DEMO-004")
        assert refreshed is not None
        self.assertEqual(refreshed["status"], permit_state.PERMIT_ACTIVE)


# --------------------------------------------------------------------------- #
# 3 — 暂停作业 / 恢复作业
# --------------------------------------------------------------------------- #


class SuspendResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = db.open_database(":memory:")
        self.addCleanup(self.connection.close)
        demo_seed.seed_demo_data(self.connection)
        self.owner = _user(self.connection, demo_seed.OWNER)

    def _actions(self, permit_id: str) -> list[str]:
        return [
            str(event["action"])
            for event in audit.list_events(
                self.connection, entity_type="permit", entity_id=permit_id
            )
        ]

    def test_the_demo_carries_one_suspended_permit_in_force(self) -> None:
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-007")
        assert permit is not None
        self.assertEqual(permit["status"], permit_state.PERMIT_SUSPENDED)
        self.assertTrue(common.permit_validity(permit)["in_force"])
        self.assertIn("permit.suspend", self._actions("PERMIT-DEMO-007"))

    def test_suspend_requires_a_reason(self) -> None:
        ok, message = commands.execute(
            self.connection, "permit", "PERMIT-DEMO-007", "suspend", user=self.owner
        )
        self.assertFalse(ok)
        self.assertTrue(message)

    def test_resume_then_suspend_moves_state_and_writes_audit(self) -> None:
        # resume 恢复作业 → 执行中
        ok, message = commands.execute(
            self.connection, "permit", "PERMIT-DEMO-007", "resume", user=self.owner
        )
        self.assertTrue(ok, message)
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-007")
        assert permit is not None
        self.assertEqual(permit["status"], permit_state.PERMIT_ACTIVE)

        # suspend 暂停作业 → 已暂停, with a reason
        ok, message = commands.execute(
            self.connection,
            "permit",
            "PERMIT-DEMO-007",
            "suspend",
            user=self.owner,
            reason="现场条件再次不满足，暂停作业（模拟）",
        )
        self.assertTrue(ok, message)
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-007")
        assert permit is not None
        self.assertEqual(permit["status"], permit_state.PERMIT_SUSPENDED)

        actions = self._actions("PERMIT-DEMO-007")
        self.assertIn("permit.resume", actions)
        self.assertGreaterEqual(actions.count("permit.suspend"), 2)

        paused = [
            event
            for event in audit.list_events(
                self.connection, entity_type="permit", entity_id="PERMIT-DEMO-007"
            )
            if event["action"] == "permit.suspend"
        ][-1]
        self.assertIn("暂停作业", str(paused["reason"]))

    def test_a_lapsed_window_blocks_resume(self) -> None:
        """A paused permit must not restart once its window has passed."""
        past = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
        self.connection.execute(
            "UPDATE permits SET valid_to = ? WHERE id = ?", (past, "PERMIT-DEMO-007")
        )
        self.connection.commit()
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-007")
        assert permit is not None
        self.assertTrue(common.permit_validity(permit)["is_expired"])

        ok, message = commands.execute(
            self.connection, "permit", "PERMIT-DEMO-007", "resume", user=self.owner
        )
        self.assertFalse(ok)
        self.assertIn("有效期", message)
        refreshed = permit_service.get_permit(self.connection, "PERMIT-DEMO-007")
        assert refreshed is not None
        self.assertEqual(refreshed["status"], permit_state.PERMIT_SUSPENDED)

    def test_resume_is_not_a_silent_close(self) -> None:
        self.assertIn("resume", permit_state.PERMIT_TRANSITIONS[permit_state.PERMIT_SUSPENDED])
        self.assertNotIn("close", permit_state.PERMIT_TRANSITIONS[permit_state.PERMIT_SUSPENDED])


# --------------------------------------------------------------------------- #
# 4 — 提交作业完成 / 现场交还 / 确认关闭
# --------------------------------------------------------------------------- #


class HandbackCloseoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = db.open_database(":memory:")
        self.addCleanup(self.connection.close)
        demo_seed.seed_demo_data(self.connection)
        self.owner = _user(self.connection, demo_seed.OWNER)
        self.reviewer = _user(self.connection, demo_seed.EHS_REVIEWER)

    def test_complete_work_requires_a_handback_note(self) -> None:
        ok, message = commands.execute(
            self.connection, "permit", "PERMIT-DEMO-006", "complete_work", user=self.owner
        )
        self.assertFalse(ok)
        self.assertTrue(message)

    def test_handback_is_recorded_but_does_not_close_the_permit(self) -> None:
        ok, message = commands.execute(
            self.connection,
            "permit",
            "PERMIT-DEMO-006",
            "complete_work",
            user=self.owner,
            reason="作业完成，现场设备已复位并交接（模拟）",
        )
        self.assertTrue(ok, message)
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-006")
        assert permit is not None
        self.assertEqual(permit["status"], permit_state.PERMIT_CLOSEOUT_REVIEW)
        self.assertIn("交接", str(permit["handback_note"]))
        # 作业完成 ≠ 许可关闭
        self.assertNotEqual(permit["status"], permit_state.PERMIT_CLOSED)

    def test_an_open_hazard_blocks_the_close(self) -> None:
        commands.execute(
            self.connection,
            "permit",
            "PERMIT-DEMO-006",
            "complete_work",
            user=self.owner,
            reason="作业完成，现场已交接（模拟）",
        )
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-006")
        assert permit is not None
        self.assertGreater(permit["open_hazard_count"], 0)

        ok, message = commands.execute(
            self.connection, "permit", "PERMIT-DEMO-006", "close", user=self.reviewer,
            reason="申请关闭",
        )
        self.assertFalse(ok)
        self.assertIn("未关闭", message)
        refreshed = permit_service.get_permit(self.connection, "PERMIT-DEMO-006")
        assert refreshed is not None
        self.assertEqual(refreshed["status"], permit_state.PERMIT_CLOSEOUT_REVIEW)

    def test_the_closeout_gate_names_four_checks(self) -> None:
        from ui import permits as permits_page

        commands.execute(
            self.connection,
            "permit",
            "PERMIT-DEMO-006",
            "complete_work",
            user=self.owner,
            reason="作业完成，现场已交接（模拟）",
        )
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-006")
        assert permit is not None
        gate = permits_page._closeout_gate(self.connection, permit, self.reviewer)
        self.assertEqual(len(gate), 4)
        labels = {label: ok for label, ok, _ in gate}
        self.assertTrue(labels["作业已结束（已提交作业完成）"])
        self.assertTrue(labels["现场交还信息已填写"])
        self.assertFalse(labels["关联未关闭隐患 = 0"])
        self.assertTrue(labels["当前身份具备关闭权限"])

    def test_close_is_allowed_once_no_hazard_is_open(self) -> None:
        """Close the linked hazard first, then the permit closes with a note."""
        # Drive DEMO-006 to closeout, then finish its linked hazard.
        commands.execute(
            self.connection, "permit", "PERMIT-DEMO-006", "complete_work",
            user=self.owner, reason="作业完成并交还现场（模拟）",
        )
        hazard = hazard_service.get_hazard(self.connection, "HZ-DEMO-002")
        assert hazard is not None
        self.assertEqual(hazard["status"], "verification_pending")
        ok, message = commands.execute(
            self.connection, "hazard", "HZ-DEMO-002", "verify_pass",
            user=self.reviewer, reason="现场复核通过（模拟）",
        )
        self.assertTrue(ok, message)
        ok, message = commands.execute(
            self.connection, "permit", "PERMIT-DEMO-006", "close",
            user=self.reviewer, reason="关联隐患已关闭，许可闭环（模拟）",
        )
        self.assertTrue(ok, message)
        permit = permit_service.get_permit(self.connection, "PERMIT-DEMO-006")
        assert permit is not None
        self.assertEqual(permit["status"], permit_state.PERMIT_CLOSED)
        self.assertEqual(str(permit["closed_by"]), demo_seed.EHS_REVIEWER)
        self.assertTrue(str(permit["closure_note"]))


# --------------------------------------------------------------------------- #
# 6 + 13 — 黄金 Demo 每一步 Action Queue 变化
# --------------------------------------------------------------------------- #


class GoldenActionQueueTests(unittest.TestCase):
    """The golden story, observed through each persona's derived queue."""

    GOLDEN = demo_seed.GOLDEN_PERMIT_ID

    def setUp(self) -> None:
        self.connection = db.open_database(":memory:")
        self.addCleanup(self.connection.close)
        demo_seed.seed_demo_data(self.connection)
        self.applicant = _user(self.connection, demo_seed.APPLICANT)
        self.reviewer = _user(self.connection, demo_seed.EHS_REVIEWER)
        self.approver = _user(self.connection, demo_seed.APPROVER)
        self.owner = _user(self.connection, demo_seed.OWNER)

    def _run(self, code: str, user: dict, **kwargs) -> None:
        ok, message = commands.execute(
            self.connection, "permit", self.GOLDEN, code, user=user, **kwargs
        )
        self.assertTrue(ok, f"{code} 失败：{message}")

    def test_the_golden_queue_changes_at_every_step(self) -> None:
        # Step 1 — draft: only the applicant has work (submit).
        self.assertIn((self.GOLDEN, "submit"), _queue(self.connection, demo_seed.APPLICANT))
        self.assertNotIn(self.GOLDEN, {e for e, _ in _queue(self.connection, demo_seed.EHS_REVIEWER)})

        self._run("submit", self.applicant)
        # Step 2 — EHS review: the reviewer owns the next action.
        self.assertIn(
            (self.GOLDEN, action_queue.ACTION_EHS_REVIEW),
            _queue(self.connection, demo_seed.EHS_REVIEWER),
        )
        self.assertNotIn(self.GOLDEN, {e for e, _ in _queue(self.connection, demo_seed.APPLICANT)})

        self._run("confirm", self.reviewer, reason="SDS 与 JSA 已核对")
        # Step 3 — approval: the approver owns the next action.
        self.assertIn(
            (self.GOLDEN, action_queue.ACTION_APPROVE),
            _queue(self.connection, demo_seed.APPROVER),
        )

        self._run("approve", self.approver, reason="同意作业")
        # Step 4 — pre-start: the owner owns the next action.
        self.assertIn(
            (self.GOLDEN, action_queue.ACTION_PRESTART_CONFIRM),
            _queue(self.connection, demo_seed.OWNER),
        )

        self._run("prestart_confirm", self.owner, checks=PRESTART_CHECKS)
        # Step 5 — executing: no permit action remains.
        self.assertNotIn(
            (self.GOLDEN, action_queue.ACTION_PRESTART_CONFIRM),
            _queue(self.connection, demo_seed.OWNER),
        )

        self._run("suspend", self.owner, reason="现场条件不满足，暂停作业（模拟）")
        # Step 5b — suspended: resume is the owner's next action.
        self.assertIn(
            (self.GOLDEN, action_queue.ACTION_RESUME),
            _queue(self.connection, demo_seed.OWNER),
        )

        self._run("resume", self.owner)
        self.assertNotIn(
            (self.GOLDEN, action_queue.ACTION_RESUME),
            _queue(self.connection, demo_seed.OWNER),
        )

        # Step 6 — the owner reports the linked hazard, then submits the close.
        self.assertIn(
            ("HZ-DEMO-005", action_queue.ACTION_START_RECTIFICATION),
            _queue(self.connection, demo_seed.OWNER),
        )
        ok, message = commands.execute(
            self.connection, "hazard", "HZ-DEMO-005", "start", user=self.owner
        )
        self.assertTrue(ok, message)
        self.assertIn(
            ("HZ-DEMO-005", action_queue.ACTION_SUBMIT_RECTIFICATION),
            _queue(self.connection, demo_seed.OWNER),
        )
        ok, message = commands.execute(
            self.connection,
            "hazard",
            "HZ-DEMO-005",
            "submit_rectification",
            user=self.owner,
            reason="已补齐应急冲洗设施点检记录（模拟）",
            evidence=[{"file_name": "（占位）点检记录（模拟）.xlsx", "evidence_type": "记录"}],
        )
        self.assertTrue(ok, message)
        # Step 7 — verification belongs to the EHS reviewer.
        self.assertIn(
            ("HZ-DEMO-005", action_queue.ACTION_VERIFY),
            _queue(self.connection, demo_seed.EHS_REVIEWER),
        )
        ok, message = commands.execute(
            self.connection, "hazard", "HZ-DEMO-005", "verify_pass",
            user=self.reviewer, reason="现场复核通过（模拟）",
        )
        self.assertTrue(ok, message)
        self.assertNotIn(
            ("HZ-DEMO-005", action_queue.ACTION_VERIFY),
            _queue(self.connection, demo_seed.EHS_REVIEWER),
        )

        self._run("complete_work", self.owner, reason="作业完成并交还现场（模拟）")
        # Step 8 — closeout: the reviewer owns the close.
        self.assertIn(
            (self.GOLDEN, action_queue.ACTION_CLOSE),
            _queue(self.connection, demo_seed.EHS_REVIEWER),
        )

        self._run("close", self.reviewer, reason="关联隐患已关闭，许可闭环")
        # Step 9 — closed: no action remains for anyone.
        for persona in (
            demo_seed.APPLICANT,
            demo_seed.EHS_REVIEWER,
            demo_seed.APPROVER,
            demo_seed.OWNER,
        ):
            with self.subTest(persona=persona):
                self.assertNotIn(self.GOLDEN, {e for e, _ in _queue(self.connection, persona)})

        # The whole story is a single audited chain on one permit.
        actions = [
            str(event["action"])
            for event in audit.list_events(
                self.connection, entity_type="permit", entity_id=self.GOLDEN
            )
        ]
        for expected in (
            "permit.submit",
            "permit.confirm",
            "permit.approve",
            "permit.prestart_confirm",
            "permit.suspend",
            "permit.resume",
            "permit.complete_work",
            "permit.close",
        ):
            with self.subTest(action=expected):
                self.assertIn(expected, actions)


# --------------------------------------------------------------------------- #
# 13 — 流程进度与 status 一致
# --------------------------------------------------------------------------- #


class FlowStatusConsistencyTests(unittest.TestCase):
    def test_every_status_maps_to_one_consistent_stage(self) -> None:
        for status in permit_state.PERMIT_STATUSES:
            with self.subTest(status=status):
                flow = common.permit_flow({"status": status})
                self.assertEqual(flow["total"], 6)
                index = flow["index"]
                self.assertEqual(
                    common.permit_stage_label(status),
                    flow["stages"][index]["label"],
                )
                self.assertIn(common.permit_stage_label(status), FLOW_STAGES)

    def test_the_terminated_statuses_stop_on_the_last_stage(self) -> None:
        for status in (permit_state.PERMIT_CANCELLED, permit_state.PERMIT_EXPIRED):
            with self.subTest(status=status):
                flow = common.permit_flow({"status": status})
                self.assertTrue(flow["terminated"])
                self.assertEqual(flow["stages"][5]["state"], common.STAGE_STOPPED)
                self.assertIn("终止", flow["position_text"])

    def test_a_closed_permit_has_every_stage_done(self) -> None:
        flow = common.permit_flow({"status": permit_state.PERMIT_CLOSED})
        self.assertTrue(flow["closed"])
        self.assertTrue(
            all(stage["state"] == common.STAGE_DONE for stage in flow["stages"])
        )

    def test_a_suspended_permit_stays_on_the_execution_stage(self) -> None:
        flow = common.permit_flow({"status": permit_state.PERMIT_SUSPENDED})
        self.assertEqual(flow["index"], FLOW_STAGES.index("执行"))
        self.assertFalse(flow["terminated"])


# --------------------------------------------------------------------------- #
# 7 — 下一角色提示
# --------------------------------------------------------------------------- #


class NextRoleHintTests(unittest.TestCase):
    EXPECTED = {
        permit_state.PERMIT_DRAFT: "作业申请人",
        permit_state.PERMIT_RETURNED: "作业申请人",
        permit_state.PERMIT_EHS_REVIEW: "EHS审核人",
        permit_state.PERMIT_APPROVAL_PENDING: "作业审批人",
        permit_state.PERMIT_APPROVED: "作业负责人",
        permit_state.PERMIT_ACTIVE: "作业负责人",
        permit_state.PERMIT_SUSPENDED: "作业负责人",
        permit_state.PERMIT_CLOSEOUT_REVIEW: "EHS审核人",
    }

    def test_every_moving_status_has_the_right_next_role(self) -> None:
        for status, role in self.EXPECTED.items():
            with self.subTest(status=status):
                label, todo = product.next_role_hint({"status": status})
                self.assertEqual(label, role)
                self.assertTrue(todo)

    def test_terminal_statuses_have_no_next_role(self) -> None:
        for status in (
            permit_state.PERMIT_CLOSED,
            permit_state.PERMIT_CANCELLED,
            permit_state.PERMIT_EXPIRED,
        ):
            with self.subTest(status=status):
                label, todo = product.next_role_hint({"status": status})
                self.assertEqual(label, "")
                self.assertTrue(todo)

    def test_every_hint_role_maps_to_a_demo_persona(self) -> None:
        labels = dict(common.PERSONA_CHOICES)
        for role, persona_id in product.PERSONA_FOR_ROLE.items():
            with self.subTest(role=role):
                self.assertTrue(persona_id.startswith("DEMO-"))
                self.assertIn(persona_id, labels)
                # The persona's business label names the role it plays.
                self.assertIn(role, labels[persona_id])


# --------------------------------------------------------------------------- #
# 5 — 业务词映射（页面不打印技术状态词）
# --------------------------------------------------------------------------- #


class BusinessVocabularyTests(unittest.TestCase):
    def test_every_status_maps_to_a_business_word(self) -> None:
        for status in permit_state.PERMIT_STATUSES:
            with self.subTest(status=status):
                label = common.business_state_label("permit", status)
                self.assertNotEqual(label, status)
                self.assertTrue(label)
        for status in ("open", "assigned", "in_progress", "verification_pending", "closed"):
            with self.subTest(hazard_status=status):
                self.assertNotEqual(common.business_state_label("hazard", status), status)

    def test_an_empty_state_reads_as_a_dash(self) -> None:
        self.assertEqual(common.business_state_label("permit", ""), "—")


# --------------------------------------------------------------------------- #
# 2 + 3 + 4 + 5 + 7 + 9 + 10 — the rendered pages
# --------------------------------------------------------------------------- #


class P0C3UiTests(unittest.TestCase):
    """The real Streamlit app, driven through ``AppTest``."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._original = db.EHS_DB_PATH
        db.EHS_DB_PATH = str(Path(self._temp.name) / "p0c3.db")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        db.EHS_DB_PATH = self._original
        self._temp.cleanup()

    def _app(self) -> AppTest:
        app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=300).run()
        if app.exception:
            self.fail(f"页面抛出异常：{[item.value for item in app.exception]}")
        return app

    def _store(self):
        """Open the same file store the app uses (for arranging a fixture)."""
        return db.open_database(db.EHS_DB_PATH)

    def _open(self, app: AppTest, page: str, entity_type: str, entity_id: str) -> AppTest:
        app.session_state[common.NAV_KEY] = page
        app.session_state[common.DETAIL_KEY] = {
            "type": entity_type,
            "id": entity_id,
            "page": page,
        }
        result = app.run(timeout=300)
        self.assertEqual([item.value for item in result.exception], [])
        return result

    # --- 9 — the list ---------------------------------------------------- #

    def test_the_permit_list_shows_type_validity_and_next_action(self) -> None:
        app = self._app()
        _nav(app, common.PAGE_PERMITS)
        frame = app.dataframe[0].value
        for column in ("编号", "名称", "作业类型", "区域", "风险", "状态", "负责人", "有效期", "有效期状态", "是否逾期"):
            with self.subTest(column=column):
                self.assertIn(column, list(frame.columns))
        types = {str(value) for value in frame["作业类型"]}
        self.assertIn("其他高风险作业", types)
        validity_labels = {str(value) for value in frame["有效期状态"]}
        self.assertTrue(validity_labels & {"有效", "即将到期", "已过期"})

    # --- 2 — validity on the detail -------------------------------------- #

    def test_the_expired_permit_detail_blocks_starting_work(self) -> None:
        app = self._app()
        app = self._open(app, common.PAGE_PERMITS, "permit", "PERMIT-DEMO-004")
        text = _page_text(app)
        self.assertIn("责任角色", text)
        self.assertIn("许可有效期", text)
        self.assertIn("已过期", text)
        self.assertIn("不能开工", text)
        # The start of work must not be offered as a live action.
        prestart = _button(app, "提交开工检查")
        if prestart is not None:
            self.assertTrue(bool(getattr(prestart, "disabled", False)))

    # --- 3 — suspension panel ------------------------------------------- #

    def test_the_suspended_permit_shows_a_pause_panel_and_is_not_closed(self) -> None:
        app = self._app()
        app = self._open(app, common.PAGE_PERMITS, "permit", "PERMIT-DEMO-007")
        text = _page_text(app)
        self.assertIn("作业已暂停", text)
        self.assertIn("并没有关闭", text)
        self.assertIn("暂停人", text)
        self.assertIn("暂停时间", text)
        self.assertIn("暂停原因", text)

    # --- 4 — closeout gate ---------------------------------------------- #

    def test_the_closeout_detail_shows_the_four_checks(self) -> None:
        connection = self._store()
        try:
            demo_seed.seed_demo_data(connection)
            owner = _user(connection, demo_seed.OWNER)
            ok, message = commands.execute(
                connection, "permit", "PERMIT-DEMO-006", "complete_work",
                user=owner, reason="作业完成，现场设备已复位并交接（模拟）",
            )
            self.assertTrue(ok, message)
        finally:
            connection.close()

        app = self._app()
        app = self._open(app, common.PAGE_PERMITS, "permit", "PERMIT-DEMO-006")
        text = _page_text(app)
        self.assertIn("现场交还与确认关闭", text)
        self.assertIn("关闭前检查", text)
        self.assertIn("作业已结束（已提交作业完成）", text)
        self.assertIn("现场交还信息已填写", text)
        self.assertIn("关联未关闭隐患 = 0", text)
        self.assertIn("未关闭的隐患会阻止许可关闭", text)

    # --- 5 — responsible roles on the first screen ---------------------- #

    def test_the_permit_first_screen_shows_the_four_roles(self) -> None:
        app = self._app()
        app = self._open(app, common.PAGE_PERMITS, "permit", demo_seed.GOLDEN_PERMIT_ID)
        text = _page_text(app)
        self.assertIn("责任角色", text)
        for role in ("作业申请人", "作业负责人", "EHS审核人", "作业审批人"):
            with self.subTest(role=role):
                self.assertIn(role, text)

    def test_the_hazard_first_screen_shows_reporter_rectifier_verifier(self) -> None:
        app = self._app()
        app = self._open(app, common.PAGE_HAZARDS, "hazard", "HZ-DEMO-002")
        text = _page_text(app)
        self.assertIn("责任角色", text)
        for role in ("发现人", "整改负责人", "EHS验证人"):
            with self.subTest(role=role):
                self.assertIn(role, text)

    def test_the_hazard_verify_action_collects_a_note_before_closing(self) -> None:
        """验证通过 must open the 验证说明 form — a bare click can never pass.

        The state machine refuses a pass without a note, so the UI must collect
        one; otherwise the button is dead and the golden Demo cannot close.
        """
        app = self._app()
        app = self._open(app, common.PAGE_HAZARDS, "hazard", "HZ-DEMO-002")
        button = _button(app, "验证通过")
        self.assertIsNotNone(button)
        button.click().run(timeout=300)
        self.assertEqual([item.value for item in app.exception], [])

        labels = [str(item.label) for item in app.text_area]
        self.assertIn("验证说明", labels)

        connection = self._store()
        try:
            hazard = hazard_service.get_hazard(connection, "HZ-DEMO-002")
            assert hazard is not None
            # Not closed yet — the note is mandatory.
            self.assertEqual(hazard["status"], "verification_pending")
        finally:
            connection.close()

        app.text_area(key="hreason_HZ-DEMO-002_verify_pass").set_value("现场复核通过（模拟）")
        confirm = next(item for item in app.button if item.label == "确认执行")
        confirm.click().run(timeout=300)
        self.assertEqual([item.value for item in app.exception], [])

        connection = self._store()
        try:
            hazard = hazard_service.get_hazard(connection, "HZ-DEMO-002")
            assert hazard is not None
            self.assertEqual(hazard["status"], "closed")
        finally:
            connection.close()

    # --- 7 — next-role hint on the golden detail ------------------------ #

    def test_the_golden_detail_suggests_the_next_role_without_switching(self) -> None:
        app = self._app()
        app = self._open(app, common.PAGE_PERMITS, "permit", demo_seed.GOLDEN_PERMIT_ID)
        text = _page_text(app)
        self.assertIn("下一步建议切换为：**作业申请人**", text)
        # A hint is offered, but the identity has NOT changed by itself.
        self.assertEqual(
            app.selectbox(key=common.USER_KEY).value, common.DEFAULT_USER_ID
        )

    # --- 10 — the risk dashboard answers six questions ------------------ #

    def test_the_risk_dashboard_answers_six_management_questions(self) -> None:
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
        text = _page_text(app)
        for heading in (
            "即将到期 / 已过期的作业许可",
            "高/重大风险未关闭的作业许可",
            "已逾期的隐患",
            "等待 EHS 验证的隐患",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, text)
        self.assertIn("时间维度：许可有效期，与风险等级无关", text)

    # --- 5 — no raw technical status leaks onto a business page --------- #

    def test_no_page_prints_a_raw_status_identifier(self) -> None:
        app = self._app()
        for page in common.NAV_PAGES:
            with self.subTest(page=page):
                _nav(app, page)
                text = _page_text(app)
                for token in ("closeout_review", "verification_pending", "approval_pending", "ehs_review"):
                    self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

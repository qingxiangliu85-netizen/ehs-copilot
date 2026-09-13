"""P0A tests for the pure V5 permit state machine."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from jobs import JOB_STATUSES
from workflow import permit_state, roles


NOW = datetime(2026, 9, 13, 10, 0, 0)


def ready_context(**overrides: object) -> dict[str, object]:
    context: dict[str, object] = {
        "chemical_count": 1,
        "sds_evidence_count": 1,
        "public_evidence_count": 3,
        "jsa_item_count": 1,
        "residual_risk_level": "中",
        "control_measures": "",
        "prestart_passed": False,
        "open_hazard_count": 0,
        "valid_to": (NOW + timedelta(days=3)).isoformat(timespec="seconds"),
    }
    context.update(overrides)
    return context


def check(
    from_status: str,
    action: str,
    *,
    actor_role: str = "",
    reason: str = "",
    context: dict[str, object] | None = None,
):
    return permit_state.validate_permit_transition(
        from_status,
        action,
        actor_role=actor_role,
        reason=reason,
        context=context if context is not None else ready_context(),
        now=NOW,
    )


class PermitLifecycleTests(unittest.TestCase):
    def test_full_legal_lifecycle(self) -> None:
        sequence = (
            ("draft", "submit", roles.ROLE_APPLICANT, "", ready_context()),
            ("ehs_review", "confirm", roles.ROLE_EHS_REVIEWER, "", ready_context()),
            (
                "approval_pending",
                "approve",
                roles.ROLE_APPROVER,
                "",
                ready_context(),
            ),
            (
                "approved",
                "prestart_confirm",
                roles.ROLE_ACTION_OWNER,
                "",
                ready_context(prestart_passed=True),
            ),
            (
                "active",
                "complete_work",
                roles.ROLE_ACTION_OWNER,
                "现场已交接",
                ready_context(prestart_passed=True),
            ),
            (
                "closeout_review",
                "close",
                roles.ROLE_EHS_REVIEWER,
                "",
                ready_context(prestart_passed=True),
            ),
        )
        for from_status, action, role, reason, context in sequence:
            with self.subTest(action=action):
                result = check(
                    from_status, action, actor_role=role, reason=reason, context=context
                )
                self.assertTrue(result.allowed, result.message)
                self.assertEqual(result.code, permit_state.CODE_OK)
        self.assertEqual(
            permit_state.validate_permit_transition(
                "closeout_review",
                "close",
                actor_role=roles.ROLE_EHS_REVIEWER,
                context=ready_context(),
                now=NOW,
            ).to_status,
            "closed",
        )

    def test_suspend_and_resume_cycle(self) -> None:
        suspend = check(
            "active",
            "suspend",
            actor_role=roles.ROLE_ACTION_OWNER,
            reason="现场风速超标",
        )
        self.assertTrue(suspend.allowed)
        self.assertEqual(suspend.to_status, "suspended")
        resume = check("suspended", "resume", actor_role=roles.ROLE_ACTION_OWNER)
        self.assertTrue(resume.allowed)
        self.assertEqual(resume.to_status, "active")

    def test_illegal_transitions_are_rejected(self) -> None:
        cases = (
            ("draft", "approve"),
            ("draft", "prestart_confirm"),
            ("draft", "close"),
            ("ehs_review", "approve"),
            ("approval_pending", "prestart_confirm"),
            ("approved", "complete_work"),
            ("approved", "close"),
            ("active", "close"),
            ("closeout_review", "suspend"),
            ("closed", "submit"),
            ("cancelled", "submit"),
            ("expired", "submit"),
        )
        for from_status, action in cases:
            with self.subTest(from_status=from_status, action=action):
                result = check(
                    from_status, action, actor_role=roles.ROLE_DEMO_ADMIN
                )
                self.assertFalse(result.allowed)
                self.assertEqual(
                    result.code, permit_state.CODE_ILLEGAL_TRANSITION
                )

    def test_terminal_statuses_have_no_outgoing_transitions(self) -> None:
        for status in permit_state.PERMIT_TERMINAL_STATUSES:
            self.assertEqual(permit_state.PERMIT_TRANSITIONS[status], {})

    def test_unknown_status_and_action(self) -> None:
        unknown = check("not_a_status", "submit", actor_role=roles.ROLE_APPLICANT)
        self.assertEqual(unknown.code, permit_state.CODE_UNKNOWN_STATUS)
        unknown_action = check(
            "draft", "teleport", actor_role=roles.ROLE_APPLICANT
        )
        self.assertEqual(unknown_action.code, permit_state.CODE_ILLEGAL_TRANSITION)


class PermitGateTests(unittest.TestCase):
    def test_submit_requires_chemical_sds_and_jsa(self) -> None:
        missing_chemical = check(
            "draft",
            "submit",
            actor_role=roles.ROLE_APPLICANT,
            context=ready_context(chemical_count=0),
        )
        self.assertEqual(
            missing_chemical.code, permit_state.CODE_PRECONDITION_FAILED
        )
        missing_sds = check(
            "draft",
            "submit",
            actor_role=roles.ROLE_APPLICANT,
            context=ready_context(sds_evidence_count=0),
        )
        self.assertIn("SDS", missing_sds.message)
        missing_jsa = check(
            "draft",
            "submit",
            actor_role=roles.ROLE_APPLICANT,
            context=ready_context(jsa_item_count=0),
        )
        self.assertEqual(missing_jsa.code, permit_state.CODE_PRECONDITION_FAILED)

    def test_public_evidence_does_not_satisfy_the_sds_gate(self) -> None:
        result = check(
            "draft",
            "submit",
            actor_role=roles.ROLE_APPLICANT,
            context=ready_context(sds_evidence_count=0, public_evidence_count=19),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, permit_state.CODE_PRECONDITION_FAILED)
        self.assertIn("公开来源", result.message)

    def test_confirm_requires_residual_risk_and_controls_for_high_risk(self) -> None:
        missing_level = check(
            "ehs_review",
            "confirm",
            actor_role=roles.ROLE_EHS_REVIEWER,
            context=ready_context(residual_risk_level=""),
        )
        self.assertEqual(
            missing_level.code, permit_state.CODE_PRECONDITION_FAILED
        )
        high_without_controls = check(
            "ehs_review",
            "confirm",
            actor_role=roles.ROLE_EHS_REVIEWER,
            context=ready_context(residual_risk_level="重大", control_measures=""),
        )
        self.assertIn("控制措施", high_without_controls.message)
        high_with_controls = check(
            "ehs_review",
            "confirm",
            actor_role=roles.ROLE_EHS_REVIEWER,
            context=ready_context(
                residual_risk_level="重大", control_measures="密闭加料与双人复核"
            ),
        )
        self.assertTrue(high_with_controls.allowed)

    def test_approve_requires_approver_role(self) -> None:
        denied = check(
            "approval_pending", "approve", actor_role=roles.ROLE_APPLICANT
        )
        self.assertEqual(denied.code, permit_state.CODE_PERMISSION_DENIED)
        allowed = check(
            "approval_pending", "approve", actor_role=roles.ROLE_APPROVER
        )
        self.assertTrue(allowed.allowed)

    def test_reject_and_return_require_reason(self) -> None:
        reject = check("approval_pending", "reject", actor_role=roles.ROLE_APPROVER)
        self.assertEqual(reject.code, permit_state.CODE_REASON_REQUIRED)
        reject_with_reason = check(
            "approval_pending",
            "reject",
            actor_role=roles.ROLE_APPROVER,
            reason="SDS 证据与现场化学品不符",
        )
        self.assertTrue(reject_with_reason.allowed)
        self.assertEqual(reject_with_reason.to_status, "returned")
        send_back = check("ehs_review", "return_draft", actor_role=roles.ROLE_EHS_REVIEWER)
        self.assertEqual(send_back.code, permit_state.CODE_REASON_REQUIRED)

    def test_returned_can_resubmit(self) -> None:
        result = check("returned", "resubmit", actor_role=roles.ROLE_APPLICANT)
        self.assertTrue(result.allowed)
        self.assertEqual(result.to_status, "ehs_review")

    def test_prestart_requires_passed_checks_inside_validity(self) -> None:
        failed_checks = check(
            "approved",
            "prestart_confirm",
            actor_role=roles.ROLE_ACTION_OWNER,
            context=ready_context(prestart_passed=False),
        )
        self.assertIn("开工前检查", failed_checks.message)
        expired = check(
            "approved",
            "prestart_confirm",
            actor_role=roles.ROLE_ACTION_OWNER,
            context=ready_context(
                prestart_passed=True,
                valid_to=(NOW - timedelta(hours=1)).isoformat(timespec="seconds"),
            ),
        )
        self.assertIn("有效期", expired.message)
        missing_validity = check(
            "approved",
            "prestart_confirm",
            actor_role=roles.ROLE_ACTION_OWNER,
            context=ready_context(prestart_passed=True, valid_to=""),
        )
        self.assertIn("valid_to", missing_validity.message)

    def test_close_requires_no_open_hazards(self) -> None:
        blocked = check(
            "closeout_review",
            "close",
            actor_role=roles.ROLE_EHS_REVIEWER,
            context=ready_context(open_hazard_count=2),
        )
        self.assertFalse(blocked.allowed)
        self.assertIn("2", blocked.message)
        clear = check(
            "closeout_review",
            "close",
            actor_role=roles.ROLE_EHS_REVIEWER,
            context=ready_context(open_hazard_count=0),
        )
        self.assertTrue(clear.allowed)

    def test_expire_only_after_valid_to(self) -> None:
        too_early = check(
            "active",
            "expire",
            actor_role=roles.ROLE_SYSTEM,
            context=ready_context(valid_to=(NOW + timedelta(days=1)).isoformat()),
        )
        self.assertEqual(too_early.code, permit_state.CODE_PRECONDITION_FAILED)
        expired = check(
            "active",
            "expire",
            actor_role=roles.ROLE_SYSTEM,
            context=ready_context(valid_to=(NOW - timedelta(days=1)).isoformat()),
        )
        self.assertTrue(expired.allowed)
        self.assertEqual(expired.to_status, "expired")

    def test_cancel_requires_reason_and_is_terminal(self) -> None:
        denied = check("draft", "cancel", actor_role=roles.ROLE_APPLICANT)
        self.assertEqual(denied.code, permit_state.CODE_REASON_REQUIRED)
        cancelled = check(
            "draft",
            "cancel",
            actor_role=roles.ROLE_APPLICANT,
            reason="计划取消",
        )
        self.assertTrue(cancelled.allowed)
        self.assertEqual(cancelled.to_status, "cancelled")
        self.assertEqual(permit_state.PERMIT_TRANSITIONS["cancelled"], {})

    def test_ai_role_has_no_permit_permissions(self) -> None:
        result = check("draft", "submit", actor_role="ai_agent")
        self.assertEqual(result.code, permit_state.CODE_PERMISSION_DENIED)


class PermitMappingTests(unittest.TestCase):
    def test_v4_status_mapping_covers_every_job_status(self) -> None:
        for status in JOB_STATUSES:
            self.assertIn(status, permit_state.V4_JOB_STATUS_TO_V5)
            self.assertTrue(permit_state.V4_JOB_STATUS_TO_V5[status])

    def test_status_mapping_round_trip_for_shared_states(self) -> None:
        for legacy in (
            "草稿",
            "待EHS确认",
            "待审批",
            "已批准",
            "执行中",
            "待复查",
            "已关闭",
            "已驳回",
        ):
            canonical = permit_state.V4_JOB_STATUS_TO_V5[legacy]
            self.assertEqual(
                permit_state.V5_TO_V4_JOB_STATUS[canonical], legacy
            )

    def test_highest_risk_level(self) -> None:
        self.assertEqual(permit_state.highest_risk_level(["低", "中"]), "中")
        self.assertEqual(
            permit_state.highest_risk_level(["中", "重大", "高"]), "重大"
        )
        self.assertEqual(permit_state.highest_risk_level([]), "")


if __name__ == "__main__":
    unittest.main()

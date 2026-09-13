"""P0A tests for the pure V5 hazard state machine."""

from __future__ import annotations

import unittest

from workflow import hazard_state, roles


def check(
    from_status: str,
    action: str,
    *,
    actor_role: str = "",
    actor_id: str = "",
    owner_id: str = "",
    reason: str = "",
    context: dict[str, object] | None = None,
):
    return hazard_state.validate_hazard_transition(
        from_status,
        action,
        actor_role=actor_role,
        actor_id=actor_id,
        owner_id=owner_id,
        reason=reason,
        context=context or {},
    )


class HazardLifecycleTests(unittest.TestCase):
    def test_normal_rectification_closure(self) -> None:
        assigned = check(
            "open",
            "assign",
            actor_role=roles.ROLE_EHS_REVIEWER,
            context={"target_owner_id": "整改人甲"},
        )
        self.assertTrue(assigned.allowed)
        self.assertEqual(assigned.to_status, "assigned")

        started = check(
            "assigned",
            "start",
            actor_role=roles.ROLE_ACTION_OWNER,
            actor_id="整改人甲",
            owner_id="整改人甲",
        )
        self.assertTrue(started.allowed)
        self.assertEqual(started.to_status, "in_progress")

        submitted = check(
            "in_progress",
            "submit_rectification",
            actor_role=roles.ROLE_ACTION_OWNER,
            actor_id="整改人甲",
            owner_id="整改人甲",
            context={"corrective_action_count": 1, "evidence_count": 1},
        )
        self.assertTrue(submitted.allowed)
        self.assertEqual(submitted.to_status, "verification_pending")

        verified = check(
            "verification_pending",
            "verify_pass",
            actor_role=roles.ROLE_EHS_REVIEWER,
            actor_id="EHS审核人",
            owner_id="整改人甲",
            context={"verification_notes": "现场复核通过"},
        )
        self.assertTrue(verified.allowed)
        self.assertEqual(verified.to_status, "closed")

    def test_verification_failure_reopens_and_can_be_reassigned(self) -> None:
        failed = check(
            "verification_pending",
            "verify_fail",
            actor_role=roles.ROLE_EHS_REVIEWER,
            actor_id="EHS审核人",
            owner_id="整改人甲",
            reason="证据照片无法辨识",
            context={"verification_notes": "证据照片无法辨识"},
        )
        self.assertTrue(failed.allowed)
        self.assertEqual(failed.to_status, "reopened")

        reassigned = check(
            "reopened",
            "assign",
            actor_role=roles.ROLE_EHS_REVIEWER,
            context={"target_owner_id": "整改人乙"},
        )
        self.assertTrue(reassigned.allowed)
        self.assertEqual(reassigned.to_status, "assigned")

    def test_closed_can_be_reopened_only_with_reason(self) -> None:
        without_reason = check(
            "closed",
            "reopen",
            actor_role=roles.ROLE_EHS_REVIEWER,
            actor_id="EHS审核人",
        )
        self.assertEqual(
            without_reason.code, hazard_state.CODE_REASON_REQUIRED
        )
        reopened = check(
            "closed",
            "reopen",
            actor_role=roles.ROLE_EHS_REVIEWER,
            actor_id="EHS审核人",
            reason="现场复查发现措施失效",
        )
        self.assertTrue(reopened.allowed)
        self.assertEqual(reopened.to_status, "reopened")

    def test_owner_cannot_verify_own_rectification(self) -> None:
        result = check(
            "verification_pending",
            "verify_pass",
            actor_role=roles.ROLE_EHS_REVIEWER,
            actor_id="整改人甲",
            owner_id="整改人甲",
            context={"verification_notes": "自行验证"},
        )
        self.assertFalse(result.allowed)
        self.assertEqual(
            result.code, hazard_state.CODE_SEPARATION_REQUIRED
        )

    def test_start_and_submit_are_owner_only(self) -> None:
        stranger = check(
            "assigned",
            "start",
            actor_role=roles.ROLE_ACTION_OWNER,
            actor_id="整改人乙",
            owner_id="整改人甲",
        )
        self.assertEqual(stranger.code, hazard_state.CODE_OWNER_MISMATCH)

        unassigned = check(
            "assigned",
            "start",
            actor_role=roles.ROLE_ACTION_OWNER,
            actor_id="整改人甲",
            owner_id="",
        )
        self.assertEqual(unassigned.code, hazard_state.CODE_MISSING_OWNER)

    def test_assign_requires_target_owner(self) -> None:
        result = check(
            "open",
            "assign",
            actor_role=roles.ROLE_EHS_REVIEWER,
            context={"target_owner_id": ""},
        )
        self.assertEqual(result.code, hazard_state.CODE_MISSING_OWNER)

    def test_submit_requires_action_and_evidence(self) -> None:
        no_action = check(
            "in_progress",
            "submit_rectification",
            actor_role=roles.ROLE_ACTION_OWNER,
            actor_id="整改人甲",
            owner_id="整改人甲",
            context={"corrective_action_count": 0, "evidence_count": 1},
        )
        self.assertEqual(no_action.code, hazard_state.CODE_EVIDENCE_REQUIRED)
        no_evidence = check(
            "in_progress",
            "submit_rectification",
            actor_role=roles.ROLE_ACTION_OWNER,
            actor_id="整改人甲",
            owner_id="整改人甲",
            context={"corrective_action_count": 1, "evidence_count": 0},
        )
        self.assertEqual(no_evidence.code, hazard_state.CODE_EVIDENCE_REQUIRED)

    def test_verify_pass_requires_notes(self) -> None:
        result = check(
            "verification_pending",
            "verify_pass",
            actor_role=roles.ROLE_EHS_REVIEWER,
            actor_id="EHS审核人",
            owner_id="整改人甲",
            context={"verification_notes": ""},
        )
        self.assertEqual(
            result.code, hazard_state.CODE_VERIFICATION_REQUIRED
        )

    def test_illegal_transitions_are_rejected(self) -> None:
        cases = (
            ("open", "start"),
            ("open", "submit_rectification"),
            ("open", "verify_pass"),
            ("assigned", "submit_rectification"),
            ("assigned", "verify_pass"),
            ("in_progress", "assign"),
            ("closed", "start"),
            ("closed", "verify_pass"),
            ("reopened", "start"),
        )
        for from_status, action in cases:
            with self.subTest(from_status=from_status, action=action):
                result = check(
                    from_status,
                    action,
                    actor_role=roles.ROLE_DEMO_ADMIN,
                    context={"target_owner_id": "整改人甲"},
                )
                self.assertFalse(result.allowed)
                self.assertEqual(
                    result.code, hazard_state.CODE_ILLEGAL_TRANSITION
                )

    def test_unknown_status(self) -> None:
        result = check("not_a_status", "assign")
        self.assertEqual(result.code, hazard_state.CODE_UNKNOWN_STATUS)

    def test_demo_admin_can_facilitate_owner_actions(self) -> None:
        result = check(
            "assigned",
            "start",
            actor_role=roles.ROLE_DEMO_ADMIN,
            actor_id="演示管理员",
            owner_id="整改人甲",
        )
        self.assertTrue(result.allowed)


class HazardMappingTests(unittest.TestCase):
    def test_v4_status_mapping_round_trip(self) -> None:
        for legacy in ("待整改", "整改中", "已关闭"):
            canonical = hazard_state.V4_HAZARD_STATUS_TO_V5[legacy]
            self.assertEqual(
                hazard_state.V5_TO_V4_HAZARD_STATUS[canonical], legacy
            )

    def test_terminal_status_is_closed_only(self) -> None:
        self.assertEqual(hazard_state.HAZARD_TERMINAL_STATUSES, ("closed",))
        self.assertEqual(hazard_state.HAZARD_TRANSITIONS["closed"], {"reopen": "reopened"})


if __name__ == "__main__":
    unittest.main()

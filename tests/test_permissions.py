"""P0B tests for the central permission layer."""

from __future__ import annotations

import unittest

from workflow import hazard_state, permissions, permit_state, roles


APPLICANT = {"id": "DEMO-APPLICANT-01", "role": roles.ROLE_APPLICANT, "active": True}
EHS = {"id": "DEMO-EHS-01", "role": roles.ROLE_EHS_REVIEWER, "active": True}
EHS2 = {"id": "DEMO-EHS-02", "role": roles.ROLE_EHS_REVIEWER, "active": True}
APPROVER1 = {"id": "DEMO-APPROVER-01", "role": roles.ROLE_APPROVER, "active": True}
APPROVER2 = {"id": "DEMO-APPROVER-02", "role": roles.ROLE_APPROVER, "active": True}
OWNER1 = {"id": "DEMO-OWNER-01", "role": roles.ROLE_ACTION_OWNER, "active": True}
OWNER2 = {"id": "DEMO-OWNER-02", "role": roles.ROLE_ACTION_OWNER, "active": True}
ADMIN = {"id": "DEMO-ADMIN-01", "role": roles.ROLE_DEMO_ADMIN, "active": True}


def permit_entity(**overrides: object) -> dict[str, object]:
    entity: dict[str, object] = {
        "id": "PERMIT-TEST-001",
        "status": "approval_pending",
        "applicant_id": APPLICANT["id"],
        "owner_id": OWNER1["id"],
        "designated_approver_id": APPROVER1["id"],
        "ehs_reviewer_id": EHS["id"],
    }
    entity.update(overrides)
    return entity


def hazard_entity(**overrides: object) -> dict[str, object]:
    entity: dict[str, object] = {
        "id": "HZ-TEST-001",
        "status": hazard_state.HAZARD_VERIFICATION_PENDING,
        "owner_id": OWNER1["id"],
        "verifier_id": "",
    }
    entity.update(overrides)
    return entity


class RoleMatrixTests(unittest.TestCase):
    def test_applicant_cannot_approve(self) -> None:
        self.assertFalse(permissions.can(APPLICANT, permissions.PERMIT_APPROVE))
        with self.assertRaises(permissions.PermissionDenied):
            permissions.assert_can(APPLICANT, permissions.PERMIT_APPROVE)

    def test_approver_only_handles_assigned_request(self) -> None:
        entity = permit_entity()
        self.assertTrue(
            permissions.can(APPROVER1, permissions.PERMIT_APPROVE, entity)
        )
        self.assertFalse(
            permissions.can(APPROVER2, permissions.PERMIT_APPROVE, entity)
        )
        unassigned = permit_entity(designated_approver_id="")
        self.assertTrue(
            permissions.can(APPROVER2, permissions.PERMIT_APPROVE, unassigned)
        )

    def test_applicant_cannot_approve_own_permit(self) -> None:
        entity = permit_entity(
            applicant_id=APPROVER1["id"], designated_approver_id=APPROVER1["id"]
        )
        self.assertFalse(
            permissions.can(APPROVER1, permissions.PERMIT_APPROVE, entity)
        )

    def test_ehs_reviewer_assignment_restricts_review(self) -> None:
        entity = permit_entity(ehs_reviewer_id=EHS["id"])
        self.assertTrue(permissions.can(EHS, permissions.PERMIT_EHS_REVIEW, entity))
        self.assertFalse(
            permissions.can(EHS2, permissions.PERMIT_EHS_REVIEW, entity)
        )
        open_review = permit_entity(ehs_reviewer_id="")
        self.assertTrue(
            permissions.can(EHS2, permissions.PERMIT_EHS_REVIEW, open_review)
        )

    def test_action_owner_bound_to_assignment(self) -> None:
        entity = hazard_entity(owner_id=OWNER1["id"])
        self.assertTrue(
            permissions.can(OWNER1, permissions.HAZARD_START_RECTIFICATION, entity)
        )
        self.assertFalse(
            permissions.can(OWNER2, permissions.HAZARD_START_RECTIFICATION, entity)
        )
        unassigned = hazard_entity(owner_id="")
        self.assertFalse(
            permissions.can(OWNER1, permissions.HAZARD_START_RECTIFICATION, unassigned)
        )

    def test_owner_cannot_verify_own_rectification(self) -> None:
        entity = hazard_entity(owner_id=EHS["id"])
        self.assertFalse(permissions.can(EHS, permissions.HAZARD_VERIFY, entity))
        other = hazard_entity(owner_id=OWNER1["id"])
        self.assertTrue(permissions.can(EHS, permissions.HAZARD_VERIFY, other))

    def test_designated_verifier_restricts_verification(self) -> None:
        entity = hazard_entity(verifier_id=EHS["id"])
        self.assertTrue(permissions.can(EHS, permissions.HAZARD_VERIFY, entity))
        self.assertFalse(permissions.can(EHS2, permissions.HAZARD_VERIFY, entity))

    def test_admin_can_facilitate_every_action(self) -> None:
        self.assertTrue(
            permissions.can(ADMIN, permissions.PERMIT_APPROVE, permit_entity())
        )
        self.assertTrue(
            permissions.can(
                ADMIN, permissions.HAZARD_VERIFY, hazard_entity(owner_id=ADMIN["id"])
            )
        )

    def test_inactive_user_is_denied(self) -> None:
        inactive = {"id": APPLICANT["id"], "role": roles.ROLE_APPLICANT, "active": False}
        self.assertFalse(permissions.can(inactive, permissions.PERMIT_SUBMIT))

    def test_unknown_permission_is_denied(self) -> None:
        self.assertFalse(permissions.can(APPLICANT, "permit:teleport"))
        with self.assertRaises(permissions.PermissionDenied):
            permissions.assert_can(APPLICANT, "permit:teleport")

    def test_can_and_assert_can_share_one_rule(self) -> None:
        cases = (
            (APPLICANT, permissions.PERMIT_APPROVE, None),
            (APPROVER2, permissions.PERMIT_APPROVE, permit_entity()),
            (OWNER1, permissions.HAZARD_START_RECTIFICATION, hazard_entity()),
            (OWNER2, permissions.HAZARD_START_RECTIFICATION, hazard_entity()),
            (EHS, permissions.HAZARD_VERIFY, hazard_entity(owner_id=EHS["id"])),
            (EHS, permissions.HAZARD_VERIFY, hazard_entity()),
            (ADMIN, permissions.PERMIT_APPROVE, permit_entity()),
            (None, permissions.PERMIT_SUBMIT, None),
        )
        for user, permission, entity in cases:
            with self.subTest(permission=permission):
                allowed = permissions.can(user, permission, entity)
                reason = permissions.explain(user, permission, entity)
                self.assertEqual(allowed, reason == "")
                if allowed:
                    permissions.assert_can(user, permission, entity)
                else:
                    self.assertTrue(reason)
                    with self.assertRaises(permissions.PermissionDenied):
                        permissions.assert_can(user, permission, entity)

    def test_applicant_keeps_own_permit_actions(self) -> None:
        for permission in (
            permissions.PERMIT_CREATE,
            permissions.PERMIT_SUBMIT,
            permissions.PERMIT_PRESTART_CONFIRM,
            permissions.PERMIT_CANCEL,
        ):
            self.assertTrue(permissions.can(APPLICANT, permission), permission)

    def test_audit_read_is_shared_but_others_are_not(self) -> None:
        for user in (APPLICANT, EHS, APPROVER1, OWNER1, ADMIN):
            self.assertTrue(permissions.can(user, permissions.AUDIT_READ))
        self.assertFalse(permissions.can(APPLICANT, permissions.HAZARD_VERIFY))


class StateMachineConsistencyTests(unittest.TestCase):
    def test_permit_permission_roles_match_the_state_machine(self) -> None:
        for action, permission in permissions.PERMIT_ACTION_PERMISSIONS.items():
            with self.subTest(action=action):
                permission_roles = set(permissions.allowed_roles(permission))
                state_roles = set(permit_state.PERMIT_ACTION_ROLES[action])
                if action == "expire":
                    self.assertEqual(state_roles - permission_roles, {"system"})
                else:
                    self.assertEqual(permission_roles, state_roles)

    def test_hazard_permission_roles_match_the_state_machine(self) -> None:
        for action, permission in permissions.HAZARD_ACTION_PERMISSIONS.items():
            with self.subTest(action=action):
                self.assertEqual(
                    set(permissions.allowed_roles(permission)),
                    set(hazard_state.HAZARD_ACTION_ROLES[action]),
                )


if __name__ == "__main__":
    unittest.main()

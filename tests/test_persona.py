"""P0B tests for the demo persona store."""

from __future__ import annotations

import unittest

import db
from services import persona_service
from workflow import audit, permissions, roles


class DemoPersonaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = db.open_database(":memory:")
        self.addCleanup(self.connection.close)

    def _demo_user(self, role: str, index: int = 0) -> dict[str, object]:
        matches = [user for user in persona_service.DEMO_USERS if user["role"] == role]
        return matches[index]

    def test_seed_inserts_every_demo_role(self) -> None:
        users = persona_service.seed_demo_users(self.connection)
        self.assertEqual(len(users), len(persona_service.DEMO_USERS))
        role_set = {str(user["role"]) for user in users}
        for role in (
            roles.ROLE_APPLICANT,
            roles.ROLE_EHS_REVIEWER,
            roles.ROLE_APPROVER,
            roles.ROLE_ACTION_OWNER,
            roles.ROLE_DEMO_ADMIN,
        ):
            self.assertIn(role, role_set)

    def test_seed_is_idempotent_and_audits_only_once(self) -> None:
        persona_service.seed_demo_users(self.connection)
        persona_service.seed_demo_users(self.connection)
        users = persona_service.list_users(self.connection)
        self.assertEqual(len(users), len(persona_service.DEMO_USERS))
        events = audit.list_events(self.connection, entity_type="demo_user")
        self.assertEqual(len(events), len(persona_service.DEMO_USERS))
        self.assertEqual({event["action"] for event in events}, {"demo_user.seeded"})

    def test_require_user_rejects_missing_and_inactive(self) -> None:
        persona_service.seed_demo_users(self.connection)
        applicant = self._demo_user(roles.ROLE_APPLICANT)
        found = persona_service.require_user(self.connection, str(applicant["id"]))
        self.assertEqual(found["id"], applicant["id"])
        with self.assertRaises(ValueError):
            persona_service.require_user(self.connection, "DEMO-NOBODY")
        self.connection.execute(
            "UPDATE users SET active = 0 WHERE id = ?", (applicant["id"],)
        )
        self.connection.commit()
        with self.assertRaises(ValueError):
            persona_service.require_user(self.connection, str(applicant["id"]))

    def test_require_role_user_enforces_permission(self) -> None:
        persona_service.seed_demo_users(self.connection)
        applicant = self._demo_user(roles.ROLE_APPLICANT)
        with self.assertRaises(ValueError):
            persona_service.require_role_user(
                self.connection, str(applicant["id"]), permissions.PERMIT_APPROVE
            )
        approver = self._demo_user(roles.ROLE_APPROVER)
        record = persona_service.require_role_user(
            self.connection, str(approver["id"]), permissions.PERMIT_APPROVE
        )
        self.assertEqual(record["role"], roles.ROLE_APPROVER)

    def test_public_view_is_ui_safe_and_labels_demo(self) -> None:
        persona_service.seed_demo_users(self.connection)
        user = persona_service.get_user(
            self.connection, str(self._demo_user(roles.ROLE_EHS_REVIEWER)["id"])
        )
        view = persona_service.public_view(user)
        self.assertEqual(view["id"], user["id"])
        self.assertIn("role_label", view)
        self.assertTrue(view["is_demo"])
        for forbidden in ("password", "token", "secret"):
            self.assertNotIn(forbidden, view)

    def test_identity_notice_is_explicit(self) -> None:
        self.assertIn("Demo", persona_service.DEMO_IDENTITY_NOTICE)


if __name__ == "__main__":
    unittest.main()

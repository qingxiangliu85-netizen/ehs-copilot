"""P0B tests: assignment commands persist, audit and validate demo users."""

from __future__ import annotations

import unittest

import db
from services import hazard_service, permit_service, persona_service
from workflow import audit, permissions


class AssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = db.open_database(":memory:")
        self.addCleanup(self.connection.close)
        persona_service.seed_demo_users(self.connection)
        self.applicant = persona_service.get_user(
            self.connection, "DEMO-APPLICANT-01"
        )
        self.ehs = persona_service.get_user(self.connection, "DEMO-EHS-01")
        self.approver = persona_service.get_user(
            self.connection, "DEMO-APPROVER-01"
        )
        self.owner1 = persona_service.get_user(self.connection, "DEMO-OWNER-01")
        self.owner2 = persona_service.get_user(self.connection, "DEMO-OWNER-02")
        self.admin = persona_service.get_user(self.connection, "DEMO-ADMIN-01")

    def _create_permit(self, permit_id: str = "PERMIT-ASG-001") -> dict[str, object]:
        return permit_service.create_permit(
            self.connection,
            title="指派测试作业",
            permit_id=permit_id,
            applicant_id="DEMO-APPLICANT-01",
            owner_id="DEMO-OWNER-01",
            data_label="模拟数据 / Demo",
            is_demo=True,
            chemicals=[{"chemical_name": "氢氟酸"}],
            steps=[{"step_no": 1, "name": "配液"}],
            evidence=[
                {
                    "track": "sds",
                    "source": "demo.pdf",
                    "page": 1,
                    "snippet": "危险性",
                }
            ],
            jsa_items=[
                {
                    "work_step": "配液",
                    "hazard": "HF",
                    "consequence": "灼伤",
                    "likelihood": 3,
                    "severity": 3,
                    "residual_likelihood": 2,
                    "residual_severity": 2,
                    "proposed_controls": "PPE",
                }
            ],
            valid_to="2030-12-31T23:59:59",
        )

    def _assignments(
        self, entity_id: str = "PERMIT-ASG-001"
    ) -> list[dict[str, object]]:
        return audit.list_events(
            self.connection, entity_type="permit", entity_id=entity_id
        )

    def test_assign_owner_persists_and_writes_audit(self) -> None:
        self._create_permit()
        permit_service.assign_permit_owner(
            self.connection,
            "PERMIT-ASG-001",
            owner_id="DEMO-OWNER-02",
            user=self.applicant,
        )
        permit = permit_service.get_permit(self.connection, "PERMIT-ASG-001")
        self.assertEqual(permit["owner_id"], "DEMO-OWNER-02")
        self.assertEqual(permit["status"], "draft")
        events = [
            event
            for event in self._assignments()
            if event["action"] == "permit.assign_owner"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "DEMO-APPLICANT-01")
        self.assertIn("DEMO-OWNER-01", events[0]["before_json"])
        self.assertIn("DEMO-OWNER-02", events[0]["after_json"])

    def test_assign_reviewer_and_approver(self) -> None:
        self._create_permit()
        permit_service.assign_permit_ehs_reviewer(
            self.connection,
            "PERMIT-ASG-001",
            reviewer_id="DEMO-EHS-01",
            user=self.ehs,
        )
        permit_service.assign_permit_approver(
            self.connection,
            "PERMIT-ASG-001",
            approver_id="DEMO-APPROVER-01",
            user=self.ehs,
        )
        permit = permit_service.get_permit(self.connection, "PERMIT-ASG-001")
        self.assertEqual(permit["ehs_reviewer_id"], "DEMO-EHS-01")
        self.assertEqual(permit["designated_approver_id"], "DEMO-APPROVER-01")
        actions = {event["action"] for event in self._assignments()}
        self.assertIn("permit.assign_ehs_reviewer", actions)
        self.assertIn("permit.assign_approver", actions)

    def test_assignment_rejects_unknown_or_wrong_role_target(self) -> None:
        self._create_permit()
        with self.assertRaises(ValueError):
            permit_service.assign_permit_owner(
                self.connection,
                "PERMIT-ASG-001",
                owner_id="DEMO-NOBODY",
                user=self.applicant,
            )
        with self.assertRaises(ValueError):
            permit_service.assign_permit_approver(
                self.connection,
                "PERMIT-ASG-001",
                approver_id="DEMO-OWNER-01",
                user=self.ehs,
            )

    def test_assignment_requires_permission(self) -> None:
        self._create_permit()
        with self.assertRaises(permissions.PermissionDenied):
            permit_service.assign_permit_owner(
                self.connection,
                "PERMIT-ASG-001",
                owner_id="DEMO-OWNER-02",
                user=self.approver,
            )
        with self.assertRaises(permissions.PermissionDenied):
            permit_service.assign_permit_owner(
                self.connection,
                "PERMIT-ASG-001",
                owner_id="DEMO-OWNER-02",
                user=self.owner1,
            )
        permit_service.assign_permit_owner(
            self.connection,
            "PERMIT-ASG-001",
            owner_id="DEMO-OWNER-02",
            user=self.admin,
        )
        self.assertEqual(
            permit_service.get_permit(self.connection, "PERMIT-ASG-001")["owner_id"],
            "DEMO-OWNER-02",
        )

    def test_reassigning_the_same_owner_is_a_noop(self) -> None:
        self._create_permit()
        before = len(self._assignments())
        permit_service.assign_permit_owner(
            self.connection,
            "PERMIT-ASG-001",
            owner_id="DEMO-OWNER-01",
            user=self.applicant,
        )
        self.assertEqual(len(self._assignments()), before)

    def test_terminal_permit_cannot_be_reassigned(self) -> None:
        self._create_permit()
        permit_service.cancel_permit(
            self.connection, "PERMIT-ASG-001", reason="计划取消", user=self.applicant
        )
        with self.assertRaises(ValueError):
            permit_service.assign_permit_owner(
                self.connection,
                "PERMIT-ASG-001",
                owner_id="DEMO-OWNER-02",
                user=self.applicant,
            )

    def test_hazard_assignment_validates_demo_user_when_persona_used(self) -> None:
        hazard_service.create_hazard(
            self.connection,
            hazard_id="HZ-ASG-001",
            title="指派测试隐患",
            risk_level="中",
            owner_id="DEMO-OWNER-01",
            corrective_actions=[{"action_text": "整改"}],
            is_demo=True,
            data_label="模拟数据 / Demo",
            actor="system",
        )
        with self.assertRaises(ValueError):
            hazard_service.assign_hazard(
                self.connection,
                "HZ-ASG-001",
                owner_id="整改人甲",
                user=self.ehs,
            )
        hazard_service.assign_hazard(
            self.connection,
            "HZ-ASG-001",
            owner_id="DEMO-OWNER-02",
            user=self.ehs,
        )
        hazard = hazard_service.get_hazard(self.connection, "HZ-ASG-001")
        self.assertEqual(hazard["owner_id"], "DEMO-OWNER-02")
        events = audit.list_events(
            self.connection, entity_type="hazard", entity_id="HZ-ASG-001"
        )
        self.assertEqual(events[-1]["action"], "hazard.assign")
        self.assertEqual(events[-1]["actor"], "DEMO-EHS-01")

    def test_hazard_verifier_assignment_enforces_separation(self) -> None:
        hazard_service.create_hazard(
            self.connection,
            hazard_id="HZ-ASG-002",
            title="验证人指派测试",
            risk_level="中",
            owner_id="DEMO-OWNER-01",
            corrective_actions=[{"action_text": "整改"}],
            actor="system",
        )
        assigned = hazard_service.assign_hazard_verifier(
            self.connection,
            "HZ-ASG-002",
            verifier_id="DEMO-EHS-01",
            user=self.ehs,
        )
        self.assertEqual(assigned["verifier_id"], "DEMO-EHS-01")

        hazard_service.assign_hazard(
            self.connection,
            "HZ-ASG-002",
            owner_id="DEMO-ADMIN-01",
            user=self.ehs,
        )
        with self.assertRaises(ValueError):
            hazard_service.assign_hazard_verifier(
                self.connection,
                "HZ-ASG-002",
                verifier_id="DEMO-ADMIN-01",
                user=self.ehs,
            )

    def test_hazard_verifier_must_have_verify_permission(self) -> None:
        hazard_service.create_hazard(
            self.connection,
            hazard_id="HZ-ASG-003",
            title="验证权限测试",
            risk_level="中",
            owner_id="DEMO-OWNER-01",
            actor="system",
        )
        with self.assertRaises(ValueError):
            hazard_service.assign_hazard_verifier(
                self.connection,
                "HZ-ASG-003",
                verifier_id="DEMO-OWNER-02",
                user=self.ehs,
            )

    def test_legacy_assignment_without_persona_still_works(self) -> None:
        connection = db.open_database(":memory:")
        self.addCleanup(connection.close)
        hazard_service.create_hazard(
            connection,
            hazard_id="HZ-LEGACY-001",
            title="无Persona指派",
            risk_level="中",
            owner_id="整改人甲",
            actor="system",
        )
        updated = hazard_service.assign_hazard(
            connection, "HZ-LEGACY-001", owner_id="整改人乙", actor="EHS审核人甲"
        )
        self.assertEqual(updated["owner_id"], "整改人乙")


if __name__ == "__main__":
    unittest.main()

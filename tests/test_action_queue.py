"""P0B tests for the derived, permission-filtered action queue."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import db
import schema
from services import (
    action_queue_service,
    hazard_service,
    permit_service,
    persona_service,
)
from workflow import action_queue, permissions, sla


NOW = datetime(2026, 9, 13, 10, 0, 0)


class ActionQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = db.open_database(":memory:")
        self.addCleanup(self.connection.close)
        persona_service.seed_demo_users(self.connection)
        self.applicant = persona_service.get_user(
            self.connection, "DEMO-APPLICANT-01"
        )
        self.ehs = persona_service.get_user(self.connection, "DEMO-EHS-01")
        self.approver1 = persona_service.get_user(
            self.connection, "DEMO-APPROVER-01"
        )
        self.approver2 = persona_service.get_user(
            self.connection, "DEMO-APPROVER-02"
        )
        self.owner1 = persona_service.get_user(self.connection, "DEMO-OWNER-01")
        self.owner2 = persona_service.get_user(self.connection, "DEMO-OWNER-02")
        self.admin = persona_service.get_user(self.connection, "DEMO-ADMIN-01")

    def _queue(
        self,
        user: dict[str, object],
        *,
        now: datetime | None = NOW,
        view: str | None = None,
    ) -> list[dict[str, object]]:
        return action_queue_service.build_action_queue(
            self.connection, user, now=now, view=view
        )

    def _create_permit(
        self,
        permit_id: str,
        *,
        designated: str = "DEMO-APPROVER-01",
        ehs_reviewer: str = "DEMO-EHS-01",
        owner: str = "DEMO-OWNER-01",
        now: datetime = NOW,
    ) -> dict[str, object]:
        return permit_service.create_permit(
            self.connection,
            title=f"队列作业 {permit_id}",
            permit_id=permit_id,
            applicant_id="DEMO-APPLICANT-01",
            owner_id=owner,
            ehs_reviewer_id=ehs_reviewer,
            designated_approver_id=designated,
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
            now=now,
        )

    def _create_hazard(
        self,
        hazard_id: str,
        *,
        owner: str = "DEMO-OWNER-01",
        risk: str = "低",
        due_at: str | None = None,
    ) -> dict[str, object]:
        due = (
            due_at
            if due_at is not None
            else (NOW + timedelta(days=5)).isoformat(timespec="seconds")
        )
        return hazard_service.create_hazard(
            self.connection,
            hazard_id=hazard_id,
            title=f"队列隐患 {hazard_id}",
            risk_level=risk,
            owner_id=owner,
            due_at=due,
            corrective_actions=[{"action_text": "整改措施"}],
            is_demo=True,
            data_label="模拟数据 / Demo",
            actor="system",
            now=NOW,
        )

    def _actions(self, items: list[dict[str, object]]) -> list[str]:
        return [str(item["action_type"]) for item in items]

    def test_permit_queue_moves_with_each_status(self) -> None:
        self._create_permit("PERMIT-Q-001")
        self.assertEqual(self._queue(self.ehs), [])
        self.assertEqual(self._queue(self.applicant), [])

        permit_service.submit_permit(
            self.connection, "PERMIT-Q-001", user=self.applicant, now=NOW
        )
        ehs_items = self._queue(self.ehs)
        self.assertEqual(self._actions(ehs_items), [action_queue.ACTION_EHS_REVIEW])
        self.assertEqual(ehs_items[0]["entity_id"], "PERMIT-Q-001")
        self.assertEqual(self._queue(self.approver1), [])
        self.assertEqual(self._queue(self.owner1), [])

        permit_service.complete_ehs_review(
            self.connection, "PERMIT-Q-001", user=self.ehs, decision="confirm", now=NOW
        )
        self.assertEqual(self._queue(self.ehs), [])
        approver_items = self._queue(self.approver1)
        self.assertEqual(self._actions(approver_items), [action_queue.ACTION_APPROVE])
        self.assertEqual(self._queue(self.approver2), [])

        permit_service.decide_approval(
            self.connection, "PERMIT-Q-001", user=self.approver1, decision="approve"
        )
        owner_items = self._queue(self.owner1)
        self.assertEqual(
            self._actions(owner_items), [action_queue.ACTION_PRESTART_CONFIRM]
        )
        self.assertEqual(self._queue(self.approver1), [])
        self.assertEqual(self._queue(self.applicant), [])

        permit_service.confirm_prestart(
            self.connection,
            "PERMIT-Q-001",
            [{"item_code": "PPE", "item_text": "防护就位", "result": "pass"}],
            user=self.owner1,
        )
        self.assertEqual(self._queue(self.owner1), [])

        permit_service.complete_work(
            self.connection,
            "PERMIT-Q-001",
            user=self.owner1,
            handback_note="现场已交接",
        )
        close_items = self._queue(self.ehs)
        self.assertEqual(self._actions(close_items), [action_queue.ACTION_CLOSE])

        permit_service.close_permit(
            self.connection, "PERMIT-Q-001", user=self.ehs, note="闭环"
        )
        self.assertEqual(self._queue(self.ehs), [])

    def test_hazard_assignment_change_moves_the_item(self) -> None:
        self._create_hazard("HZ-Q-ASSIGN", owner="DEMO-OWNER-01")
        hazard_service.assign_hazard(
            self.connection, "HZ-Q-ASSIGN", owner_id="DEMO-OWNER-01", user=self.ehs
        )
        self.assertEqual(
            self._actions(self._queue(self.owner1)),
            [action_queue.ACTION_START_RECTIFICATION],
        )
        self.assertEqual(self._queue(self.owner2), [])

        hazard_service.assign_hazard(
            self.connection, "HZ-Q-ASSIGN", owner_id="DEMO-OWNER-02", user=self.ehs
        )
        self.assertEqual(self._queue(self.owner1), [])
        moved = self._queue(self.owner2)
        self.assertEqual(
            self._actions(moved), [action_queue.ACTION_START_RECTIFICATION]
        )
        self.assertEqual(moved[0]["assigned_to"], "DEMO-OWNER-02")

    def test_completing_the_action_swaps_in_the_next_item(self) -> None:
        self._create_hazard("HZ-Q-FLOW", owner="DEMO-OWNER-01")
        hazard_service.assign_hazard(
            self.connection, "HZ-Q-FLOW", owner_id="DEMO-OWNER-01", user=self.ehs
        )
        hazard_service.start_rectification(
            self.connection, "HZ-Q-FLOW", user=self.owner1
        )
        self.assertEqual(
            self._actions(self._queue(self.owner1)),
            [action_queue.ACTION_SUBMIT_RECTIFICATION],
        )
        hazard_service.submit_rectification(
            self.connection,
            "HZ-Q-FLOW",
            user=self.owner1,
            evidence=[{"file_name": "fix.pdf", "evidence_type": "照片"}],
        )
        self.assertEqual(self._queue(self.owner1), [])
        verify_items = self._queue(self.ehs)
        self.assertEqual(self._actions(verify_items), [action_queue.ACTION_VERIFY])
        self.assertEqual(verify_items[0]["entity_id"], "HZ-Q-FLOW")

    def test_approver_only_sees_own_permits(self) -> None:
        self._create_permit("PERMIT-Q-A1", designated="DEMO-APPROVER-01")
        self._create_permit("PERMIT-Q-A2", designated="DEMO-APPROVER-02")
        for permit_id in ("PERMIT-Q-A1", "PERMIT-Q-A2"):
            permit_service.submit_permit(
                self.connection, permit_id, user=self.applicant, now=NOW
            )
            permit_service.complete_ehs_review(
                self.connection,
                permit_id,
                user=self.ehs,
                decision="confirm",
                now=NOW,
            )
        first = self._queue(self.approver1)
        second = self._queue(self.approver2)
        self.assertEqual([item["entity_id"] for item in first], ["PERMIT-Q-A1"])
        self.assertEqual([item["entity_id"] for item in second], ["PERMIT-Q-A2"])

    def test_queue_is_deduplicated(self) -> None:
        self._create_permit("PERMIT-Q-D1")
        self._create_permit("PERMIT-Q-D2")
        for permit_id in ("PERMIT-Q-D1", "PERMIT-Q-D2"):
            permit_service.submit_permit(
                self.connection, permit_id, user=self.applicant, now=NOW
            )
        items = self._queue(self.admin)
        keys = [
            (item["entity_type"], item["entity_id"], item["action_type"])
            for item in items
        ]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(items), 2)

    def test_overdue_ranks_before_normal_and_risk_is_separate(self) -> None:
        overdue_due = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
        normal_due = (NOW + timedelta(days=5)).isoformat(timespec="seconds")
        self._create_hazard("HZ-Q-OVD", risk="低", due_at=overdue_due)
        self._create_hazard("HZ-Q-BIG", risk="重大", due_at=normal_due)
        for hazard_id in ("HZ-Q-OVD", "HZ-Q-BIG"):
            hazard_service.assign_hazard(
                self.connection,
                hazard_id,
                owner_id="DEMO-OWNER-01",
                user=self.ehs,
            )
        items = self._queue(self.owner1)
        self.assertEqual(items[0]["entity_id"], "HZ-Q-OVD")
        self.assertTrue(items[0]["is_overdue"])
        self.assertEqual(items[0]["risk_level"], "低")
        big = next(item for item in items if item["entity_id"] == "HZ-Q-BIG")
        self.assertFalse(big["is_overdue"])
        self.assertEqual(big["risk_level"], "重大")
        self.assertFalse(big["is_overdue"] and big["risk_level"] == "低")
        self.assertGreater(items[0]["priority_score"], big["priority_score"])

    def test_queue_views_filter_by_deadline(self) -> None:
        self._create_hazard(
            "HZ-Q-V1", due_at=(NOW - timedelta(days=1)).isoformat(timespec="seconds")
        )
        self._create_hazard(
            "HZ-Q-V2", due_at=(NOW + timedelta(days=1)).isoformat(timespec="seconds")
        )
        self._create_hazard(
            "HZ-Q-V3", due_at=(NOW + timedelta(days=6)).isoformat(timespec="seconds")
        )
        for hazard_id in ("HZ-Q-V1", "HZ-Q-V2", "HZ-Q-V3"):
            hazard_service.assign_hazard(
                self.connection,
                hazard_id,
                owner_id="DEMO-OWNER-01",
                user=self.ehs,
            )
        overdue = self._queue(self.owner1, view=action_queue.QUEUE_VIEW_OVERDUE)
        due = self._queue(self.owner1, view=action_queue.QUEUE_VIEW_DUE)
        self.assertEqual([item["entity_id"] for item in overdue], ["HZ-Q-V1"])
        self.assertEqual([item["entity_id"] for item in due], ["HZ-Q-V2"])
        with self.assertRaises(ValueError):
            self._queue(self.owner1, view="not-a-view")

    def test_sla_deadline_is_persisted_and_not_recomputed(self) -> None:
        self._create_permit("PERMIT-Q-SLA")
        permit_service.submit_permit(
            self.connection, "PERMIT-Q-SLA", user=self.applicant, now=NOW
        )
        permit = permit_service.get_permit(self.connection, "PERMIT-Q-SLA")
        expected = sla.calculate_due_at(
            NOW, sla.approval_days(str(permit["residual_risk_level"]))
        ).isoformat(timespec="seconds")
        self.assertEqual(permit["approval_due_at"], expected)

        permit_service.complete_ehs_review(
            self.connection,
            "PERMIT-Q-SLA",
            user=self.ehs,
            decision="confirm",
            now=NOW + timedelta(days=1),
        )
        after = permit_service.get_permit(self.connection, "PERMIT-Q-SLA")
        self.assertEqual(after["approval_due_at"], expected)
        again = permit_service.get_permit(self.connection, "PERMIT-Q-SLA")
        self.assertEqual(again["approval_due_at"], expected)
        items = self._queue(self.ehs, now=NOW)
        approve_items = self._queue(self.approver1, now=NOW + timedelta(days=2))
        self.assertEqual(approve_items[0]["due_at"], expected)

    def test_hazard_deadlines_follow_the_risk_policy(self) -> None:
        hazard_service.create_hazard(
            self.connection,
            hazard_id="HZ-Q-POLICY",
            title="SLA 策略隐患",
            risk_level="重大",
            owner_id="DEMO-OWNER-01",
            corrective_actions=[{"action_text": "整改"}],
            actor="system",
            now=NOW,
        )
        hazard = hazard_service.get_hazard(self.connection, "HZ-Q-POLICY")
        self.assertEqual(
            hazard["due_at"],
            sla.calculate_due_at(NOW, sla.rectification_days("重大")).isoformat(
                timespec="seconds"
            ),
        )

    def test_queue_is_not_a_stored_table(self) -> None:
        names = schema.table_names(self.connection)
        self.assertFalse(
            any("queue" in name or name.startswith("action") for name in names)
        )

    def test_queue_summary_counts(self) -> None:
        self._create_hazard(
            "HZ-Q-S1", due_at=(NOW - timedelta(days=1)).isoformat(timespec="seconds")
        )
        self._create_hazard(
            "HZ-Q-S2", due_at=NOW.isoformat(timespec="seconds")
        )
        for hazard_id in ("HZ-Q-S1", "HZ-Q-S2"):
            hazard_service.assign_hazard(
                self.connection,
                hazard_id,
                owner_id="DEMO-OWNER-01",
                user=self.ehs,
            )
        summary = action_queue_service.queue_summary(
            self.connection, self.owner1, now=NOW
        )
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["overdue"], 1)
        self.assertEqual(summary["due_soon"], 1)

    def test_demo_admin_sees_every_item(self) -> None:
        self._create_permit("PERMIT-Q-ADM")
        permit_service.submit_permit(
            self.connection, "PERMIT-Q-ADM", user=self.applicant, now=NOW
        )
        self._create_hazard("HZ-Q-ADM")
        admin_items = self._queue(self.admin)
        entities = {(item["entity_type"], item["entity_id"]) for item in admin_items}
        self.assertIn(("permit", "PERMIT-Q-ADM"), entities)
        self.assertIn(("hazard", "HZ-Q-ADM"), entities)

    def test_service_permission_checks_match_the_ui_helper(self) -> None:
        self._create_permit("PERMIT-Q-PERM")
        permit_service.submit_permit(
            self.connection, "PERMIT-Q-PERM", user=self.applicant, now=NOW
        )
        permit_service.complete_ehs_review(
            self.connection,
            "PERMIT-Q-PERM",
            user=self.ehs,
            decision="confirm",
            now=NOW,
        )
        permit = permit_service.get_permit(self.connection, "PERMIT-Q-PERM")
        self.assertFalse(
            permissions.can(self.applicant, permissions.PERMIT_APPROVE, permit)
        )
        with self.assertRaises(permissions.PermissionDenied):
            permit_service.decide_approval(
                self.connection,
                "PERMIT-Q-PERM",
                user=self.applicant,
                decision="approve",
            )
        self.assertFalse(
            permissions.can(self.approver2, permissions.PERMIT_APPROVE, permit)
        )
        with self.assertRaises(permissions.PermissionDenied):
            permit_service.decide_approval(
                self.connection,
                "PERMIT-Q-PERM",
                user=self.approver2,
                decision="approve",
            )
        approved = permit_service.decide_approval(
            self.connection,
            "PERMIT-Q-PERM",
            user=self.approver1,
            decision="approve",
        )
        self.assertEqual(approved["status"], "approved")

    def test_owner_cannot_verify_but_ehs_reviewer_can(self) -> None:
        self._create_hazard("HZ-Q-VERIFY", owner="DEMO-OWNER-01")
        hazard_service.assign_hazard(
            self.connection, "HZ-Q-VERIFY", owner_id="DEMO-OWNER-01", user=self.ehs
        )
        hazard_service.start_rectification(
            self.connection, "HZ-Q-VERIFY", user=self.owner1
        )
        hazard_service.submit_rectification(
            self.connection,
            "HZ-Q-VERIFY",
            user=self.owner1,
            evidence=[{"file_name": "fix.pdf", "evidence_type": "照片"}],
        )
        hazard = hazard_service.get_hazard(self.connection, "HZ-Q-VERIFY")
        self.assertFalse(
            permissions.can(self.owner1, permissions.HAZARD_VERIFY, hazard)
        )
        with self.assertRaises(permissions.PermissionDenied):
            hazard_service.verify_hazard(
                self.connection,
                "HZ-Q-VERIFY",
                user=self.owner1,
                result="pass",
                notes="自行验证",
            )
        closed = hazard_service.verify_hazard(
            self.connection,
            "HZ-Q-VERIFY",
            user=self.ehs,
            result="pass",
            notes="现场复核通过",
        )
        self.assertEqual(closed["status"], "closed")

    def test_target_fields_point_at_the_detail_pages(self) -> None:
        self._create_permit("PERMIT-Q-TGT")
        permit_service.submit_permit(
            self.connection, "PERMIT-Q-TGT", user=self.applicant, now=NOW
        )
        item = self._queue(self.ehs)[0]
        self.assertEqual(item["target_page"], "permit_detail")
        self.assertEqual(item["target_id"], "PERMIT-Q-TGT")


if __name__ == "__main__":
    unittest.main()

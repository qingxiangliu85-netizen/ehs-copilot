"""P0A tests: business writes and audit events share one transaction."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import db
from services import permit_service
from workflow import audit


def permit_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": "HF酸洗模拟作业",
        "permit_id": "PERMIT-T001",
        "applicant_id": "申请人甲",
        "owner_id": "执行人甲",
        "area": "酸洗区（模拟）",
        "data_label": "模拟数据 / Demo，不代表真实企业记录",
        "is_demo": True,
        "chemicals": [
            {"chemical_name": "氢氟酸", "aliases": ["HF"], "sds_status": "demo"}
        ],
        "steps": [{"step_no": 1, "name": "密闭配液"}],
        "evidence": [
            {
                "track": "sds",
                "source": "hf_demo_synthetic.pdf",
                "page": 4,
                "sections": "8",
                "snippet": "佩戴耐HF防护装备",
                "data_label": "Demo / Synthetic SDS",
            }
        ],
        "jsa_items": [
            {
                "work_step": "密闭配液",
                "hazard": "HF飞溅",
                "consequence": "化学灼伤",
                "likelihood": 4,
                "severity": 5,
                "existing_controls": "局部排风",
                "proposed_controls": "面屏与耐酸手套",
                "residual_likelihood": 2,
                "residual_severity": 2,
            }
        ],
        "valid_to": "2030-12-31T23:59:59",
    }
    payload.update(overrides)
    return payload


class AuditAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "audit.db")
        self.connection = db.open_database(self.path)
        self.addCleanup(self.connection.close)

    def _create(self, **overrides: object) -> dict[str, object]:
        return permit_service.create_permit(
            self.connection, actor="申请人甲", **permit_payload(**overrides)
        )

    def test_successful_mutation_writes_business_and_audit(self) -> None:
        permit = self._create()
        self.assertEqual(permit["status"], "draft")
        self.assertEqual(
            audit.count_events(
                self.connection,
                entity_type=audit.ENTITY_PERMIT,
                entity_id="PERMIT-T001",
            ),
            1,
        )

        permit_service.submit_permit(
            self.connection, "PERMIT-T001", actor="申请人甲"
        )
        updated = permit_service.get_permit(self.connection, "PERMIT-T001")
        self.assertEqual(updated["status"], "ehs_review")

        events = audit.list_events(
            self.connection,
            entity_type=audit.ENTITY_PERMIT,
            entity_id="PERMIT-T001",
        )
        self.assertEqual(len(events), 2)
        submit_event = events[-1]
        self.assertEqual(submit_event["action"], "permit.submit")
        self.assertEqual(submit_event["actor"], "申请人甲")
        self.assertEqual(submit_event["from_state"], "draft")
        self.assertEqual(submit_event["to_state"], "ehs_review")
        self.assertTrue(submit_event["correlation_id"])
        self.assertTrue(submit_event["before_json"])
        self.assertTrue(submit_event["after_json"])
        self.assertIn("ehs_review", submit_event["after_json"])

    def test_illegal_transition_leaves_no_audit(self) -> None:
        self._create()
        before = audit.count_events(self.connection)
        with self.assertRaises(ValueError):
            permit_service.transition_permit(
                self.connection,
                "PERMIT-T001",
                "confirm",
                actor="EHS审核人",
                actor_role="ehs_reviewer",
            )
        after = audit.count_events(self.connection)
        self.assertEqual(before, after)
        self.assertEqual(
            permit_service.get_permit(self.connection, "PERMIT-T001")["status"],
            "draft",
        )

    def test_audit_failure_rolls_back_business_write(self) -> None:
        self._create()
        before = audit.count_events(self.connection)
        with mock.patch(
            "workflow.audit.record_event",
            side_effect=RuntimeError("audit store unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                permit_service.submit_permit(
                    self.connection, "PERMIT-T001", actor="申请人甲"
                )
        self.assertEqual(
            permit_service.get_permit(self.connection, "PERMIT-T001")["status"],
            "draft",
        )
        self.assertEqual(audit.count_events(self.connection), before)

    def test_business_failure_does_not_leave_orphan_audit(self) -> None:
        with self.assertRaises(ValueError):
            permit_service.create_permit(
                self.connection,
                actor="申请人甲",
                **permit_payload(
                    evidence=[
                        {
                            "track": "sds",
                            "source": "fake.pdf",
                            "source_url": "https://example.com/not-sds",
                            "page": 1,
                            "snippet": "不应写入",
                        }
                    ]
                ),
            )
        self.assertEqual(audit.count_events(self.connection), 0)
        self.assertIsNone(
            permit_service.get_permit(self.connection, "PERMIT-T001")
        )

        self._create()
        with self.assertRaises(ValueError):
            self._create()
        self.assertEqual(
            audit.count_events(
                self.connection,
                entity_type=audit.ENTITY_PERMIT,
                entity_id="PERMIT-T001",
            ),
            1,
        )

    def test_audit_api_is_append_only(self) -> None:
        for name in (
            "update_event",
            "delete_event",
            "remove_event",
            "update_audit_event",
            "delete_audit_event",
        ):
            self.assertFalse(hasattr(audit, name), name)

    def test_event_ids_are_unique_and_events_filterable(self) -> None:
        self._create()
        permit_service.submit_permit(
            self.connection, "PERMIT-T001", actor="申请人甲"
        )
        events = audit.list_events(self.connection)
        self.assertEqual(len({event["event_id"] for event in events}), len(events))
        self.assertEqual(
            len(audit.list_events(self.connection, action="permit.submit")), 1
        )
        correlation = events[0]["correlation_id"]
        grouped = audit.list_events(self.connection, correlation_id=correlation)
        self.assertEqual([event["action"] for event in grouped], ["permit.created"])

    def test_event_json_snapshots_are_recorded(self) -> None:
        self._create()
        event = audit.list_events(
            self.connection,
            entity_type=audit.ENTITY_PERMIT,
            entity_id="PERMIT-T001",
        )[0]
        self.assertEqual(event["before_json"], "")
        self.assertIn("PERMIT-T001", event["after_json"])
        self.assertIn("draft", event["after_json"])


if __name__ == "__main__":
    unittest.main()

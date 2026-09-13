"""P0A tests: SQLite persistence, SDS hard gate and demo labelling."""

from __future__ import annotations

import os
import tempfile
import unittest

import db
import schema
from services import hazard_service, permit_service


def permit_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": "HF酸洗模拟作业",
        "permit_id": "PERMIT-P001",
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


def public_only_evidence() -> list[dict[str, object]]:
    return [
        {
            "track": "public_sources",
            "evidence_id": "ev-public-1",
            "source_title": "NIOSH HF",
            "source_url": "https://www.cdc.gov/niosh/",
            "organization": "CDC/NIOSH",
            "snippet": "公开来源安全信息",
            "retrieved_at": "2026-09-01",
            "topic": "ppe",
        }
    ]


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "persistence.db")

    def test_schema_initialization_is_idempotent(self) -> None:
        connection = db.open_database(self.path)
        self.addCleanup(connection.close)
        schema.initialize(connection)
        schema.initialize(connection)
        tables = schema.table_names(connection)
        for expected in schema.EXPECTED_TABLES:
            self.assertIn(expected, tables)
        self.assertEqual(schema.schema_version(connection), schema.SCHEMA_VERSION)

    def test_data_survives_reconnection(self) -> None:
        connection = db.open_database(self.path)
        permit_service.create_permit(
            connection, actor="申请人甲", **permit_payload()
        )
        hazard_service.create_hazard(
            connection,
            permit_id="PERMIT-P001",
            title="PPE配置不完整",
            owner_id="整改人甲",
            corrective_actions=[{"action_text": "补齐PPE配置"}],
            actor="执行人甲",
            is_demo=True,
            data_label="模拟数据 / Demo",
        )
        connection.close()

        reopened = db.open_database(self.path)
        self.addCleanup(reopened.close)
        permit = permit_service.get_permit(reopened, "PERMIT-P001")
        self.assertIsNotNone(permit)
        self.assertEqual(permit["status"], "draft")
        self.assertEqual(len(permit["chemicals"]), 1)
        self.assertEqual(len(permit["evidence"]["sds"]), 1)
        self.assertEqual(len(permit["jsa_items"]), 1)
        self.assertEqual(permit["linked_hazard_ids"], ["HZ-001"])
        hazard = hazard_service.get_hazard(reopened, "HZ-001")
        self.assertEqual(hazard["status"], "open")
        self.assertEqual(len(hazard["corrective_actions"]), 1)

    def test_sds_hard_gate_is_enforced_by_the_service(self) -> None:
        connection = db.open_database(":memory:")
        self.addCleanup(connection.close)
        permit_service.create_permit(
            connection,
            actor="申请人甲",
            **permit_payload(
                evidence=public_only_evidence(),
            ),
        )
        with self.assertRaises(ValueError) as error:
            permit_service.submit_permit(
                connection, "PERMIT-P001", actor="申请人甲"
            )
        self.assertIn("SDS", str(error.exception))
        self.assertEqual(
            permit_service.get_permit(connection, "PERMIT-P001")["status"],
            "draft",
        )

        permit_service.add_evidence(
            connection,
            "PERMIT-P001",
            track="sds",
            item={
                "source": "hf_demo_synthetic.pdf",
                "page": 2,
                "snippet": "危险性概述：可致严重灼伤",
                "data_label": "Demo / Synthetic SDS",
            },
            actor="EHS审核人甲",
        )
        submitted = permit_service.submit_permit(
            connection, "PERMIT-P001", actor="申请人甲"
        )
        self.assertEqual(submitted["status"], "ehs_review")

    def test_public_evidence_never_satisfies_sds_track(self) -> None:
        connection = db.open_database(":memory:")
        self.addCleanup(connection.close)
        permit_service.create_permit(
            connection, actor="申请人甲", **permit_payload()
        )
        with self.assertRaises(ValueError):
            permit_service.add_evidence(
                connection,
                "PERMIT-P001",
                track="sds",
                item={
                    "source": "quote.pdf",
                    "source_url": "https://example.com/quote",
                    "page": 1,
                    "snippet": "公开来源",
                },
                actor="EHS审核人甲",
            )
        with self.assertRaises(ValueError):
            permit_service.add_evidence(
                connection,
                "PERMIT-P001",
                track="public_sources",
                item={
                    "source": "quote.pdf",
                    "page": 1,
                    "snippet": "SDS 形状的条目",
                },
                actor="EHS审核人甲",
            )

    def test_demo_sds_marking_is_preserved(self) -> None:
        connection = db.open_database(":memory:")
        self.addCleanup(connection.close)
        permit_service.create_permit(
            connection, actor="申请人甲", **permit_payload()
        )
        permit = permit_service.get_permit(connection, "PERMIT-P001")
        self.assertTrue(permit["is_demo"])
        self.assertEqual(permit["data_label"], "模拟数据 / Demo，不代表真实企业记录")
        sds_item = permit["evidence"]["sds"][0]
        self.assertTrue(sds_item["is_demo"])
        self.assertEqual(sds_item["data_label"], "Demo / Synthetic SDS")

    def test_evidence_tracks_stay_separated_in_storage(self) -> None:
        connection = db.open_database(":memory:")
        self.addCleanup(connection.close)
        permit_service.create_permit(
            connection,
            actor="申请人甲",
            **permit_payload(evidence=public_only_evidence()),
        )
        permit_service.add_evidence(
            connection,
            "PERMIT-P001",
            track="sds",
            item={
                "source": "hf_demo_synthetic.pdf",
                "page": 5,
                "snippet": "泄漏处置",
            },
            actor="EHS审核人甲",
        )
        permit = permit_service.get_permit(connection, "PERMIT-P001")
        self.assertEqual(len(permit["evidence"]["public_sources"]), 1)
        self.assertEqual(len(permit["evidence"]["sds"]), 1)
        self.assertEqual(
            permit["evidence"]["public_sources"][0]["organization"], "CDC/NIOSH"
        )
        self.assertEqual(permit["evidence"]["sds"][0]["source"], "hf_demo_synthetic.pdf")

    def test_approval_flow_respects_designated_approver(self) -> None:
        connection = db.open_database(":memory:")
        self.addCleanup(connection.close)
        permit_service.create_permit(
            connection,
            actor="申请人甲",
            **permit_payload(designated_approver_id="批准人甲"),
        )
        permit_service.submit_permit(connection, "PERMIT-P001", actor="申请人甲")
        permit_service.complete_ehs_review(
            connection,
            "PERMIT-P001",
            actor="EHS审核人甲",
            decision="confirm",
        )
        with self.assertRaises(ValueError):
            permit_service.decide_approval(
                connection,
                "PERMIT-P001",
                actor="批准人乙",
                decision="approve",
            )
        approved = permit_service.decide_approval(
            connection,
            "PERMIT-P001",
            actor="批准人甲",
            decision="approve",
        )
        self.assertEqual(approved["status"], "approved")

    def test_hazard_lifecycle_controls_permit_close_gate(self) -> None:
        connection = db.open_database(":memory:")
        self.addCleanup(connection.close)
        permit_service.create_permit(
            connection, actor="申请人甲", **permit_payload()
        )
        permit_service.submit_permit(connection, "PERMIT-P001", actor="申请人甲")
        permit_service.complete_ehs_review(
            connection,
            "PERMIT-P001",
            actor="EHS审核人甲",
            decision="confirm",
        )
        permit_service.decide_approval(
            connection,
            "PERMIT-P001",
            actor="批准人甲",
            decision="approve",
        )
        active = permit_service.confirm_prestart(
            connection,
            "PERMIT-P001",
            [
                {
                    "item_code": "PPE",
                    "item_text": "HF 防护装备已就位",
                    "result": "pass",
                }
            ],
            actor="执行人甲",
        )
        self.assertEqual(active["status"], "active")

        hazard_service.create_hazard(
            connection,
            permit_id="PERMIT-P001",
            title="PPE配置不完整",
            risk_level="高",
            owner_id="整改人甲",
            due_at="2030-12-01",
            corrective_actions=[{"action_text": "补齐PPE配置"}],
            actor="执行人甲",
            is_demo=True,
            data_label="模拟数据 / Demo",
        )
        permit_service.complete_work(
            connection,
            "PERMIT-P001",
            actor="执行人甲",
            handback_note="现场已交接",
        )
        with self.assertRaises(ValueError) as error:
            permit_service.close_permit(
                connection, "PERMIT-P001", actor="EHS审核人甲"
            )
        self.assertIn("未关闭关联隐患", str(error.exception))

        hazard_service.assign_hazard(
            connection,
            "HZ-001",
            owner_id="整改人甲",
            actor="EHS审核人甲",
        )
        hazard_service.start_rectification(
            connection, "HZ-001", actor="整改人甲"
        )
        pending = hazard_service.submit_rectification(
            connection,
            "HZ-001",
            actor="整改人甲",
            evidence=[
                {
                    "file_name": "ppe_fix.pdf",
                    "evidence_type": "照片",
                    "note": "更换后的PPE",
                }
            ],
        )
        self.assertEqual(pending["status"], "verification_pending")

        closed_hazard = hazard_service.verify_hazard(
            connection,
            "HZ-001",
            actor="EHS审核人甲",
            result="pass",
            notes="现场复核通过",
        )
        self.assertEqual(closed_hazard["status"], "closed")
        self.assertEqual(closed_hazard["verification_result"], "pass")
        closed_permit = permit_service.close_permit(
            connection, "PERMIT-P001", actor="EHS审核人甲", note="闭环"
        )
        self.assertEqual(closed_permit["status"], "closed")
        self.assertEqual(closed_permit["open_hazard_count"], 0)

        events = db_audit_actions(connection)
        self.assertIn("hazard.verify_pass", events)
        self.assertIn("permit.close", events)

    def test_cancelled_permit_is_terminal(self) -> None:
        connection = db.open_database(":memory:")
        self.addCleanup(connection.close)
        permit_service.create_permit(
            connection, actor="申请人甲", **permit_payload()
        )
        cancelled = permit_service.cancel_permit(
            connection, "PERMIT-P001", actor="申请人甲", reason="计划取消"
        )
        self.assertEqual(cancelled["status"], "cancelled")
        with self.assertRaises(ValueError):
            permit_service.submit_permit(
                connection, "PERMIT-P001", actor="申请人甲"
            )


def db_audit_actions(connection) -> list[str]:
    from workflow import audit

    return [event["action"] for event in audit.list_events(connection)]


if __name__ == "__main__":
    unittest.main()

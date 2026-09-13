"""P0A tests: the V4 compatibility adapter keeps legacy Job/Hazard data usable."""

from __future__ import annotations

import os
import tempfile
import unittest

import db
import hazards
from demo_cases import create_demo_hf_case
from jobs import JOB_FIELDS, JOB_STATUSES, create_job
from services import hazard_service, legacy_adapter, permit_service
from workflow import hazard_state, permit_state


def sample_public_item() -> dict[str, object]:
    return {
        "evidence_id": "ev-1",
        "source_title": "NIOSH Hydrofluoric Acid",
        "source_url": "https://www.cdc.gov/niosh/",
        "organization": "CDC/NIOSH",
        "passage": "Wear protective clothing.",
        "retrieved_at": "2026-09-01",
        "topic": "ppe",
        "section": "prevention",
    }


def sample_sds_item() -> dict[str, object]:
    return {
        "source": "hf_demo_synthetic.pdf",
        "page": 3,
        "sections": "2",
        "snippet": "引起严重皮肤灼伤和眼损伤。",
    }


class LegacyJobAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "adapter.db")

    def test_job_status_mapping_covers_every_v4_status(self) -> None:
        for status in JOB_STATUSES:
            self.assertIn(status, permit_state.V4_JOB_STATUS_TO_V5)
        self.assertEqual(legacy_adapter.job_status_to_v5("未知状态"), "")

    def test_job_to_permit_keeps_identity_tracks_and_labels(self) -> None:
        case = create_demo_hf_case()
        job = case["job"]
        mapped = legacy_adapter.job_to_permit(job)

        self.assertEqual(mapped["permit_id"], job["job_id"])
        self.assertEqual(mapped["title"], job["job_name"])
        self.assertEqual(mapped["status"], "draft")
        self.assertEqual(len(mapped["chemicals"]), 1)
        self.assertEqual(mapped["chemicals"][0]["chemical_name"], "氢氟酸")
        public = [
            item for item in mapped["evidence"] if item["track"] == "public_sources"
        ]
        sds = [item for item in mapped["evidence"] if item["track"] == "sds"]
        self.assertEqual(len(public), len(job["public_evidence"]))
        self.assertEqual(sds, [])
        self.assertTrue(mapped["is_demo"])
        self.assertEqual(mapped["data_label"], job["data_label"])

    def test_legacy_job_import_persists_and_round_trips(self) -> None:
        connection = db.open_database(self.path)
        self.addCleanup(connection.close)
        case = create_demo_hf_case()
        job = case["job"]

        imported = permit_service.import_legacy_job(connection, job)
        self.assertEqual(imported["id"], job["job_id"])
        self.assertEqual(imported["status"], "draft")
        self.assertTrue(imported["is_demo"])
        self.assertEqual(
            len(imported["evidence"]["public_sources"]),
            len(job["public_evidence"]),
        )
        self.assertEqual(imported["evidence"]["sds"], [])
        self.assertEqual(len(imported["steps"]), len(job["steps"]))

        round_trip = legacy_adapter.permit_to_job(imported)
        self.assertEqual(round_trip["job_id"], job["job_id"])
        self.assertEqual(round_trip["job_name"], job["job_name"])
        self.assertEqual(round_trip["status"], "草稿")
        self.assertEqual(round_trip["data_label"], job["data_label"])
        self.assertEqual(
            len(round_trip["public_evidence"]), len(job["public_evidence"])
        )
        self.assertEqual(round_trip["sds_evidence"], [])
        self.assertEqual(set(JOB_FIELDS), set(round_trip.keys()))

    def test_confirmed_jsa_maps_to_recomputed_jsa_items(self) -> None:
        connection = db.open_database(":memory:")
        self.addCleanup(connection.close)
        job = create_job(
            job_id="JOB-ADAPTER-001",
            job_name="适配测试作业",
            chemicals=[{"name": "氢氟酸", "aliases": ["HF"]}],
            steps=[{"order": 1, "name": "密闭配液"}],
            created_by="申请人甲",
        )
        job["public_evidence"] = [sample_public_item()]
        job["sds_evidence"] = [sample_sds_item()]
        job["jsa_confirmation"] = {
            "confirmed_by": "EHS审核人甲",
            "confirmed_at": "2026-09-13T10:00:00",
            "final": {
                "作业步骤": "密闭配液",
                "危害因素": "HF飞溅",
                "可能后果": "化学灼伤",
                "可能性L": 4,
                "严重度S": 5,
                "现有控制措施": "局部排风",
                "建议控制措施": "面屏与耐酸手套",
                "控制后可能性L": 2,
                "控制后严重度S": 2,
                "风险值R": 20,
                "风险等级": "重大风险",
                "残余风险R": 4,
                "残余风险等级": "低风险",
            },
        }

        imported = permit_service.import_legacy_job(connection, job)
        self.assertEqual(imported["status"], "draft")
        self.assertEqual(imported["risk_level"], "重大")
        self.assertEqual(imported["residual_risk_level"], "低")
        self.assertEqual(imported["control_measures"], "面屏与耐酸手套")
        self.assertEqual(len(imported["jsa_items"]), 1)
        item = imported["jsa_items"][0]
        self.assertEqual(item["risk_score"], 20)
        self.assertEqual(item["risk_level"], "重大")
        self.assertEqual(item["residual_risk_score"], 4)
        self.assertEqual(item["residual_risk_level"], "低")
        self.assertEqual(item["confirmed_by"], "EHS审核人甲")

    def test_imported_ready_job_can_be_submitted(self) -> None:
        connection = db.open_database(":memory:")
        self.addCleanup(connection.close)
        job = create_job(
            job_id="JOB-ADAPTER-002",
            job_name="可提交作业",
            chemicals=[{"name": "氢氟酸"}],
            steps=[{"order": 1, "name": "配液"}],
            created_by="申请人甲",
        )
        job["public_evidence"] = [sample_public_item()]
        job["sds_evidence"] = [sample_sds_item()]
        job["jsa_confirmation"] = {
            "confirmed_by": "EHS审核人甲",
            "final": {
                "作业步骤": "配液",
                "危害因素": "HF",
                "可能后果": "灼伤",
                "可能性L": 3,
                "严重度S": 3,
                "现有控制措施": "排风",
                "建议控制措施": "PPE",
                "控制后可能性L": 2,
                "控制后严重度S": 2,
                "风险值R": 9,
                "风险等级": "中风险",
                "残余风险R": 4,
                "残余风险等级": "低风险",
            },
        }
        permit_service.import_legacy_job(connection, job)
        submitted = permit_service.submit_permit(
            connection, "JOB-ADAPTER-002", actor="申请人甲"
        )
        self.assertEqual(submitted["status"], "ehs_review")


class LegacyHazardAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "hazard_adapter.db")

    def test_hazard_status_mapping_round_trip(self) -> None:
        for legacy in ("待整改", "整改中", "已关闭"):
            canonical = hazard_state.V4_HAZARD_STATUS_TO_V5[legacy]
            self.assertEqual(
                hazard_state.V5_TO_V4_HAZARD_STATUS[canonical], legacy
            )
        self.assertEqual(legacy_adapter.hazard_status_to_v5("未知状态"), "")

    def test_demo_hazards_import_and_reverse_map(self) -> None:
        connection = db.open_database(self.path)
        self.addCleanup(connection.close)
        records = hazards.create_demo_hazard_records()
        record = records[0]

        mapped = legacy_adapter.legacy_hazard_to_v5(record)
        self.assertEqual(mapped["hazard_id"], record["隐患编号"])
        self.assertEqual(mapped["status"], "open")
        self.assertTrue(mapped["is_demo"])
        self.assertEqual(len(mapped["corrective_actions"]), 1)

        imported = hazard_service.import_legacy_hazard(connection, record)
        self.assertEqual(imported["id"], record["隐患编号"])
        self.assertEqual(imported["status"], "open")
        self.assertTrue(imported["is_demo"])
        self.assertEqual(imported["data_label"], record["数据性质"])
        self.assertEqual(len(imported["corrective_actions"]), 1)

        reverse = legacy_adapter.v5_hazard_to_legacy(imported)
        self.assertEqual(reverse["隐患编号"], record["隐患编号"])
        self.assertEqual(reverse["隐患描述"], record["隐患描述"])
        self.assertEqual(reverse["状态"], "待整改")
        self.assertEqual(reverse["数据性质"], record["数据性质"])

    def test_closed_legacy_hazard_keeps_verification_evidence(self) -> None:
        connection = db.open_database(":memory:")
        self.addCleanup(connection.close)
        record = hazards.create_hazard_record(
            hazard_id="HZ-LEGACY-900",
            description="关闭状态的历史隐患（模拟）",
            hazard_type="PPE",
            risk_level="高",
            owner="整改人甲",
            found_on="2026-09-01",
            due_on="2026-09-10",
            corrective_action="补齐PPE",
            status="已关闭",
            data_label=hazards.DEMO_DATA_LABEL,
            rectification_evidence=[
                {"file_name": "done.pdf", "evidence_type": "照片", "note": ""}
            ],
            reviewer="复查人甲",
            review_date="2026-09-09",
            review_note="现场复核通过",
            closed_by="复查人甲",
            closed_at="2026-09-09",
        )
        mapped = legacy_adapter.legacy_hazard_to_v5(record)
        self.assertEqual(mapped["status"], "closed")
        self.assertIsNotNone(mapped["verification"])
        self.assertEqual(mapped["verification"]["result"], "pass")

        imported = hazard_service.import_legacy_hazard(connection, record)
        self.assertEqual(imported["status"], "closed")
        self.assertEqual(imported["verification_result"], "pass")
        self.assertEqual(imported["verifier_id"], "复查人甲")
        self.assertEqual(len(imported["evidence"]), 1)

        reverse = legacy_adapter.v5_hazard_to_legacy(imported)
        self.assertEqual(reverse["状态"], "已关闭")
        self.assertEqual(reverse["reviewer"], "复查人甲")
        self.assertEqual(reverse["review_note"], "现场复核通过")

    def test_duplicate_imports_are_refused(self) -> None:
        connection = db.open_database(":memory:")
        self.addCleanup(connection.close)
        record = hazards.create_demo_hazard_records()[0]
        hazard_service.import_legacy_hazard(connection, record)
        with self.assertRaises(ValueError):
            hazard_service.import_legacy_hazard(connection, record)


if __name__ == "__main__":
    unittest.main()

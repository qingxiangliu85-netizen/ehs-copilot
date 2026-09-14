"""Phase 1 deterministic domain and persistence acceptance tests."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from streamlit.testing.v1 import AppTest

import db
import safety_review
from services import permit_service, persona_service, safety_review_service


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SafetyReviewDomainTests(unittest.TestCase):
    def test_work_type_and_compound_risk_precheck(self) -> None:
        result = safety_review.deterministic_precheck(
            {
                "title": "受限空间清洗",
                "description": "承包商进入罐内使用盐酸清洗",
                "work_steps": ["进入罐内清洗"],
                "chemicals": ["盐酸"],
                "contractor_involved": True,
            }
        )
        self.assertEqual(result["suggested_work_type"], "受限空间")
        self.assertIn("受限空间", result["suggested_risk_tags"])
        self.assertIn("化学品暴露", result["suggested_risk_tags"])
        self.assertIn("承包商作业", result["suggested_risk_tags"])

    def test_ai_jsa_candidate_never_contains_risk_numbers(self) -> None:
        item = safety_review.make_jsa_item(
            step_no=1, work_step="拆卸管线", hazard="化学品暴露"
        )
        for field in (
            "likelihood", "severity", "risk_score", "residual_likelihood",
            "residual_severity", "residual_risk_score"
        ):
            self.assertIsNone(item[field])

    def test_human_rating_uses_shared_calculator(self) -> None:
        rated = safety_review.apply_human_risk_rating(
            safety_review.make_jsa_item(
                step_no=1,
                work_step="拆卸管线",
                hazard="腐蚀/灼伤",
                proposed_controls="按适用SDS与SOP确认控制措施",
            ),
            likelihood=4,
            severity=5,
            residual_likelihood=2,
            residual_severity=3,
            confirmed_by="DEMO-EHS-01",
        )
        self.assertEqual((rated["risk_score"], rated["risk_level"]), (20, "重大风险"))
        self.assertEqual((rated["residual_risk_score"], rated["residual_risk_level"]), (6, "中风险"))

    def test_incomplete_citation_blocks_confirmation(self) -> None:
        draft = complete_draft()
        pack = complete_pack()
        pack["evidence"][0]["page"] = 0
        self.assertTrue(any("Citation不完整" in item for item in safety_review.pack_blockers(draft, pack)))

    def test_chemical_without_sds_blocks_confirmation(self) -> None:
        draft = complete_draft()
        pack = complete_pack()
        pack["evidence"] = []
        blockers = safety_review.pack_blockers(draft, pack)
        self.assertTrue(any("SDS" in item for item in blockers))


class SafetyReviewPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = db.open_database(":memory:")
        self.addCleanup(self.connection.close)
        persona_service.seed_demo_users(self.connection)
        self.applicant = persona_service.require_user(self.connection, "DEMO-APPLICANT-01")
        self.ehs = persona_service.require_user(self.connection, "DEMO-EHS-01")

    def _create(self) -> dict[str, object]:
        return safety_review_service.create_work_draft(
            self.connection, user=self.applicant, **complete_draft()
        )

    def test_pack_versions_increment_and_do_not_create_permit(self) -> None:
        draft = self._create()
        initial_permits = len(permit_service.list_permits(self.connection))
        first = safety_review_service.save_pack_version(
            self.connection, draft["id"], complete_pack(), user=self.ehs
        )
        second = safety_review_service.save_pack_version(
            self.connection, draft["id"], complete_pack(), user=self.ehs
        )
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(len(permit_service.list_permits(self.connection)), initial_permits)

    def test_unrated_jsa_cannot_be_confirmed(self) -> None:
        draft = self._create()
        pack = complete_pack()
        pack["jsa_draft"] = [
            safety_review.make_jsa_item(
                step_no=1, work_step="拆卸管线", hazard="腐蚀/灼伤"
            )
        ]
        safety_review_service.save_pack_version(
            self.connection, draft["id"], pack, user=self.ehs
        )
        with self.assertRaisesRegex(ValueError, "JSA"):
            safety_review_service.confirm_pack(
                self.connection, draft["id"], user=self.ehs
            )

    def test_complete_pack_can_be_confirmed_without_permit(self) -> None:
        draft = self._create()
        safety_review_service.save_pack_version(
            self.connection, draft["id"], complete_pack(), user=self.ehs
        )
        confirmed = safety_review_service.confirm_pack(
            self.connection, draft["id"], user=self.ehs
        )
        self.assertEqual(
            safety_review_service.require_work_draft(self.connection, draft["id"])["status"],
            safety_review.STATUS_CONFIRMED,
        )
        self.assertEqual(confirmed["confirmed_by"], "DEMO-EHS-01")
        self.assertEqual(len(permit_service.list_permits(self.connection)), 0)

    def test_applicant_cannot_confirm_pack(self) -> None:
        draft = self._create()
        safety_review_service.save_pack_version(
            self.connection, draft["id"], complete_pack(), user=self.ehs
        )
        with self.assertRaises(ValueError):
            safety_review_service.confirm_pack(
                self.connection, draft["id"], user=self.applicant
            )


class SafetyReviewUiSmokeTests(unittest.TestCase):
    def test_permit_page_opens_four_step_safety_review_workbench(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = db.EHS_DB_PATH
            db.EHS_DB_PATH = str(Path(directory) / "safety-review-ui.db")
            try:
                app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=300).run()
                app.selectbox(key="demo_user_id").set_value("DEMO-APPLICANT-01").run(timeout=300)
                app.radio(key="nav_page").set_value("作业许可").run(timeout=300)
                button = next(
                    item for item in app.button if item.label == "新建高风险作业"
                )
                button.click().run(timeout=300)
                self.assertFalse(app.exception)
                visible = "\n".join(
                    str(item.value)
                    for collection in (app.markdown, app.caption, app.subheader)
                    for item in collection
                )
                self.assertIn("新建高风险作业 · 安全准备", visible)
                self.assertIn("1. 填写真实作业", visible)
            finally:
                db.EHS_DB_PATH = original


def complete_draft() -> dict[str, object]:
    return {
        "title": "HF酸洗设备检维修（模拟）",
        "work_type": "危化品作业",
        "description": "设备停机后拆卸管路，涉及氢氟酸。",
        "location": "酸洗区（模拟）",
        "planned_start": "2030-01-01T08:00",
        "planned_end": "2030-01-01T12:00",
        "people_count": 2,
        "responsible_person": "现场负责人（Demo）",
        "contractor_involved": True,
        "work_steps": ["拆卸管线"],
        "chemicals": ["氢氟酸"],
        "user_risk_tags": ["化学品暴露"],
        "ai_risk_tags": ["腐蚀/灼伤"],
        "confirmed_risk_tags": ["化学品暴露", "腐蚀/灼伤"],
        "documents": [
            {
                "document_id": "HF-SDS-01",
                "source_type": "sds",
                "file_name": "HF_Synthetic_SDS.pdf",
                "version": "v1",
            }
        ],
    }


def complete_pack() -> dict[str, object]:
    evidence = {
        "evidence_id": "EV-HF-01",
        "source_type": "sds",
        "source_name": "HF_Synthetic_SDS.pdf",
        "version": "v1",
        "page": 2,
        "snippet": "HF synthetic demo PPE and emergency passage.",
        "chunk_id": "1",
        "locator": "",
    }
    item = safety_review.apply_human_risk_rating(
        safety_review.make_jsa_item(
            step_no=1,
            work_step="拆卸管线",
            hazard="腐蚀/灼伤",
            consequence="人员伤害",
            proposed_controls="按适用SDS与企业SOP确认后执行",
            evidence_refs=["EV-HF-01"],
        ),
        likelihood=4,
        severity=5,
        residual_likelihood=2,
        residual_severity=3,
        confirmed_by="DEMO-EHS-01",
    )
    return {
        "work_summary": "HF酸洗设备检维修（模拟）",
        "suggested_work_type": "危化品作业",
        "risk_findings": [safety_review.make_pack_item("腐蚀/灼伤", evidence_refs=["EV-HF-01"])],
        "evidence": [evidence],
        "jsa_draft": [item],
        "controls": [safety_review.make_pack_item("按原文核对控制要求", evidence_refs=["EV-HF-01"])],
        "ppe": [safety_review.make_pack_item("按原文核对PPE", evidence_refs=["EV-HF-01"])],
        "emergency_requirements": [safety_review.make_pack_item("按原文核对应急要求", evidence_refs=["EV-HF-01"])],
        "missing_items": [],
        "conflicts": [],
        "evidence_insufficient": False,
        "human_confirmations": ["EHS已确认"],
    }


if __name__ == "__main__":
    unittest.main()

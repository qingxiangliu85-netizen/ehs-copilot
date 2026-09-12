"""Tests for the V4 HF demo case data assets (P2)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from demo_cases import (
    HF_CASE_ID,
    HF_JOB_ID,
    HF_SDS_REGISTRY_PATH,
    HFCaseSourceError,
    create_demo_hf_case,
    hf_sds_source_ready,
    load_hf_sds_registry,
    require_hf_sds_source,
)
from hazards import DEMO_DATA_LABEL
from jobs import JOB_FIELDS, JOB_STATUS_DRAFT, get_job, job_summary


FIXED_TODAY = date(2026, 9, 12)
FIXED_NOW = datetime(2026, 9, 12, 10, 0, 0)


def make_case() -> dict[str, object]:
    return create_demo_hf_case(today=FIXED_TODAY, now=FIXED_NOW)


class HFCaseLoadTests(unittest.TestCase):
    def test_case_loads_with_the_required_sections(self) -> None:
        case = make_case()
        self.assertEqual(case["case_id"], HF_CASE_ID)
        for key in (
            "data_label",
            "job",
            "chemicals",
            "steps",
            "linked_hazards",
            "evidence_placeholders",
            "sds_source",
            "sds_evidence",
            "data_gaps",
        ):
            self.assertIn(key, case)
        self.assertEqual(case["data_label"], DEMO_DATA_LABEL)

    def test_job_record_has_the_full_field_shape(self) -> None:
        case = make_case()
        job = case["job"]
        self.assertEqual(set(JOB_FIELDS) - set(job), set())
        self.assertEqual(job["job_id"], HF_JOB_ID)
        self.assertEqual(job["status"], JOB_STATUS_DRAFT)
        self.assertIn("模拟", str(job["job_name"]))
        self.assertIn("模拟", str(job["area"]))
        self.assertIs(get_job([job], HF_JOB_ID), job)

    def test_chemicals_declare_hf_identity_and_wait_for_source(self) -> None:
        case = make_case()
        self.assertEqual(len(case["chemicals"]), 1)
        chemical = case["chemicals"][0]
        self.assertEqual(chemical["name"], "氢氟酸")
        self.assertIn("HF", chemical["aliases"])
        self.assertIn("hydrofluoric acid", chemical["aliases"])
        self.assertEqual(chemical["sds_file"], "")
        self.assertEqual(chemical["sds_status"], "pending_real_source")

    def test_steps_are_simulated_process_steps(self) -> None:
        case = make_case()
        steps = case["steps"]
        self.assertGreaterEqual(len(steps), 3)
        for index, step in enumerate(steps, start=1):
            self.assertEqual(step["order"], index)
            self.assertIn("模拟", str(step["note"]))

    def test_site_and_people_are_marked_simulated(self) -> None:
        case = make_case()
        job = case["job"]
        self.assertIn("模拟", str(job["area"]))
        self.assertIn("模拟", str(job["status_history"][0]["actor"]))


class HFHazardLinkTests(unittest.TestCase):
    def test_exactly_three_linked_hazards_are_created(self) -> None:
        case = make_case()
        self.assertEqual(len(case["linked_hazards"]), 3)

    def test_hazards_link_back_to_the_job_id(self) -> None:
        case = make_case()
        hazard_ids = [hazard["隐患编号"] for hazard in case["linked_hazards"]]
        self.assertEqual(case["job"]["linked_hazard_ids"], hazard_ids)
        for hazard in case["linked_hazards"]:
            self.assertEqual(hazard["related_job_id"], HF_JOB_ID)
            self.assertEqual(hazard["related_job_id"], case["job"]["job_id"])

    def test_every_hazard_has_owner_and_valid_due_date(self) -> None:
        case = make_case()
        for hazard in case["linked_hazards"]:
            self.assertTrue(str(hazard["责任人"]).strip())
            self.assertIn("模拟", str(hazard["责任人"]))
            self.assertGreaterEqual(
                date.fromisoformat(str(hazard["整改期限"])),
                date.fromisoformat(str(hazard["发现日期"])),
            )

    def test_job_summary_counts_the_linked_hazards(self) -> None:
        case = make_case()
        summary = job_summary([case["job"]])
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["active"], 1)
        self.assertEqual(summary["linked_hazard_total"], 3)


class DemoLabelTests(unittest.TestCase):
    def test_job_and_hazards_carry_the_demo_label(self) -> None:
        case = make_case()
        self.assertEqual(case["job"]["data_label"], DEMO_DATA_LABEL)
        for hazard in case["linked_hazards"]:
            self.assertEqual(hazard["数据性质"], DEMO_DATA_LABEL)

    def test_hazard_descriptions_are_explicitly_simulated(self) -> None:
        case = make_case()
        for hazard in case["linked_hazards"]:
            self.assertIn("模拟", str(hazard["隐患描述"]))

    def test_evidence_placeholders_are_pending_uploads(self) -> None:
        case = make_case()
        hazard_ids = {hazard["隐患编号"] for hazard in case["linked_hazards"]}
        placeholders = case["evidence_placeholders"]
        self.assertEqual(len(placeholders), len(hazard_ids))
        self.assertEqual(
            {item["hazard_id"] for item in placeholders}, hazard_ids
        )
        for item in placeholders:
            self.assertEqual(item["status"], "pending_upload")
            self.assertIn("模拟", str(item["note"]))

    def test_no_real_rectification_evidence_is_attached(self) -> None:
        case = make_case()
        for hazard in case["linked_hazards"]:
            self.assertFalse(hazard.get("rectification_evidence"))
            for field in ("reviewer", "review_date", "closed_by", "closed_at"):
                self.assertNotIn(field, hazard)
        self.assertEqual(case["job"]["attachments"], [])


class HFSourceGapTests(unittest.TestCase):
    def test_registry_reports_pending_real_source(self) -> None:
        self.assertTrue(HF_SDS_REGISTRY_PATH.is_file())
        registry = load_hf_sds_registry()
        self.assertEqual(registry["status"], "pending_real_source")
        self.assertEqual(registry["registered_demo_sds"]["file_name"], "")

    def test_candidate_sources_are_references_not_sds(self) -> None:
        registry = load_hf_sds_registry()
        candidates = registry["candidate_sources"]
        self.assertGreaterEqual(len(candidates), 3)
        for source in candidates:
            self.assertNotEqual(source["document_type"], "sds")
            self.assertFalse(source["usable_as_demo_sds"])
            self.assertTrue(str(source["url"]).startswith("https://"))
            self.assertTrue(
                str(source["legal_basis"]).strip(),
                msg="每个候选来源都必须写明版权/许可依据。",
            )

    def test_vendor_sds_are_recorded_as_rejected(self) -> None:
        registry = load_hf_sds_registry()
        rejected = registry["rejected_sources"]
        self.assertGreaterEqual(len(rejected), 1)
        for source in rejected:
            self.assertEqual(source["document_type"], "sds")
            reason = str(source["rejected_reason"])
            self.assertTrue("版权" in reason or "许可" in reason)

    def test_case_ships_no_sds_evidence_and_no_jsa_draft(self) -> None:
        case = make_case()
        self.assertEqual(case["sds_evidence"], [])
        self.assertEqual(case["job"]["sds_evidence"], [])
        self.assertIsNone(case["job"]["jsa_draft"])
        self.assertFalse(case["sds_source"]["ready"])
        self.assertEqual(case["sds_source"]["status"], "pending_real_source")
        self.assertTrue(case["data_gaps"])

    def test_require_refuses_to_continue_without_a_source(self) -> None:
        self.assertFalse(hf_sds_source_ready())
        with self.assertRaises(HFCaseSourceError):
            require_hf_sds_source()

    def test_missing_registry_falls_back_to_pending(self) -> None:
        missing = Path("definitely-missing-registry.json")
        registry = load_hf_sds_registry(missing)
        self.assertEqual(registry["status"], "pending_real_source")
        self.assertFalse(hf_sds_source_ready(missing))
        with self.assertRaises(HFCaseSourceError):
            require_hf_sds_source(missing)

    def test_ready_requires_an_actual_registered_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry_path = Path(directory) / "registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "registered_demo_sds": {"file_name": "HF_SDS.pdf"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertFalse(hf_sds_source_ready(registry_path))

            registered_file = Path(directory) / "HF_SDS.pdf"
            registered_file.write_bytes(b"%PDF-1.4 test placeholder")
            self.assertTrue(hf_sds_source_ready(registry_path))
            self.assertEqual(require_hf_sds_source(registry_path), registered_file)


if __name__ == "__main__":
    unittest.main(verbosity=2)

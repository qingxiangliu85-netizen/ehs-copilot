"""Tests for the HF public-source evidence pack and its separation rules."""

from __future__ import annotations

import unittest
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from demo_cases import (
    HF_EVIDENCE_PACK_PATH,
    HF_SDS_REGISTRY_PATH,
    create_demo_hf_case,
    hf_public_evidence,
    load_hf_evidence_pack,
    load_hf_sds_registry,
)
from hazards import DEMO_DATA_LABEL


FIXED_TODAY = date(2026, 9, 12)
FIXED_NOW = datetime(2026, 9, 12, 10, 0, 0)

REQUIRED_FIELDS = (
    "evidence_id",
    "source_title",
    "source_url",
    "organization",
    "section",
    "topic",
    "passage",
    "locator",
    "retrieved_at",
    "evidence_type",
    "usage_note",
)

ALLOWED_DOMAINS = {"www.cdc.gov", "www.osha.gov"}
ALLOWED_ORGANIZATIONS = {"NIOSH (US CDC)", "OSHA (US DOL)"}
ALLOWED_TOPICS = {
    "hazard_identification",
    "exposure_routes",
    "exposure_limits",
    "physical_properties",
    "storage_incompatibility",
    "ppe",
    "respiratory_protection",
    "first_aid",
    "firefighting",
    "spill_response",
    "decontamination",
}


def make_case() -> dict[str, object]:
    return create_demo_hf_case(today=FIXED_TODAY, now=FIXED_NOW)


class EvidencePackStructureTests(unittest.TestCase):
    def test_pack_loads_with_sources_items_and_gaps(self) -> None:
        self.assertTrue(HF_EVIDENCE_PACK_PATH.is_file())
        pack = load_hf_evidence_pack()
        self.assertEqual(pack["status"], "available")
        self.assertGreaterEqual(len(pack["sources"]), 3)
        self.assertGreaterEqual(len(pack["items"]), 10)
        self.assertTrue(pack["gaps"])

    def test_every_item_has_the_required_fields(self) -> None:
        for item in load_hf_evidence_pack()["items"]:
            with self.subTest(evidence_id=item.get("evidence_id")):
                for field in REQUIRED_FIELDS:
                    self.assertIn(field, item)
                    self.assertTrue(
                        str(item[field]).strip(), msg=f"{field} 不能为空。"
                    )
                self.assertGreater(len(str(item["passage"])), 20)

    def test_evidence_ids_are_unique(self) -> None:
        ids = [item["evidence_id"] for item in load_hf_evidence_pack()["items"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_sources_are_official_and_the_urls_are_allowed(self) -> None:
        pack = load_hf_evidence_pack()
        source_ids = {source["source_id"] for source in pack["sources"]}
        for item in pack["items"]:
            with self.subTest(evidence_id=item["evidence_id"]):
                self.assertIn(item["source_id"], source_ids)
                self.assertIn(item["organization"], ALLOWED_ORGANIZATIONS)
                host = urlparse(str(item["source_url"])).netloc
                self.assertIn(host, ALLOWED_DOMAINS)
                self.assertIn(item["topic"], ALLOWED_TOPICS)

    def test_retrieved_at_is_an_iso_date(self) -> None:
        for item in load_hf_evidence_pack()["items"]:
            with self.subTest(evidence_id=item["evidence_id"]):
                date.fromisoformat(str(item["retrieved_at"]))

    def test_first_aid_and_ppe_topics_are_covered(self) -> None:
        topics = [item["topic"] for item in load_hf_evidence_pack()["items"]]
        self.assertGreaterEqual(topics.count("first_aid"), 3)
        self.assertGreaterEqual(topics.count("ppe"), 2)
        self.assertGreaterEqual(topics.count("exposure_limits"), 2)

    def test_usage_notes_refuse_to_replace_the_sds(self) -> None:
        for item in load_hf_evidence_pack()["items"]:
            with self.subTest(evidence_id=item["evidence_id"]):
                note = str(item["usage_note"])
                self.assertIn("SDS", note)

    def test_missing_pack_falls_back_to_empty(self) -> None:
        missing = Path("definitely-missing-evidence-pack.json")
        pack = load_hf_evidence_pack(missing)
        self.assertEqual(pack["status"], "unavailable")
        self.assertEqual(pack["items"], [])
        self.assertEqual(hf_public_evidence(missing), [])


class EvidenceTrackSeparationTests(unittest.TestCase):
    def test_case_exposes_the_public_evidence_track(self) -> None:
        case = make_case()
        pack_items = load_hf_evidence_pack()["items"]
        public = case["public_evidence"]
        self.assertEqual(public["status"], "available")
        self.assertEqual(len(public["items"]), len(pack_items))
        self.assertIn("非 SDS", public["label"])
        self.assertIn("不替代 SDS", public["note"])

    def test_evidence_tracks_distinguish_public_sources_from_sds(self) -> None:
        case = make_case()
        tracks = {track["track"]: track for track in case["evidence_tracks"]}
        self.assertSetEqual(set(tracks), {"public_sources", "sds"})
        self.assertTrue(tracks["public_sources"]["ready"])
        self.assertEqual(
            tracks["public_sources"]["count"],
            len(case["public_evidence"]["items"]),
        )
        self.assertFalse(tracks["sds"]["ready"])
        self.assertEqual(tracks["sds"]["count"], 0)

    def test_sds_evidence_stays_empty_until_a_source_is_registered(self) -> None:
        case = make_case()
        self.assertEqual(case["sds_evidence"], [])
        self.assertEqual(case["job"]["sds_evidence"], [])
        self.assertIsNone(case["job"]["jsa_draft"])

    def test_public_evidence_is_not_marked_as_demo_data(self) -> None:
        for item in make_case()["public_evidence"]["items"]:
            with self.subTest(evidence_id=item["evidence_id"]):
                self.assertNotIn("数据性质", item)
                self.assertNotEqual(item.get("data_label"), DEMO_DATA_LABEL)

    def test_simulated_business_data_carries_no_public_source_fields(self) -> None:
        case = make_case()
        self.assertEqual(case["job"]["data_label"], DEMO_DATA_LABEL)
        for hazard in case["linked_hazards"]:
            with self.subTest(hazard_id=hazard["隐患编号"]):
                self.assertEqual(hazard["数据性质"], DEMO_DATA_LABEL)
                for field in ("source_url", "organization", "retrieved_at"):
                    self.assertNotIn(field, hazard)

    def test_hf_pdfs_must_be_registered_before_use(self) -> None:
        registry = load_hf_sds_registry()
        registered = str(
            (registry.get("registered_demo_sds") or {}).get("file_name", "")
        )
        for path in HF_SDS_REGISTRY_PATH.parent.glob("*HF*.pdf"):
            self.assertEqual(
                path.name,
                registered,
                "存在未登记来源的 HF PDF；禁止使用或提交未核验的 SDS。",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)

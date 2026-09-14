"""Dedicated Safety Review graph tests without API or embedding downloads."""

from __future__ import annotations

import unittest

from langchain_core.documents import Document

import evidence_adapter
import safety_review
from workflow.safety_review_graph import SafetyReviewContext, run_safety_review


class _Index:
    ntotal = 4


class _SDSStore:
    index = _Index()

    def similarity_search_with_score(self, query: str, k: int = 4):
        section = "8" if "PPE" in query else "4,5,6" if "急救" in query else "2"
        doc = Document(
            page_content=f"Hydrogen fluoride HF synthetic demo evidence for {query}",
            metadata={
                "source": "HF_Synthetic_SDS.pdf",
                "page": 2,
                "sections": section,
                "product_aliases": "hf|氢氟酸|hydrogenfluoride",
                "version": "v1",
            },
        )
        return [(doc, 0.1)]


class _GenericStore:
    def similarity_search_with_score(self, query: str, k: int = 4):
        return [
            (
                Document(
                    page_content="作业前隔离设备并由现场负责人确认",
                    metadata={"source": "HF酸洗SOP.pdf", "page": 3, "version": "A"},
                ),
                0.2,
            )
        ]


def draft() -> dict[str, object]:
    return {
        "id": "WD-0001",
        "title": "HF酸洗设备检维修（模拟）",
        "work_type": "危化品作业",
        "description": "停机后拆卸管路，涉及氢氟酸",
        "location": "酸洗区（模拟）",
        "planned_start": "2030-01-01T08:00",
        "planned_end": "2030-01-01T12:00",
        "people_count": 2,
        "responsible_person": "现场负责人（Demo）",
        "contractor_involved": True,
        "work_steps": ["拆卸管线"],
        "chemicals": ["氢氟酸"],
        "user_risk_tags": ["化学品暴露"],
        "confirmed_risk_tags": ["化学品暴露", "腐蚀/灼伤"],
    }


class SafetyReviewGraphTests(unittest.TestCase):
    def test_no_key_path_builds_pack_and_pauses_for_human(self) -> None:
        result = run_safety_review(
            draft(),
            context=SafetyReviewContext(
                stores=evidence_adapter.EvidenceStores(
                    sds=_SDSStore(), sop=_GenericStore()
                ),
                use_llm=False,
            ),
            thread_id="test-success",
        )
        self.assertIn("pack", result)
        self.assertEqual(result["mode"], "rules_retrieval")
        self.assertTrue(result["pack"]["evidence"])
        self.assertIn("__interrupt__", result)
        for item in result["pack"]["jsa_draft"]:
            self.assertIsNone(item["likelihood"])
            self.assertIsNone(item["severity"])

    def test_mismatched_sds_is_evidence_insufficient(self) -> None:
        class WrongStore(_SDSStore):
            def similarity_search_with_score(self, query: str, k: int = 4):
                doc = Document(
                    page_content="Acetone synthetic SDS evidence",
                    metadata={
                        "source": "Acetone_SDS.pdf",
                        "page": 2,
                        "sections": "2,4,5,6,8",
                        "product_aliases": "acetone|丙酮",
                    },
                )
                return [(doc, 0.1)]

        result = run_safety_review(
            draft(),
            context=SafetyReviewContext(
                stores=evidence_adapter.EvidenceStores(sds=WrongStore()),
                use_llm=False,
            ),
            thread_id="test-mismatch",
        )
        self.assertTrue(result["pack"]["evidence_insufficient"])
        self.assertTrue(any("适用SDS" in item for item in result["blockers"]))
        self.assertIn("__interrupt__", result)

    def test_missing_intake_is_detected(self) -> None:
        incomplete = dict(draft())
        incomplete["location"] = ""
        result = run_safety_review(
            incomplete,
            context=SafetyReviewContext(use_llm=False),
            thread_id="test-missing",
        )
        self.assertTrue(any("作业地点" in item for item in result["blockers"]))


if __name__ == "__main__":
    unittest.main()

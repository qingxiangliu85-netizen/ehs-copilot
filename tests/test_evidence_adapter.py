"""Evidence track, Citation and chemical-match tests for Phase 1."""

from __future__ import annotations

import unittest

from langchain_core.documents import Document

import evidence_adapter


class _Index:
    ntotal = 1


class _Store:
    index = _Index()

    def __init__(self, document: Document) -> None:
        self.document = document

    def similarity_search_with_score(self, query: str, k: int = 4):
        return [(self.document, 0.1)]


class EvidenceAdapterTests(unittest.TestCase):
    def test_tracks_are_retrieved_separately(self) -> None:
        sds = Document(
            page_content="SECTION 8 PPE for hydrogen fluoride HF",
            metadata={
                "source": "HF_Synthetic_SDS.pdf",
                "page": 2,
                "sections": "8",
                "product_aliases": "hf|氢氟酸|hydrogenfluoride",
                "version": "v1",
            },
        )
        sop = Document(
            page_content="作业前执行设备隔离并由现场负责人确认",
            metadata={"source": "HF酸洗SOP.pdf", "page": 3, "version": "A"},
        )
        result = evidence_adapter.retrieve_evidence(
            evidence_adapter.EvidenceStores(sds=_Store(sds), sop=_Store(sop)),
            query="HF作业需要哪些PPE",
        )
        self.assertEqual(result["sds"][0]["source_type"], "sds")
        self.assertEqual(result["sop"][0]["source_type"], "sop")
        self.assertEqual(result["sds"][0]["page"], 2)
        self.assertEqual(result["sop"][0]["page"], 3)

    def test_hf_match_accepts_name_or_alias_and_rejects_other_sds(self) -> None:
        matching = {
            "source_name": "Hydrogen_Fluoride_SDS.pdf",
            "snippet": "Hydrogen fluoride HF",
        }
        wrong = {"source_name": "Acetone_SDS.pdf", "snippet": "acetone"}
        self.assertTrue(evidence_adapter.citation_matches_chemical(matching, "氢氟酸"))
        self.assertFalse(evidence_adapter.citation_matches_chemical(wrong, "氢氟酸"))

    def test_multiple_unselected_sds_versions_are_a_conflict(self) -> None:
        conflicts = evidence_adapter.version_conflicts(
            [
                {"source_type": "sds", "chemical_name": "HF", "version": "v1", "document_id": "1"},
                {"source_type": "sds", "chemical_name": "HF", "version": "v2", "document_id": "2"},
            ]
        )
        self.assertEqual(len(conflicts), 1)
        self.assertFalse(conflicts[0]["resolved"])


if __name__ == "__main__":
    unittest.main()

"""Repeatable v0.1 SDS retrieval tests using clearly simulated documents.

The PDFs generated here are synthetic QA fixtures, not real safety documents and
must never be used for operational decisions.  Their deliberately distinct
wording makes source/page mix-ups observable without hard-coding application
answers.
"""

from __future__ import annotations

import io
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from rag import (
    NOT_FOUND_MESSAGE,
    answer_question,
    build_knowledge_base,
    retrieve_documents,
)


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_text_pdf(pages: list[str]) -> bytes:
    """Create a tiny text PDF without adding a test-only runtime dependency."""
    objects: list[bytes] = []

    def add_object(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    content_ids: list[int] = []
    pages_id = 2 + len(pages) * 2
    catalog_id = pages_id + 1

    for page_text in pages:
        commands = ["BT", "/F1 10 Tf", "45 790 Td", "13 TL"]
        for line in page_text.splitlines():
            commands.append(f"({_escape_pdf_text(line)}) Tj")
            commands.append("T*")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1")
        content_id = add_object(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )
        page_id = add_object(
            (
                f"<< /Type /Page /Parent {pages_id} 0 R "
                f"/MediaBox [0 0 595 842] /Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
        content_ids.append(content_id)
        page_ids.append(page_id)

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    add_object(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii"))
    add_object(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"))

    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets = [0]
    for object_id, body in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{object_id} 0 obj\n".encode("ascii"))
        output.write(body)
        output.write(b"\nendobj\n")
    xref_offset = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.write(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return output.getvalue()


class MemoryUpload(io.BytesIO):
    def __init__(self, name: str, payload: bytes):
        super().__init__(payload)
        self.name = name

    def getvalue(self) -> bytes:
        return super().getvalue()


HF_PAGES = [
    "SECTION 1 IDENTIFICATION\nProduct name: Hydrofluoric Acid (HF)\nSECTION 2 HAZARDS\nCorrosive and toxic. Causes severe skin burns and eye damage. Toxic if inhaled.",
    "SECTION 8 EXPOSURE CONTROLS AND PPE\nHF-UNIQUE-PPE: Wear chemical splash goggles, face shield, acid-resistant gloves and protective clothing.",
    "SECTION 7 STORAGE\nHF-UNIQUE-STORAGE: Store locked up in a cool, well-ventilated acid cabinet. Keep container tightly closed and away from incompatible bases.",
    "SECTION 4 FIRST AID\nHF-UNIQUE-SKIN: Immediately remove contaminated clothing and rinse skin with water. Obtain immediate medical attention.\nHF-UNIQUE-EYE: Rinse cautiously with water for at least 15 minutes and obtain immediate medical attention.",
    "SECTION 4 FIRST AID - INHALATION\nHF-UNIQUE-INHALATION: Move the person to fresh air, keep at rest and obtain immediate medical attention.",
    "SECTION 6 ACCIDENTAL RELEASE\nHF-UNIQUE-SPILL: Evacuate the area, ventilate, wear specified PPE and collect with compatible inert absorbent. Do not allow entry into drains.",
    "SECTION 5 FIRE FIGHTING\nHF-UNIQUE-FIRE: Use extinguishing media suitable for the surrounding fire. Firefighters must wear positive-pressure self-contained breathing apparatus.",
]

HCL_PAGES = [
    "SECTION 1 IDENTIFICATION\nProduct name: Hydrochloric Acid (HCl)\nSECTION 2 HAZARDS\nCorrosive. Causes severe skin burns and eye damage. May cause respiratory irritation.",
    "SECTION 8 EXPOSURE CONTROLS AND PPE\nHCL-UNIQUE-PPE: Wear splash goggles, chemical-resistant gloves, protective apron and suitable respiratory protection when ventilation is inadequate.",
    "SECTION 7 STORAGE\nHCL-UNIQUE-STORAGE: Store in a corrosion-resistant container in a ventilated corrosives cabinet, separated from alkalis and oxidizers.",
    "SECTION 4 FIRST AID\nHCL-UNIQUE-SKIN: Remove contaminated clothing and flush skin with plenty of water for at least 15 minutes. Seek medical advice.\nHCL-UNIQUE-EYE: Rinse cautiously with water for at least 20 minutes and seek immediate medical attention.",
    "SECTION 4 FIRST AID - INHALATION\nHCL-UNIQUE-INHALATION: Remove to fresh air and keep comfortable for breathing. Get medical attention if symptoms persist.",
    "SECTION 6 ACCIDENTAL RELEASE\nHCL-UNIQUE-SPILL: Restrict access, provide ventilation, wear protective equipment and absorb with non-combustible compatible material. Prevent discharge to drains.",
    "SECTION 5 FIRE FIGHTING\nHCL-UNIQUE-FIRE: The product is not combustible. Use media appropriate for the surrounding fire and wear self-contained breathing apparatus.",
]

ACETONE_PAGES = [
    "SECTION 1 IDENTIFICATION\nProduct name: Acetone\nSECTION 2 HAZARDS\nHighly flammable liquid and vapor. Causes serious eye irritation. May cause drowsiness or dizziness.",
    "SECTION 8 EXPOSURE CONTROLS AND PPE\nACETONE-UNIQUE-PPE: Wear safety goggles and solvent-resistant gloves. Use local exhaust ventilation.",
    "SECTION 7 STORAGE\nACETONE-UNIQUE-STORAGE: Store in a cool, well-ventilated flammables cabinet away from heat, sparks, open flames and oxidizers. Ground containers during transfer.",
    "SECTION 4 FIRST AID\nACETONE-UNIQUE-SKIN: Remove contaminated clothing and wash skin with soap and water.\nACETONE-UNIQUE-EYE: Rinse cautiously with water for at least 15 minutes and remove contact lenses if easy to do.",
    "SECTION 4 FIRST AID - INHALATION\nACETONE-UNIQUE-INHALATION: Move to fresh air and keep at rest. Seek medical attention if symptoms continue.",
    "SECTION 6 ACCIDENTAL RELEASE\nACETONE-UNIQUE-SPILL: Eliminate ignition sources, ventilate, use non-sparking tools and absorb with inert material. Prevent entry into drains.",
    "SECTION 5 FIRE FIGHTING\nACETONE-UNIQUE-FIRE: Use alcohol-resistant foam, dry chemical or carbon dioxide. Vapors may form explosive mixtures with air.",
]


@dataclass(frozen=True)
class Scenario:
    category: str
    question: str
    sources: frozenset[str]
    pages: frozenset[int]
    marker: str | None = None
    not_found: bool = False


SCENARIOS = [
    Scenario("危险性-中文", "HF 有哪些主要危险性？", frozenset({"HF_SIMULATED_SDS.pdf"}), frozenset({1}), "severe skin burns"),
    Scenario("危险性-英文", "What are the main hazards of hydrochloric acid HCl?", frozenset({"HCl_SIMULATED_SDS.pdf"}), frozenset({1}), "respiratory irritation"),
    Scenario("危险性-英文", "What hazards are listed for acetone?", frozenset({"Acetone_SIMULATED_SDS.pdf"}), frozenset({1}), "Highly flammable"),
    Scenario("PPE-中文", "操作 HF 应佩戴什么 PPE？", frozenset({"HF_SIMULATED_SDS.pdf"}), frozenset({2}), "HF-UNIQUE-PPE"),
    Scenario("PPE-英文", "What PPE is required for HCl?", frozenset({"HCl_SIMULATED_SDS.pdf"}), frozenset({2}), "HCL-UNIQUE-PPE"),
    Scenario("PPE-英文", "What personal protective equipment is specified for acetone?", frozenset({"Acetone_SIMULATED_SDS.pdf"}), frozenset({2}), "ACETONE-UNIQUE-PPE"),
    Scenario("储存-中文", "HF 应如何储存？", frozenset({"HF_SIMULATED_SDS.pdf"}), frozenset({3}), "HF-UNIQUE-STORAGE"),
    Scenario("储存-英文", "How should hydrochloric acid HCl be stored?", frozenset({"HCl_SIMULATED_SDS.pdf"}), frozenset({3}), "HCL-UNIQUE-STORAGE"),
    Scenario("储存-中文", "丙酮 Acetone 的储存要求是什么？", frozenset({"Acetone_SIMULATED_SDS.pdf"}), frozenset({3}), "ACETONE-UNIQUE-STORAGE"),
    Scenario("皮肤接触急救-中文", "皮肤接触 HF 后如何急救？", frozenset({"HF_SIMULATED_SDS.pdf"}), frozenset({4}), "HF-UNIQUE-SKIN"),
    Scenario("皮肤接触急救-英文", "What first aid is specified for acetone skin contact?", frozenset({"Acetone_SIMULATED_SDS.pdf"}), frozenset({4}), "ACETONE-UNIQUE-SKIN"),
    Scenario("眼睛接触急救-中文", "HCl 进入眼睛后怎么办？", frozenset({"HCl_SIMULATED_SDS.pdf"}), frozenset({4}), "HCL-UNIQUE-EYE"),
    Scenario("眼睛接触急救-英文", "What is the eye-contact first aid for HF?", frozenset({"HF_SIMULATED_SDS.pdf"}), frozenset({4}), "HF-UNIQUE-EYE"),
    Scenario("吸入急救-中文", "吸入 HF 后应采取什么措施？", frozenset({"HF_SIMULATED_SDS.pdf"}), frozenset({5}), "HF-UNIQUE-INHALATION"),
    Scenario("吸入急救-英文", "What should be done after inhaling acetone vapor?", frozenset({"Acetone_SIMULATED_SDS.pdf"}), frozenset({5}), "ACETONE-UNIQUE-INHALATION"),
    Scenario("泄漏处置-中文", "HCl 泄漏后应该如何处置？", frozenset({"HCl_SIMULATED_SDS.pdf"}), frozenset({6}), "HCL-UNIQUE-SPILL"),
    Scenario("泄漏处置-英文", "How should an HF spill be handled?", frozenset({"HF_SIMULATED_SDS.pdf"}), frozenset({6}), "HF-UNIQUE-SPILL"),
    Scenario("消防措施-中文", "丙酮 Acetone 着火时使用什么灭火介质？", frozenset({"Acetone_SIMULATED_SDS.pdf"}), frozenset({7}), "ACETONE-UNIQUE-FIRE"),
    Scenario("消防措施-英文", "What firefighting measures are stated for HF?", frozenset({"HF_SIMULATED_SDS.pdf"}), frozenset({7}), "HF-UNIQUE-FIRE"),
    Scenario("多文档查询-英文", "Compare the storage requirements for HF and HCl.", frozenset({"HF_SIMULATED_SDS.pdf", "HCl_SIMULATED_SDS.pdf"}), frozenset({3}), "UNIQUE-STORAGE"),
    Scenario("多文档查询-中文", "比较 HF 和 Acetone 的消防措施。", frozenset({"HF_SIMULATED_SDS.pdf", "Acetone_SIMULATED_SDS.pdf"}), frozenset({7}), "UNIQUE-FIRE"),
    Scenario("文档隔离-英文", "What PPE is required for acetone?", frozenset({"Acetone_SIMULATED_SDS.pdf"}), frozenset({2}), "ACETONE-UNIQUE-PPE"),
    Scenario("不存在信息-中文", "HF 的慢性生殖毒性数值是多少？", frozenset(), frozenset(), not_found=True),
    Scenario("不存在信息-英文", "What exact aquatic LC50 value is listed for HCl?", frozenset(), frozenset(), not_found=True),
    Scenario("不存在信息-英文", "What is the UN transport number for acetone?", frozenset(), frozenset(), not_found=True),
    Scenario("不存在信息-通用", "Who is the chief executive officer CEO of this product?", frozenset(), frozenset(), not_found=True),
]


class SDSSScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        uploads = [
            MemoryUpload("HF_SIMULATED_SDS.pdf", make_text_pdf(HF_PAGES)),
            MemoryUpload("HCl_SIMULATED_SDS.pdf", make_text_pdf(HCL_PAGES)),
            MemoryUpload("Acetone_SIMULATED_SDS.pdf", make_text_pdf(ACETONE_PAGES)),
        ]
        cls.kb = build_knowledge_base(uploads)

    def test_pdf_and_index_counts(self) -> None:
        self.assertEqual(self.kb.page_count, 21)
        self.assertEqual(self.kb.chunk_count, 21)
        self.assertEqual(len(self.kb.file_names), 3)

    def test_all_scenarios(self) -> None:
        failures: list[str] = []
        for number, scenario in enumerate(SCENARIOS, start=1):
            documents = retrieve_documents(self.kb.vector_store, scenario.question)
            actual_sources = {str(doc.metadata["source"]) for doc in documents}
            actual_pages = {int(doc.metadata["page"]) for doc in documents}
            context = "\n".join(doc.page_content for doc in documents)

            if scenario.not_found:
                if documents:
                    failures.append(
                        f"#{number} {scenario.category}: expected not-found, got "
                        f"{sorted(zip(actual_sources, actual_pages))}"
                    )
                with patch("rag.generate_response") as mocked_llm:
                    result = answer_question(self.kb.vector_store, scenario.question)
                if result.answer != NOT_FOUND_MESSAGE or result.sources:
                    failures.append(f"#{number} {scenario.category}: refusal contract failed")
                if mocked_llm.called:
                    failures.append(
                        f"#{number} {scenario.category}: LLM was called for unsupported query"
                    )
                continue

            if actual_sources != set(scenario.sources):
                failures.append(
                    f"#{number} {scenario.category}: sources {sorted(actual_sources)} "
                    f"!= {sorted(scenario.sources)}"
                )
            if not scenario.pages.issubset(actual_pages):
                failures.append(
                    f"#{number} {scenario.category}: pages {sorted(actual_pages)} "
                    f"do not include {sorted(scenario.pages)}"
                )
            if scenario.marker and scenario.marker not in context:
                failures.append(f"#{number} {scenario.category}: marker not retrieved")

        self.assertFalse(failures, "\n" + "\n".join(failures))

    def test_grounded_answer_context_and_citations(self) -> None:
        """Verify the LLM receives only scoped evidence and citations match it."""
        for number, scenario in enumerate(SCENARIOS, start=1):
            if scenario.not_found:
                continue
            with self.subTest(number=number, category=scenario.category):
                captured_prompt = ""

                def grounded_stub(prompt: str) -> str:
                    nonlocal captured_prompt
                    captured_prompt = prompt
                    return "SIMULATED GROUNDED ANSWER"

                with patch("rag.generate_response", side_effect=grounded_stub):
                    result = answer_question(self.kb.vector_store, scenario.question)

                self.assertEqual(result.answer, "SIMULATED GROUNDED ANSWER")
                self.assertEqual(
                    {source for source, _page in result.sources},
                    set(scenario.sources),
                )
                self.assertTrue(scenario.pages.issubset({page for _source, page in result.sources}))
                self.assertIn(scenario.question, captured_prompt)
                self.assertIn("请仅依据下方 SDS 检索上下文回答问题", captured_prompt)
                for source in scenario.sources:
                    self.assertIn(f"Source: {source}", captured_prompt)
                unrelated_sources = {
                    "HF_SIMULATED_SDS.pdf",
                    "HCl_SIMULATED_SDS.pdf",
                    "Acetone_SIMULATED_SDS.pdf",
                } - set(scenario.sources)
                for source in unrelated_sources:
                    self.assertNotIn(f"Source: {source}", captured_prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)

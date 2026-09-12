"""End-to-end UI test for the repository-owned Synthetic Demo SDS."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader
from streamlit.testing.v1 import AppTest

from rag import build_knowledge_base, retrieve_documents


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_PDF = PROJECT_ROOT / "data" / "demo_sds" / "EHS_Copilot_Demo_Synthetic_SDS.pdf"
DEMO_NOTICE = (
    "Synthetic SDS for demonstration only - not for real-world safety decisions."
)


class DemoUpload:
    name = DEMO_PDF.name

    def getvalue(self) -> bytes:
        return DEMO_PDF.read_bytes()


class DemoExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.knowledge_base = build_knowledge_base([DemoUpload()])

    def test_demo_pdf_is_clearly_marked(self) -> None:
        reader = PdfReader(DEMO_PDF)
        self.assertEqual(len(reader.pages), 6)
        page_texts = [page.extract_text() or "" for page in reader.pages]
        self.assertTrue(all(DEMO_NOTICE in text for text in page_texts))
        for marker in (
            "DEMO-HAZARDS",
            "DEMO-PPE",
            "DEMO-STORAGE",
            "DEMO-SKIN-FIRST-AID",
            "DEMO-SPILL",
            "DEMO-FIRE",
        ):
            self.assertTrue(any(marker in text for text in page_texts), marker)

    def test_all_six_example_topics_retrieve_expected_demo_page(self) -> None:
        scenarios = (
            ("该化学品有哪些主要危险性？", 1, "DEMO-HAZARDS"),
            ("操作时需要佩戴哪些 PPE？", 2, "DEMO-PPE"),
            ("发生皮肤接触后如何处理？", 4, "DEMO-SKIN-FIRST-AID"),
            ("该化学品应该如何储存？", 3, "DEMO-STORAGE"),
            ("泄漏时 SDS 建议采取什么措施？", 5, "DEMO-SPILL"),
            ("发生火灾时应使用什么灭火介质？", 6, "DEMO-FIRE"),
        )
        for question, expected_page, marker in scenarios:
            with self.subTest(question=question):
                documents = retrieve_documents(
                    self.knowledge_base.vector_store,
                    question,
                )
                self.assertEqual(
                    {document.metadata["source"] for document in documents},
                    {DEMO_PDF.name},
                )
                self.assertIn(expected_page, {document.metadata["page"] for document in documents})
                self.assertIn(marker, "\n".join(document.page_content for document in documents))

    def test_no_key_retrieval_mode_and_single_example_submission(self) -> None:
        with (
            patch("llm.OPENAI_API_KEY", ""),
            patch("rag.generate_response") as mocked_llm,
        ):
            app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=240).run()

            # P4B: SDS 检索页现在是一级导航项「SDS资料库」。
            app.radio[0].set_value("SDS资料库").run(timeout=240)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.session_state["knowledge_base_mode"], "demo")
            self.assertEqual(
                app.session_state["loaded_files"],
                ["EHS_Copilot_Demo_Synthetic_SDS.pdf"],
            )
            self.assertEqual(app.session_state["page_count"], 6)
            self.assertGreaterEqual(app.session_state["chunk_count"], 6)
            self.assertTrue(
                any("Demo 知识库已就绪" in item.value for item in app.success)
            )

            example_button = next(
                button
                for button in app.button
                if button.label == "操作需要哪些 PPE？"
            )
            self.assertFalse(example_button.disabled)
            example_button.click().run(timeout=240)

            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(app.error), 0)
            mocked_llm.assert_not_called()
            self.assertEqual(app.session_state["messages"][0]["role"], "user")
            self.assertEqual(
                app.session_state["messages"][0]["content"],
                "操作需要哪些 PPE？",
            )
            assistant_message = app.session_state["messages"][1]
            self.assertEqual(assistant_message["role"], "assistant")
            self.assertEqual(assistant_message["response_mode"], "retrieval")
            self.assertIn("个体防护", assistant_message["content"])
            self.assertEqual(
                assistant_message["sources"],
                (("EHS_Copilot_Demo_Synthetic_SDS.pdf", 2),),
            )
            self.assertIn("DEMO-PPE-ZH", assistant_message["evidence"][0]["snippet"])

            markdown = "\n".join(item.value for item in app.markdown)
            self.assertIn("查询结果", markdown)
            self.assertIn("最相关信息 / 摘要", markdown)
            self.assertIn("证据来源", markdown)
            self.assertIn("EHS_Copilot_Demo_Synthetic_SDS.pdf · 第 2 页", markdown)

            app.run(timeout=90)
            self.assertEqual(len(app.exception), 0)
            user_messages = [
                item
                for item in app.session_state["messages"]
                if item["role"] == "user"
            ]
            self.assertEqual(len(user_messages), 1)

            app.chat_input[0].set_value("该化学品的联合国编号是多少？").run(timeout=90)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(app.error), 0)
            mocked_llm.assert_not_called()
            self.assertEqual(
                app.session_state["messages"][-1]["content"],
                "当前 SDS 知识库中未找到相关信息。",
            )
            self.assertEqual(app.session_state["messages"][-1]["evidence"], [])

    def test_api_key_keeps_ai_answer_path(self) -> None:
        def grounded_demo_llm(prompt: str) -> str:
            self.assertIn("DEMO-HAZARDS", prompt)
            self.assertIn(DEMO_NOTICE, prompt)
            return "The Synthetic Demo SDS identifies flammability and irritation hazards."

        with (
            patch("llm.OPENAI_API_KEY", "test-api-key"),
            patch("rag.generate_response", side_effect=grounded_demo_llm) as mocked_llm,
        ):
            app = AppTest.from_file(PROJECT_ROOT / "app.py", default_timeout=240).run()
            # P4B: SDS 检索页现在是一级导航项「SDS资料库」。
            app.radio[0].set_value("SDS资料库").run(timeout=240)
            next(
                button
                for button in app.button
                if button.label == "主要危险性是什么？"
            ).click().run(timeout=240)

            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(app.error), 0)
            mocked_llm.assert_called_once()
            assistant_messages = [
                item
                for item in app.session_state["messages"]
                if item["role"] == "assistant"
            ]
            self.assertEqual(len(assistant_messages), 1)
            self.assertEqual(assistant_messages[0]["response_mode"], "ai")


if __name__ == "__main__":
    unittest.main(verbosity=2)

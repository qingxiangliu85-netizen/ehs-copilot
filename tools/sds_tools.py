"""SDS retrieval tool.

Thin wrapper over :mod:`rag`.  All parsing, chunking, filtering and grounded
answering stay in :mod:`rag`; this module only adapts the result into the
JSON-shaped payload the workflow exposes.

The payload carries three extra signals the guardrails rely on:

``sources``
    ``[file, page]`` pairs, exactly as phase 1 produced them.
``evidence``
    ranked rows with file, page, section and the original snippet.
``aliases``
    the product aliases of the retrieved documents, used to detect a chemical
    that is not covered by the loaded SDS.
"""

from __future__ import annotations

from config import NOT_FOUND_MESSAGE, TOP_K
from llm import is_llm_configured
from rag import answer_question, collect_sources, retrieve_documents

from .context import ToolContext


TOOL_NAME = "search_sds"
_SNIPPET_LIMIT = 320
_ANSWER_LIMIT = 360


def _condense(text: str, limit: int) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) > limit:
        return f"{collapsed[:limit].rstrip()}…"
    return collapsed


def _document_aliases(document: object) -> tuple[str, ...]:
    """Return the normalised product aliases recorded on one document."""
    return tuple(
        value
        for value in str(document.metadata.get("product_aliases", "")).split("|")
        if value
    )


def collect_aliases(documents: tuple[object, ...]) -> list[str]:
    """Return the de-duplicated product aliases of the retrieved documents."""
    aliases: list[str] = []
    for document in documents:
        for alias in _document_aliases(document):
            if alias not in aliases:
                aliases.append(alias)
    return aliases


def build_evidence(documents: tuple[object, ...]) -> list[dict[str, object]]:
    """Return de-duplicated source/page snippets for the retrieved documents."""
    evidence: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for document in documents:
        source = str(document.metadata.get("source", "未知文件"))
        page = int(document.metadata.get("page", 0))
        if (source, page) in seen:
            continue
        seen.add((source, page))
        evidence.append(
            {
                "rank": len(evidence) + 1,
                "source": source,
                "page": page,
                "sections": str(document.metadata.get("sections", "")),
                "snippet": _condense(document.page_content, _SNIPPET_LIMIT),
            }
        )
    return evidence


def extractive_answer(documents: tuple[object, ...]) -> str:
    """Return a compact passage from the top hit when no LLM is configured."""
    if not documents:
        return NOT_FOUND_MESSAGE
    return _condense(documents[0].page_content, _ANSWER_LIMIT)


def search_sds(
    context: ToolContext,
    question: str,
    top_k: int | None = None,
) -> dict[str, object]:
    """Answer an SDS question strictly from the knowledge base of this session."""
    cleaned = str(question or "").strip()
    if not cleaned:
        return {
            "tool": TOOL_NAME,
            "status": "error",
            "message": "问题不能为空。",
            "question": "",
            "answer": "",
            "sources": [],
            "evidence": [],
            "aliases": [],
        }

    if not context.sds_ready:
        return {
            "tool": TOOL_NAME,
            "status": "blocked",
            "message": "尚未构建 SDS 知识库，请先在侧边栏上传 PDF 并构建知识库。",
            "question": cleaned,
            "answer": NOT_FOUND_MESSAGE,
            "sources": [],
            "evidence": [],
            "aliases": [],
        }

    documents = tuple(
        retrieve_documents(context.vector_store, cleaned, top_k or TOP_K)
    )
    sources = collect_sources(documents)
    mode = "retrieval"
    answer = extractive_answer(documents)

    # Reuse the existing grounded-answer path when an API key is configured.
    if documents and is_llm_configured():
        try:
            result = answer_question(context.vector_store, cleaned)
            answer = result.answer
            sources = result.sources
            mode = "ai"
        except Exception as exc:  # noqa: BLE001 - degrade instead of failing the run
            answer = f"{extractive_answer(documents)}（AI 摘要不可用：{exc}）"

    return {
        "tool": TOOL_NAME,
        "status": "ok",
        "question": cleaned,
        "answer": answer,
        "mode": mode,
        "retrieved_count": len(documents),
        "sources": [list(item) for item in sources],
        "evidence": build_evidence(documents),
        "aliases": collect_aliases(documents),
    }

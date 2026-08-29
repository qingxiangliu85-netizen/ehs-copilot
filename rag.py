"""PDF parsing, chunking, embedding, FAISS retrieval and grounded QA."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re
from typing import TYPE_CHECKING, BinaryIO, Iterable, Protocol

from pypdf import PdfReader

from config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    EMBEDDING_MODEL_NAME,
    NOT_FOUND_MESSAGE,
    SAFETY_DISCLAIMER,
    TOP_K,
)
from llm import generate_response


if TYPE_CHECKING:
    from langchain_community.vectorstores import FAISS
    from langchain_core.documents import Document
    from langchain_huggingface import HuggingFaceEmbeddings


class UploadedPDF(Protocol):
    """Minimal interface implemented by Streamlit UploadedFile."""

    name: str

    def getvalue(self) -> bytes: ...


class SDSProcessingError(RuntimeError):
    """Raised when an uploaded SDS cannot be processed."""


@dataclass(frozen=True)
class KnowledgeBaseResult:
    vector_store: FAISS
    page_count: int
    chunk_count: int
    file_names: tuple[str, ...]


@dataclass(frozen=True)
class AnswerResult:
    answer: str
    sources: tuple[tuple[str, int], ...]
    retrieved_documents: tuple[Document, ...]


_GENERIC_SOURCE_WORDS = {
    "sds",
    "msds",
    "simulated",
    "simulation",
    "safety",
    "data",
    "sheet",
    "test",
    "demo",
}

# Standard SDS topic routing.  This maps questions to document sections, not to
# chemical-specific answers, and prevents semantically similar passages from a
# different safety topic being treated as evidence.
_SECTION_INTENTS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, (r"\bidentif(?:ication|ier)\b", r"产品名称", r"化学品名称", r"供应商")),
    (2, (r"\bhazards?\b", r"\bdangers?\b", r"危险性", r"主要危害", r"危害概述")),
    (3, (r"\bcomposition\b", r"\bingredients?\b", r"成分", r"组成信息")),
    (4, (r"\bfirst[- ]?aid\b", r"\bskin contact\b", r"\beye contact\b", r"\binhal(?:e|ed|ation|ing)\b", r"急救", r"皮肤接触", r"眼睛", r"吸入")),
    (5, (r"\bfire(?:fighting)?\b", r"\bextinguish", r"消防", r"灭火", r"着火")),
    (6, (r"\bspill\b", r"\bleak(?:age)?\b", r"accidental release", r"泄漏", r"释放措施")),
    (7, (r"\bstor(?:e|age|ed|ing)\b", r"\bhandling\b", r"储存", r"贮存", r"操作处置")),
    (8, (r"\bppe\b", r"personal protective", r"exposure control", r"防护用品", r"个体防护", r"暴露控制", r"佩戴")),
    (9, (r"physical propert", r"\bboiling point\b", r"\bflash point\b", r"\bph\b", r"\bcolou?r\b", r"\bdensity\b", r"理化特性", r"沸点", r"闪点", r"颜色", r"密度")),
    (10, (r"\bstability\b", r"\breactivity\b", r"\bincompatib", r"稳定性", r"反应性", r"禁配物")),
    (11, (r"toxicological", r"\bld50\b", r"carcinogen", r"reproductive", r"毒理", r"致癌", r"生殖毒")),
    (12, (r"ecological", r"\blc50\b", r"\baquatic\b", r"生态", r"水生毒性")),
    (13, (r"\bdisposal\b", r"废弃处置", r"处置方法")),
    (14, (r"\btransport\b", r"\bun(?:ited nations)?\s*(?:number|no\.?|编号)\b", r"运输", r"联合国编号")),
    (15, (r"regulatory", r"法规信息", r"监管信息")),
    (16, (r"other information", r"其他信息")),
)


def _normalize_identity(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())


def _extract_sections(text: str) -> tuple[int, ...]:
    matches = re.findall(r"(?i)\bsection\s*(\d{1,2})\b|第\s*(\d{1,2})\s*部分", text)
    return tuple(dict.fromkeys(int(left or right) for left, right in matches))


def _extract_product_aliases(file_name: str, first_page_text: str) -> tuple[str, ...]:
    aliases: list[str] = []
    stem_parts = re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", Path(file_name).stem)
    aliases.extend(
        part
        for part in stem_parts
        if len(part) >= 2
        and not part.isdigit()
        and part.casefold() not in _GENERIC_SOURCE_WORDS
    )

    product_pattern = re.compile(
        r"(?im)^\s*(?:product\s+(?:name|identifier)|chemical\s+name|产品名称|化学品名称|品名)\s*[:：]\s*(.+?)\s*$"
    )
    match = product_pattern.search(first_page_text)
    if match:
        product_value = match.group(1).strip()
        aliases.append(product_value)
        aliases.extend(
            part.strip()
            for part in re.split(r"[/,，;；()（）]", product_value)
            if part.strip()
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        value = _normalize_identity(alias)
        if len(value) >= 2 and value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def _question_sections(question: str) -> frozenset[int]:
    normalized = question.casefold()
    sections = {
        section
        for section, patterns in _SECTION_INTENTS
        if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in patterns)
    }
    return frozenset(sections)


def _document_sections(document: Document) -> frozenset[int]:
    raw_sections = str(document.metadata.get("sections", ""))
    return frozenset(int(value) for value in raw_sections.split(",") if value.isdigit())


def _document_aliases(document: Document) -> tuple[str, ...]:
    return tuple(
        value
        for value in str(document.metadata.get("product_aliases", "")).split("|")
        if value
    )


def _target_sources(question: str, documents: Iterable[Document]) -> frozenset[str]:
    normalized_question = _normalize_identity(question)
    casefolded_question = question.casefold()
    matched: set[str] = set()
    for document in documents:
        alias_matches = False
        for alias in _document_aliases(document):
            if alias.isascii() and len(alias) <= 4:
                alias_matches = bool(
                    re.search(
                        rf"(?<![0-9a-z]){re.escape(alias)}(?![0-9a-z])",
                        casefolded_question,
                    )
                )
            else:
                alias_matches = alias in normalized_question
            if alias_matches:
                break
        if alias_matches:
            matched.add(str(document.metadata.get("source", "")))
    return frozenset(matched)


_QUESTION_STOPWORDS = {
    "what", "which", "who", "when", "where", "why", "how", "is", "are",
    "was", "were", "the", "a", "an", "of", "for", "to", "in", "on",
    "and", "or", "does", "do", "this", "product", "listed", "stated", "specified", "sds",
    "什么", "哪些", "如何", "怎么", "是否", "多少", "其中", "这个", "该",
}


def _has_lexical_evidence(question: str, document: Document) -> bool:
    """Conservative fallback for questions outside recognized SDS topics."""
    aliases = set(_document_aliases(document))
    ascii_tokens = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*", question)
        if len(token) >= 2 and token.casefold() not in _QUESTION_STOPWORDS
    }
    cjk_tokens = {
        token
        for token in re.findall(r"[\u4e00-\u9fff]{2,}", question)
        if token not in _QUESTION_STOPWORDS
    }
    query_tokens = {
        token
        for token in ascii_tokens | cjk_tokens
        if _normalize_identity(token) not in aliases
    }
    if not query_tokens:
        return False
    normalized_content = _normalize_identity(document.page_content)
    return any(_normalize_identity(token) in normalized_content for token in query_tokens)


def _read_upload(uploaded_file: UploadedPDF | BinaryIO) -> tuple[str, bytes]:
    file_name = getattr(uploaded_file, "name", "uploaded.pdf")
    if hasattr(uploaded_file, "getvalue"):
        file_bytes = uploaded_file.getvalue()
    else:
        file_bytes = uploaded_file.read()
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)
    if not file_bytes:
        raise SDSProcessingError(f"{file_name} 是空文件。")
    return file_name, file_bytes


def parse_pdf_bytes(file_name: str, file_bytes: bytes) -> list[Document]:
    """Extract page text and preserve one-based file/page metadata."""
    from langchain_core.documents import Document

    try:
        reader = PdfReader(BytesIO(file_bytes))
    except Exception as exc:
        raise SDSProcessingError(f"无法读取 PDF：{file_name}") from exc

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise SDSProcessingError(f"PDF 已加密，无法解析：{file_name}") from exc

    documents: list[Document] = []
    active_section: int | None = None
    first_page_text = ""
    for page_index, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:
            raise SDSProcessingError(
                f"解析 {file_name} 第 {page_index} 页时失败。"
            ) from exc
        page_text = page_text.strip()
        if page_text:
            if not first_page_text:
                first_page_text = page_text
            page_sections = _extract_sections(page_text)
            if page_sections:
                active_section = page_sections[-1]
            elif active_section is not None:
                page_sections = (active_section,)
            documents.append(
                Document(
                    page_content=page_text,
                    metadata={
                        "source": file_name,
                        "page": page_index,
                        "sections": ",".join(str(value) for value in page_sections),
                    },
                )
            )

    if not documents:
        raise SDSProcessingError(
            f"{file_name} 未提取到可检索文字；它可能是扫描件，MVP 暂不支持 OCR。"
        )
    aliases = "|".join(_extract_product_aliases(file_name, first_page_text))
    for document in documents:
        document.metadata["product_aliases"] = aliases
    return documents


def split_documents(page_documents: Iterable[Document]) -> list[Document]:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        length_function=len,
        separators=["\n\n", "\n", "。", ". ", "；", "; ", " ", ""],
    )
    chunks = splitter.split_documents(list(page_documents))
    for index, chunk in enumerate(chunks):
        chunk.metadata["chunk_id"] = index
    return chunks


@lru_cache(maxsize=1)
def get_embeddings() -> "HuggingFaceEmbeddings":
    """Load the multilingual embedding model once per Python process."""
    from langchain_huggingface import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL_NAME,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )


def build_knowledge_base(
    uploaded_files: Iterable[UploadedPDF | BinaryIO],
) -> KnowledgeBaseResult:
    files = list(uploaded_files)
    if not files:
        raise SDSProcessingError("请至少上传一份 SDS PDF。")

    page_documents: list[Document] = []
    file_names: list[str] = []
    for uploaded_file in files:
        file_name, file_bytes = _read_upload(uploaded_file)
        file_names.append(file_name)
        page_documents.extend(parse_pdf_bytes(file_name, file_bytes))

    chunks = split_documents(page_documents)
    if not chunks:
        raise SDSProcessingError("PDF 已解析，但没有生成可检索的文本块。")

    from langchain_community.vectorstores import FAISS

    vector_store = FAISS.from_documents(chunks, get_embeddings())
    return KnowledgeBaseResult(
        vector_store=vector_store,
        page_count=len(page_documents),
        chunk_count=len(chunks),
        file_names=tuple(file_names),
    )


def retrieve_documents(
    vector_store: FAISS,
    question: str,
    top_k: int = TOP_K,
) -> list[Document]:
    cleaned_question = question.strip()
    if not cleaned_question:
        return []

    total_documents = int(getattr(vector_store.index, "ntotal", top_k))
    candidate_count = min(total_documents, max(top_k * 8, 40))
    scored_candidates = vector_store.similarity_search_with_score(
        cleaned_question,
        k=candidate_count,
    )
    if not scored_candidates:
        return []

    candidates = [document for document, _score in scored_candidates]
    target_sources = _target_sources(cleaned_question, candidates)
    requested_sections = _question_sections(cleaned_question)

    filtered: list[Document] = []
    for document, _score in scored_candidates:
        source = str(document.metadata.get("source", ""))
        if target_sources and source not in target_sources:
            continue
        if requested_sections and not (_document_sections(document) & requested_sections):
            continue
        if not requested_sections and not _has_lexical_evidence(cleaned_question, document):
            continue
        filtered.append(document)

    # A recognized SDS topic with no corresponding section is an evidence gap,
    # not permission to answer from a merely similar passage.
    if requested_sections and not filtered:
        return []

    return filtered[:top_k]


def build_grounded_prompt(question: str, documents: Iterable[Document]) -> str:
    context_blocks: list[str] = []
    for index, document in enumerate(documents, start=1):
        source = document.metadata.get("source", "未知文件")
        page = document.metadata.get("page", "未知")
        context_blocks.append(
            f"[Context {index} | Source: {source} | Page: {page}]\n"
            f"{document.page_content}"
        )

    context = "\n\n".join(context_blocks)
    return f"""请仅依据下方 SDS 检索上下文回答问题。

如果上下文不能直接支持答案，请只回答：{NOT_FOUND_MESSAGE}

SDS CONTEXT:
{context}

USER QUESTION:
{question.strip()}

回答末尾不需要自行编写来源，来源文件和页码由系统单独展示。
安全提示：{SAFETY_DISCLAIMER}
"""


def collect_sources(documents: Iterable[Document]) -> tuple[tuple[str, int], ...]:
    sources: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for document in documents:
        source = str(document.metadata.get("source", "未知文件"))
        page = int(document.metadata.get("page", 0))
        item = (source, page)
        if item not in seen:
            seen.add(item)
            sources.append(item)
    return tuple(sources)


def answer_question(vector_store: FAISS, question: str) -> AnswerResult:
    documents = retrieve_documents(vector_store, question)
    if not documents:
        return AnswerResult(
            answer=NOT_FOUND_MESSAGE,
            sources=(),
            retrieved_documents=(),
        )

    prompt = build_grounded_prompt(question, documents)
    answer = generate_response(prompt)
    return AnswerResult(
        answer=answer,
        sources=collect_sources(documents),
        retrieved_documents=tuple(documents),
    )

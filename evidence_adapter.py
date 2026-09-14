"""Evidence ingestion/retrieval adapters for Safety Review Pack generation.

SDS and enterprise documents deliberately use separate vector stores. The
adapter reuses the existing parser, chunker, embeddings and FAISS dependency,
but exposes one Citation shape to the rest of Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Any, Iterable, Mapping
from uuid import uuid4

from rag import get_embeddings, parse_pdf_bytes, retrieve_documents, split_documents
from safety_review import normalize_citation


@dataclass(frozen=True)
class EvidenceStores:
    sds: Any = None
    sop: Any = None
    internal: Any = None


def document_metadata(
    *,
    source_type: str,
    file_name: str,
    file_bytes: bytes,
    version: str = "",
    is_demo: bool = False,
    document_id: str = "",
) -> dict[str, Any]:
    """Return persistence-safe metadata; never stores bytes or local paths."""
    return {
        "document_id": str(document_id or uuid4().hex),
        "source_type": str(source_type),
        "file_name": str(file_name),
        "version": str(version or "未标注"),
        "sha256": sha256(file_bytes).hexdigest(),
        "uploaded_at": "",
        "is_demo": bool(is_demo),
    }


def _read_resource(resource: Mapping[str, Any]) -> tuple[str, bytes]:
    file_name = str(resource.get("file_name") or resource.get("name") or "uploaded.pdf")
    payload = resource.get("file_bytes")
    if payload is None and hasattr(resource.get("upload"), "getvalue"):
        payload = resource["upload"].getvalue()
    if not isinstance(payload, bytes) or not payload:
        raise ValueError(f"{file_name} 缺少可解析的文件内容。")
    return file_name, payload


def _parse_resource(resource: Mapping[str, Any]) -> list[Any]:
    """Parse PDF or UTF-8 text without weakening the existing SDS parser."""
    from langchain_core.documents import Document

    file_name, payload = _read_resource(resource)
    if file_name.casefold().endswith(".pdf"):
        pages = parse_pdf_bytes(file_name, payload)
    elif file_name.casefold().endswith((".txt", ".md")):
        text = payload.decode("utf-8", errors="replace").strip()
        if not text:
            raise ValueError(f"{file_name} 未提取到文字。")
        pages = [Document(page_content=text, metadata={"source": file_name, "page": 1})]
    else:
        raise ValueError(f"暂不支持的资料格式：{file_name}。")
    for page in pages:
        page.metadata.update(
            {
                "document_id": str(resource.get("document_id") or uuid4().hex),
                "source_type": str(resource.get("source_type") or "internal"),
                "version": str(resource.get("version") or "未标注"),
                "is_demo": bool(resource.get("is_demo", False)),
            }
        )
    return pages


def _build_store(resources: Iterable[Mapping[str, Any]]) -> Any:
    pages: list[Any] = []
    for resource in resources:
        pages.extend(_parse_resource(resource))
    if not pages:
        return None
    chunks = split_documents(pages)
    from langchain_community.vectorstores import FAISS

    return FAISS.from_documents(chunks, get_embeddings())


def build_evidence_stores(resources: Iterable[Mapping[str, Any]]) -> EvidenceStores:
    """Build separate session-level stores so SDS and internal evidence never mix."""
    buckets: dict[str, list[Mapping[str, Any]]] = {"sds": [], "sop": [], "internal": []}
    for resource in resources:
        source_type = str(resource.get("source_type") or "internal")
        if source_type in buckets:
            buckets[source_type].append(resource)
    return EvidenceStores(
        sds=_build_store(buckets["sds"]),
        sop=_build_store(buckets["sop"]),
        internal=_build_store(buckets["internal"]),
    )


def citation_from_document(document: Any, *, position: int = 1) -> dict[str, Any]:
    metadata = dict(getattr(document, "metadata", {}) or {})
    source_type = str(metadata.get("source_type") or "internal")
    source_name = str(metadata.get("source") or metadata.get("file_name") or "")
    key = "|".join(
        [source_type, source_name, str(metadata.get("page") or 0), str(metadata.get("chunk_id") or position)]
    )
    evidence_id = str(metadata.get("evidence_id") or f"EV-{sha256(key.encode('utf-8')).hexdigest()[:12]}")
    return normalize_citation(
        {
            "evidence_id": evidence_id,
            "source_type": source_type,
            "source_name": source_name,
            "version": metadata.get("version", "未标注"),
            "page": metadata.get("page", 0),
            "snippet": str(getattr(document, "page_content", ""))[:700],
            "chunk_id": metadata.get("chunk_id", position),
            "locator": metadata.get("locator", ""),
        }
    )


def _generic_retrieve(store: Any, query: str, top_k: int) -> list[Any]:
    if store is None or not str(query).strip():
        return []
    return [doc for doc, _score in store.similarity_search_with_score(str(query), k=top_k)]


def retrieve_evidence(
    stores: EvidenceStores,
    *,
    query: str,
    top_k: int = 4,
) -> dict[str, list[dict[str, Any]]]:
    """Retrieve SDS with existing strict routing and other material generically."""
    documents: dict[str, list[Any]] = {
        "sds": retrieve_documents(stores.sds, query, top_k=top_k) if stores.sds else [],
        "sop": _generic_retrieve(stores.sop, query, top_k),
        "internal": _generic_retrieve(stores.internal, query, top_k),
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for track, values in documents.items():
        citations = []
        for index, document in enumerate(values, start=1):
            document.metadata["source_type"] = track
            citations.append(citation_from_document(document, position=index))
        result[track] = citations
    return result


def _normal(value: str) -> str:
    return "".join(char for char in str(value).casefold() if char.isalnum())


_CHEMICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "氢氟酸": ("氢氟酸", "hydrofluoricacid", "hydrogenfluoride", "hf"),
    "盐酸": ("盐酸", "hydrochloricacid", "hcl"),
    "硫酸": ("硫酸", "sulfuricacid", "sulphuricacid", "h2so4"),
}


def citation_matches_chemical(citation: Mapping[str, Any], chemical: str) -> bool:
    """Conservative identity match; no match is safer than a false match."""
    haystack = _normal(
        " ".join(
            [
                str(citation.get("source_name", "")),
                str(citation.get("snippet", "")),
            ]
        )
    )
    name = _normal(chemical)
    aliases = {_normal(value) for value in _CHEMICAL_ALIASES.get(str(chemical), (chemical,))}
    aliases.add(name)
    return any(alias and alias in haystack for alias in aliases)


def sds_match_report(
    chemicals: Iterable[str], citations: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    items = [normalize_citation(item) for item in citations]
    missing = [
        str(chemical)
        for chemical in chemicals
        if not any(citation_matches_chemical(item, str(chemical)) for item in items)
    ]
    return {"matched": not missing, "missing_chemicals": missing}


def version_conflicts(resources: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Detect multiple unselected versions of the same named SDS."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for resource in resources:
        if str(resource.get("source_type")) != "sds":
            continue
        name = _normal(str(resource.get("chemical_name") or resource.get("file_name") or ""))
        grouped.setdefault(name, []).append(resource)
    conflicts = []
    for name, values in grouped.items():
        versions = {str(item.get("version") or "未标注") for item in values}
        selected = [item for item in values if item.get("selected_version")]
        if name and len(versions) > 1 and len(selected) != 1:
            conflicts.append(
                {
                    "text": "同一化学品存在多个SDS版本，尚未选择当前适用版本",
                    "document_ids": [str(item.get("document_id", "")) for item in values],
                    "versions": sorted(versions),
                    "resolved": False,
                }
            )
    return conflicts


__all__ = [
    "EvidenceStores",
    "build_evidence_stores",
    "citation_from_document",
    "citation_matches_chemical",
    "document_metadata",
    "retrieve_evidence",
    "sds_match_report",
    "version_conflicts",
]

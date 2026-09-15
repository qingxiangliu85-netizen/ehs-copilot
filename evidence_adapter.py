"""Evidence ingestion/retrieval adapters for Safety Review Pack generation.

SDS and enterprise documents deliberately use separate vector stores. The
adapter reuses the existing parser, chunker, embeddings and FAISS dependency,
but exposes one Citation shape to the rest of Phase 1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
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
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return persistence-safe metadata; never stores bytes or local paths."""
    metadata = {
        "document_id": str(document_id or uuid4().hex),
        "source_type": str(source_type),
        "file_name": str(file_name),
        "version": str(version or "未标注"),
        "sha256": sha256(file_bytes).hexdigest(),
        "uploaded_at": "",
        "is_demo": bool(is_demo),
    }
    if extra:
        # Provenance fields (source_url, chemical_name, source_nature, ...) ride
        # along; bytes and local paths must never enter persistence.
        metadata.update(
            {key: value for key, value in dict(extra).items() if key != "file_bytes"}
        )
    return metadata


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


_DEMO_DOCUMENTS_DIR = Path(__file__).resolve().parent / "data" / "demo_documents"
_DEMO_REGISTRY = _DEMO_DOCUMENTS_DIR / "registry.json"


def demo_document_registry() -> list[dict[str, Any]]:
    """Return the minimal credible demo document registry (SDS + Synthetic SOP)."""
    if not _DEMO_REGISTRY.exists():
        return []
    try:
        payload = json.loads(_DEMO_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    documents = payload.get("documents") if isinstance(payload, dict) else None
    return [dict(item) for item in documents or [] if isinstance(item, Mapping)]


def load_demo_resources(document_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Load selected registry documents as parseable resources (bytes in memory)."""
    wanted = [str(item) for item in document_ids if str(item)]
    if not wanted:
        return []
    resources: list[dict[str, Any]] = []
    for entry in demo_document_registry():
        if str(entry.get("document_id")) not in wanted:
            continue
        path = _DEMO_DOCUMENTS_DIR / str(entry.get("file_name"))
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        if not payload:
            continue
        resources.append(
            {
                **document_metadata(
                    source_type=str(entry.get("source_type") or "internal"),
                    file_name=str(entry.get("file_name")),
                    file_bytes=payload,
                    version=str(entry.get("version") or ""),
                    is_demo=bool(entry.get("is_demo", False)),
                    document_id=str(entry.get("document_id")),
                    extra={
                        key: value
                        for key, value in entry.items()
                        if key not in {"file_name", "version", "is_demo", "document_id"}
                    },
                ),
                "file_bytes": payload,
            }
        )
    return resources


def _normal(value: str) -> str:
    return "".join(char for char in str(value).casefold() if char.isalnum())


_CHEMICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "氢氟酸": ("氢氟酸", "hydrofluoricacid", "hydrogenfluoride", "hf"),
    "甲醇": ("甲醇", "methanol", "methylalcohol", "woodalcohol"),
    "盐酸": ("盐酸", "hydrochloricacid", "hcl"),
    "硫酸": ("硫酸", "sulfuricacid", "sulphuricacid", "h2so4"),
}


def chemical_aliases(chemical: str) -> tuple[str, ...]:
    """Return known aliases for a declared chemical (generic, evidence-agnostic)."""
    name = str(chemical)
    aliases = tuple(_CHEMICAL_ALIASES.get(name, ()))
    return (name, *aliases)


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


def _conflict_id(text: str, refs: Iterable[str]) -> str:
    key = "|".join([text, *sorted(str(ref) for ref in refs)])
    return f"CONF-{sha256(key.encode('utf-8')).hexdigest()[:10]}"


def _make_conflict(
    description: str,
    *,
    kind: str,
    evidence_refs: Iterable[object] = (),
    document_ids: Iterable[str] = (),
    versions: Iterable[str] = (),
) -> dict[str, Any]:
    refs = [str(ref) for ref in evidence_refs if str(ref)]
    return {
        "conflict_id": _conflict_id(kind + description, refs),
        "kind": str(kind),
        "text": str(description),
        "description": str(description),
        "evidence_refs": refs,
        "document_ids": [str(item) for item in document_ids if str(item)],
        "versions": sorted({str(item) for item in versions if str(item)}),
        "status": "unresolved",
        "resolved": False,
        "requires_human_resolution": True,
    }


def version_conflicts(resources: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Detect multiple unselected versions of the same named SDS (conflict type 1)."""
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
                _make_conflict(
                    "同一化学品存在多个SDS版本，尚未选择当前适用版本",
                    kind="sds_version",
                    evidence_refs=[item.get("document_id", "") for item in values],
                    document_ids=[item.get("document_id", "") for item in values],
                    versions=versions,
                )
            )
    return conflicts


def chemical_mismatch_conflicts(
    chemicals: Iterable[str],
    sds_citations: Iterable[Mapping[str, Any]],
    input_refs: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Detect declared chemicals not covered by any selected SDS (conflict type 3).

    A conflict is only recorded when real SDS evidence exists but does not match
    the declared chemical; with no SDS at all the plain missing-item path applies.
    """
    citations = [normalize_citation(item) for item in sds_citations]
    if not citations:
        return []
    conflicts = []
    for chemical in {str(item) for item in chemicals if str(item).strip()}:
        if any(citation_matches_chemical(item, chemical) for item in citations):
            continue
        conflicts.append(
            _make_conflict(
                f"已声明化学品「{chemical}」与当前所选SDS不匹配（chemical mismatch）："
                "所选SDS未覆盖该化学品，缺少适用SDS",
                kind="chemical_mismatch",
                evidence_refs=[*input_refs, *[item.get("evidence_id") for item in citations]],
            )
        )
    return conflicts


def conflict_is_unresolved(conflict: Mapping[str, Any]) -> bool:
    return not bool(conflict.get("resolved"))


def unresolved_conflict_note(conflicts: Iterable[Mapping[str, Any]]) -> str:
    """Return the explicit note required when no unresolved conflict remains."""
    if any(conflict_is_unresolved(conflict) for conflict in conflicts):
        return "存在未解决的资料冲突，必须人工处理。"
    return "No unresolved evidence conflict detected."


__all__ = [
    "EvidenceStores",
    "build_evidence_stores",
    "citation_from_document",
    "citation_matches_chemical",
    "chemical_aliases",
    "chemical_mismatch_conflicts",
    "conflict_is_unresolved",
    "demo_document_registry",
    "document_metadata",
    "load_demo_resources",
    "retrieve_evidence",
    "sds_match_report",
    "unresolved_conflict_note",
    "version_conflicts",
]

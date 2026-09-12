"""Clearly-labelled HF pickling demo case data assets for V4 (P2).

Data rules
----------
1. Business data (job name, area, people, dates, simulated hazards and
   rectification-evidence placeholders) is fictional and carries
   ``hazards.DEMO_DATA_LABEL``.
2. No HF safety conclusion is authored in this module.  Verbatim quotes of
   public government sources live in the separate, clearly labelled evidence
   pack (``data/demo_evidence/hf_evidence_pack.json``) and are never treated as
   SDS evidence.  SDS-grade content may only come from a traceable SDS that is
   legally safe to redistribute and registered in
   ``data/demo_sds/hf_sds_source_registry.json``.
3. While the registry reports ``pending_real_source``,
   :func:`create_demo_hf_case` exposes zero SDS evidence, the job keeps no JSA
   draft, and :func:`require_hf_sds_source` refuses to continue — a missing
   source can therefore never silently become a safety conclusion.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Mapping

from hazards import DEMO_DATA_LABEL, create_hazard_record
from jobs import create_job
from job_review import attach_public_evidence


PROJECT_ROOT = Path(__file__).resolve().parent
HF_SDS_REGISTRY_PATH = (
    PROJECT_ROOT / "data" / "demo_sds" / "hf_sds_source_registry.json"
)
HF_SDS_REGISTRY_RELATIVE = "data/demo_sds/hf_sds_source_registry.json"
HF_EVIDENCE_PACK_PATH = (
    PROJECT_ROOT / "data" / "demo_evidence" / "hf_evidence_pack.json"
)
HF_EVIDENCE_PACK_RELATIVE = "data/demo_evidence/hf_evidence_pack.json"

HF_CASE_ID = "hf-pickling-demo"
HF_JOB_ID = "DEMO-JOB-HF-001"

_FALLBACK_REGISTRY: dict[str, object] = {
    "status": "pending_real_source",
    "candidate_sources": [],
    "registered_demo_sds": {},
    "rule": "未找到 HF SDS 来源登记表；在来源核验前不得生成任何 HF 安全结论。",
}

_FALLBACK_EVIDENCE_PACK: dict[str, object] = {
    "status": "unavailable",
    "sources": [],
    "items": [],
    "gaps": [],
}


class HFCaseSourceError(RuntimeError):
    """Raised when the HF case is used before a traceable SDS source exists."""


# --------------------------------------------------------------------------- #
# Source registry access
# --------------------------------------------------------------------------- #


def load_hf_sds_registry(
    registry_path: Path | str | None = None,
) -> dict[str, object]:
    """Return the HF SDS source registry, or a safe pending fallback."""
    target = Path(registry_path) if registry_path else HF_SDS_REGISTRY_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return deepcopy(_FALLBACK_REGISTRY)
    if not isinstance(data, dict):
        return deepcopy(_FALLBACK_REGISTRY)
    return data


def _registered_source_file(
    registry: Mapping[str, object],
    registry_path: Path,
) -> Path | None:
    registered = registry.get("registered_demo_sds")
    if not isinstance(registered, Mapping):
        return None
    file_name = str(registered.get("file_name", "") or "").strip()
    if not file_name:
        return None
    return registry_path.parent / file_name


def _ready_source_path(registry_path: Path) -> Path | None:
    registry = load_hf_sds_registry(registry_path)
    if str(registry.get("status", "")) != "ready":
        return None
    candidate = _registered_source_file(registry, registry_path)
    if candidate is None or not candidate.is_file():
        return None
    return candidate


def hf_sds_source_ready(registry_path: Path | str | None = None) -> bool:
    """Return whether a registered HF SDS file actually exists on disk."""
    target = Path(registry_path) if registry_path else HF_SDS_REGISTRY_PATH
    return _ready_source_path(target) is not None


def require_hf_sds_source(registry_path: Path | str | None = None) -> Path:
    """Return the registered HF SDS path, or refuse to continue.

    Raises:
        HFCaseSourceError: when no legally usable, traceable HF SDS has been
            registered, so no deterministic HF safety conclusion may be made.
    """
    target = Path(registry_path) if registry_path else HF_SDS_REGISTRY_PATH
    candidate = _ready_source_path(target)
    if candidate is None:
        registry = load_hf_sds_registry(target)
        raise HFCaseSourceError(
            "HF 案例尚未登记可合法再分发且可核验的 HF SDS"
            f"（status={registry.get('status', 'unknown')!r}），"
            "因此不能生成任何 HF 危险性、PPE、急救、泄漏或消防结论。"
            f"请按 {HF_SDS_REGISTRY_RELATIVE} 的规则补充来源。"
        )
    return candidate


# --------------------------------------------------------------------------- #
# Public-source evidence pack (real quotes, no fabrication)
# --------------------------------------------------------------------------- #


def load_hf_evidence_pack(
    pack_path: Path | str | None = None,
) -> dict[str, object]:
    """Return the public-source HF evidence pack, or a safe empty fallback."""
    target = Path(pack_path) if pack_path else HF_EVIDENCE_PACK_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return deepcopy(_FALLBACK_EVIDENCE_PACK)
    if not isinstance(data, dict):
        return deepcopy(_FALLBACK_EVIDENCE_PACK)
    return data


def hf_public_evidence(
    pack_path: Path | str | None = None,
) -> list[dict[str, object]]:
    """Return the pack's evidence items as copies.

    Every item is a verbatim quote of a public government source.  This
    evidence is *not* SDS evidence: it may only be used for demonstration and
    human cross-checking.
    """
    pack = load_hf_evidence_pack(pack_path)
    items = pack.get("items")
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, Mapping)]


# --------------------------------------------------------------------------- #
# Simulated business data (no HF safety clauses)
# --------------------------------------------------------------------------- #


HF_SITE: dict[str, str] = {
    "area": "金工车间酸洗区（模拟区域）",
    "requester": "作业申请人（模拟）",
    "executor": "执行人（模拟）",
    "reviewer": "复查人（模拟）",
}

HF_CHEMICALS: list[dict[str, object]] = [
    {
        "name": "氢氟酸",
        "sds_file": "",
        "sds_status": "pending_real_source",
        "aliases": ["HF", "hydrofluoric acid", "hydrofluoric"],
        "note": (
            "化学品名称与别名仅用于 Demo 关联；SDS 内容待补，"
            "在来源就绪前不提供任何 HF 安全结论。"
        ),
    }
]

HF_STEPS: list[dict[str, object]] = [
    {"order": 1, "name": "预处理除油", "note": "模拟步骤，不代表真实 SOP"},
    {"order": 2, "name": "密闭配液", "note": "模拟步骤，不代表真实 SOP"},
    {"order": 3, "name": "浸洗", "note": "模拟步骤，不代表真实 SOP"},
    {"order": 4, "name": "漂洗", "note": "模拟步骤，不代表真实 SOP"},
    {"order": 5, "name": "中和", "note": "模拟步骤，不代表真实 SOP"},
    {"order": 6, "name": "废液收集", "note": "模拟步骤，不代表真实 SOP"},
]

HF_HAZARD_TEMPLATES: tuple[dict[str, object], ...] = (
    {
        "hazard_id": "DEMO-HZ-HF-001",
        "description": "HF作业区应急冲洗设施点检记录缺失（模拟隐患）",
        "hazard_type": "危化品管理",
        "risk_level": "高",
        "owner": "整改责任人甲（模拟）",
        "due_offset_days": 7,
        "corrective_action": "模拟整改：补充点检记录并核对点检频次。",
        "evidence_placeholder": {
            "file_name": "（占位）冲洗设施点检记录（模拟）.pdf",
            "kind": "记录",
        },
    },
    {
        "hazard_id": "DEMO-HZ-HF-002",
        "description": "HF周转容器二次防泄漏托盘数量与台账不符（模拟隐患）",
        "hazard_type": "危化品管理",
        "risk_level": "高",
        "owner": "整改责任人乙（模拟）",
        "due_offset_days": 14,
        "corrective_action": "模拟整改：核对并补齐二次防泄漏托盘，更新台账。",
        "evidence_placeholder": {
            "file_name": "（占位）托盘配置与台账核对表（模拟）.xlsx",
            "kind": "记录",
        },
    },
    {
        "hazard_id": "DEMO-HZ-HF-003",
        "description": "HF作业人员培训与授权记录未归档（模拟隐患）",
        "hazard_type": "危化品管理",
        "risk_level": "中",
        "owner": "整改责任人丙（模拟）",
        "due_offset_days": 21,
        "corrective_action": "模拟整改：收集并归档培训与授权记录。",
        "evidence_placeholder": {
            "file_name": "（占位）培训与授权记录归档清单（模拟）.pdf",
            "kind": "记录",
        },
    },
)

HF_DATA_GAPS: tuple[dict[str, str], ...] = (
    {
        "item": "真实且可合法再分发的 HF SDS",
        "detail": "需覆盖标识/危险性/成分/急救/消防/泄漏/操作储存/个体防护/运输章节。",
    },
    {
        "item": "由 SDS 推导的危害因素、可能后果与控制措施",
        "detail": "必须等 SDS 来源就绪后，由可追溯的 SDS 证据生成 JSA 草稿；公开来源证据不能替代 SDS。",
    },
    {
        "item": "整改证据真实文件",
        "detail": "当前仅有占位记录，尚未上传任何真实证据。",
    },
)

HF_JOB_CASE: dict[str, object] = {
    "case_id": HF_CASE_ID,
    "job_id": HF_JOB_ID,
    "job_name": "不锈钢件HF酸洗作业（模拟案例）",
    "job_type": "危化品非例行作业",
    "data_label": DEMO_DATA_LABEL,
    "site": HF_SITE,
    "chemicals": HF_CHEMICALS,
    "steps": HF_STEPS,
    "hazard_templates": HF_HAZARD_TEMPLATES,
    "data_gaps": HF_DATA_GAPS,
    "sds_source_registry": HF_SDS_REGISTRY_RELATIVE,
}


# --------------------------------------------------------------------------- #
# Case builder
# --------------------------------------------------------------------------- #


def _evidence_placeholders() -> list[dict[str, object]]:
    placeholders: list[dict[str, object]] = []
    for template in HF_HAZARD_TEMPLATES:
        placeholder = dict(template["evidence_placeholder"])  # type: ignore[arg-type]
        placeholders.append(
            {
                "hazard_id": str(template["hazard_id"]),
                **placeholder,
                "status": "pending_upload",
                "note": "模拟整改证据占位，尚未上传真实文件。",
            }
        )
    return placeholders


def create_demo_hf_case(
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Build the fully labelled HF pickling demo case.

    The job starts in ``草稿`` with zero SDS evidence and no JSA draft, because
    the source registry reports ``pending_real_source``.  Every record carries
    ``DEMO_DATA_LABEL``; nothing produced here is a safety conclusion.
    """
    base = today or date.today()
    registry = load_hf_sds_registry()
    evidence_pack = load_hf_evidence_pack()
    public_items = hf_public_evidence()

    job = create_job(
        job_id=HF_JOB_ID,
        job_name=str(HF_JOB_CASE["job_name"]),
        job_type=str(HF_JOB_CASE["job_type"]),
        chemicals=HF_CHEMICALS,
        steps=list(HF_STEPS),
        sds_evidence=[],
        jsa_draft=None,
        created_by=HF_SITE["requester"],
        data_label=DEMO_DATA_LABEL,
        now=now,
    )
    job["area"] = HF_SITE["area"]
    attach_public_evidence(job, public_items)

    hazards: list[dict[str, object]] = []
    for template in HF_HAZARD_TEMPLATES:
        found_on = base
        due_on = found_on + timedelta(days=int(template["due_offset_days"]))
        hazards.append(
            create_hazard_record(
                hazard_id=str(template["hazard_id"]),
                description=str(template["description"]),
                hazard_type=str(template["hazard_type"]),
                risk_level=str(template["risk_level"]),
                owner=str(template["owner"]),
                found_on=found_on,
                due_on=due_on,
                corrective_action=str(template["corrective_action"]),
                status="待整改",
                data_label=DEMO_DATA_LABEL,
                related_job_id=HF_JOB_ID,
            )
        )
    job["linked_hazard_ids"] = [
        str(item["隐患编号"]) for item in hazards
    ]

    return {
        "case_id": HF_CASE_ID,
        "data_label": DEMO_DATA_LABEL,
        "job": job,
        "chemicals": job["chemicals"],
        "steps": deepcopy(list(HF_STEPS)),
        "linked_hazards": hazards,
        "evidence_placeholders": _evidence_placeholders(),
        "sds_source": {
            "status": registry.get("status", "unknown"),
            "registry_path": HF_SDS_REGISTRY_RELATIVE,
            "ready": hf_sds_source_ready(),
            "note": "来源未就绪前，本案例不提供任何 HF 安全结论。",
        },
        "public_evidence": {
            "status": evidence_pack.get("status", "unknown"),
            "label": "真实公开来源安全证据（非 SDS）",
            "pack_path": HF_EVIDENCE_PACK_RELATIVE,
            "items": public_items,
            "note": (
                "逐字引用美国政府公开资料（NIOSH/OSHA），仅用于演示与人工交叉核对，"
                "不替代 SDS 原文，也不得作为 SDS 证据参与安全结论。"
            ),
        },
        "evidence_tracks": [
            {
                "track": "public_sources",
                "label": "真实公开来源安全证据（非 SDS）",
                "ready": bool(public_items),
                "count": len(public_items),
            },
            {
                "track": "sds",
                "label": "HF SDS 证据",
                "ready": hf_sds_source_ready(),
                "count": 0,
            },
        ],
        "sds_evidence": [],
        "data_gaps": [dict(item) for item in HF_DATA_GAPS],
        "generated_on": base.isoformat(),
    }


__all__ = [
    "HF_CASE_ID",
    "HF_CHEMICALS",
    "HF_DATA_GAPS",
    "HF_EVIDENCE_PACK_PATH",
    "HF_EVIDENCE_PACK_RELATIVE",
    "HF_HAZARD_TEMPLATES",
    "HF_JOB_CASE",
    "HF_JOB_ID",
    "HF_SDS_REGISTRY_PATH",
    "HF_SDS_REGISTRY_RELATIVE",
    "HF_SITE",
    "HF_STEPS",
    "HFCaseSourceError",
    "create_demo_hf_case",
    "hf_public_evidence",
    "hf_sds_source_ready",
    "load_hf_evidence_pack",
    "load_hf_sds_registry",
    "require_hf_sds_source",
]

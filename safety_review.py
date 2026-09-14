"""Pure domain rules for high-risk-work preparation and Safety Review Packs.

This module deliberately contains no Streamlit, database or network access.
It is the single place for Phase 1 statuses, intake validation, deterministic
fallback classification, JSA risk calculation and confirmation blockers.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Iterable, Mapping

from jsa import calculate_risk


WORK_TYPES: tuple[str, ...] = (
    "危化品作业",
    "动火作业",
    "受限空间",
    "高处作业",
    "吊装作业",
    "临时用电",
    "动土/开挖",
    "检维修与能量隔离",
    "其他高风险作业",
)

RISK_TAGS: tuple[str, ...] = (
    "化学品暴露",
    "腐蚀/灼伤",
    "火灾爆炸",
    "中毒窒息",
    "高处坠落",
    "触电",
    "机械伤害",
    "压力/能量释放",
    "起重与物体打击",
    "受限空间",
    "环境泄漏",
    "交叉作业",
    "承包商作业",
    "其他",
)

STATUS_DRAFT = "draft"
STATUS_NEEDS_INPUT = "needs_input"
STATUS_REVIEW_READY = "review_ready"
STATUS_CONFIRMED = "confirmed"
STATUSES = (STATUS_DRAFT, STATUS_NEEDS_INPUT, STATUS_REVIEW_READY, STATUS_CONFIRMED)

STATUS_LABELS = {
    STATUS_DRAFT: "草稿",
    STATUS_NEEDS_INPUT: "待补充资料",
    STATUS_REVIEW_READY: "待EHS确认",
    STATUS_CONFIRMED: "审核包已确认",
}

SOURCE_TYPES = ("sds", "sop", "internal", "public", "user_input")
DOCUMENT_SOURCE_TYPES = ("sds", "sop", "internal", "public")

_WORK_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("受限空间", ("受限空间", "有限空间", "罐内", "池内", "井下")),
    ("动火作业", ("动火", "焊接", "切割", "打磨", "明火")),
    ("高处作业", ("高处", "登高", "脚手架", "屋顶", "高空")),
    ("吊装作业", ("吊装", "起重", "吊车", "行车")),
    ("临时用电", ("临时用电", "临电", "配电箱", "带电")),
    ("动土/开挖", ("动土", "开挖", "挖掘", "破土")),
    ("检维修与能量隔离", ("检修", "维修", "能量隔离", "上锁挂牌", "loto")),
    ("危化品作业", ("危化品", "化学品", "酸洗", "hf", "氢氟酸", "盐酸", "硫酸")),
)

_RISK_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("化学品暴露", ("化学品", "危化品", "hf", "氢氟酸", "盐酸", "硫酸", "酸洗", "溶剂")),
    ("腐蚀/灼伤", ("酸", "碱", "腐蚀", "灼伤", "飞溅")),
    ("火灾爆炸", ("动火", "焊", "切割", "易燃", "爆炸", "可燃")),
    ("中毒窒息", ("有毒", "窒息", "缺氧", "受限空间", "有限空间", "罐内")),
    ("高处坠落", ("高处", "登高", "脚手架", "高空", "屋顶")),
    ("触电", ("用电", "带电", "配电", "电气", "电缆")),
    ("机械伤害", ("机械", "转动", "夹伤", "切割机", "设备检修")),
    ("压力/能量释放", ("压力", "蒸汽", "能量隔离", "上锁挂牌", "loto")),
    ("起重与物体打击", ("吊装", "起重", "吊车", "物体打击")),
    ("受限空间", ("受限空间", "有限空间", "罐内", "池内", "井下")),
    ("环境泄漏", ("泄漏", "溢出", "排放", "环境")),
    ("交叉作业", ("交叉作业", "同时作业", "多工种")),
    ("承包商作业", ("承包商", "外包", "施工方")),
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _unique(values: Iterable[object], allowed: Iterable[str] | None = None) -> list[str]:
    allowed_set = set(allowed or ())
    result: list[str] = []
    for item in values:
        value = _text(item)
        if not value or value in result or (allowed_set and value not in allowed_set):
            continue
        result.append(value)
    return result


def stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def normalize_work_draft(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe WorkDraft using only the Phase 1 contract fields."""
    work_type = _text(value.get("work_type")) or "其他高风险作业"
    if work_type not in WORK_TYPES:
        work_type = "其他高风险作业"
    try:
        people_count = max(int(value.get("people_count") or 0), 0)
    except (TypeError, ValueError):
        people_count = 0
    status = _text(value.get("status")) or STATUS_DRAFT
    if status not in STATUSES:
        status = STATUS_DRAFT
    return {
        "id": _text(value.get("id")),
        "title": _text(value.get("title")),
        "work_type": work_type,
        "description": _text(value.get("description")),
        "location": _text(value.get("location")),
        "planned_start": _text(value.get("planned_start")),
        "planned_end": _text(value.get("planned_end")),
        "people_count": people_count,
        "responsible_person": _text(value.get("responsible_person")),
        "contractor_involved": bool(value.get("contractor_involved", False)),
        "work_steps": _unique(value.get("work_steps") or ()),
        "chemicals": _unique(value.get("chemicals") or ()),
        "user_risk_tags": _unique(value.get("user_risk_tags") or (), RISK_TAGS),
        "ai_risk_tags": _unique(value.get("ai_risk_tags") or (), RISK_TAGS),
        "confirmed_risk_tags": _unique(value.get("confirmed_risk_tags") or (), RISK_TAGS),
        "documents": [dict(item) for item in (value.get("documents") or ()) if isinstance(item, Mapping)],
        "status": status,
        "created_by": _text(value.get("created_by")),
        "created_at": _text(value.get("created_at")),
        "updated_at": _text(value.get("updated_at")),
    }


def missing_intake_fields(draft: Mapping[str, Any]) -> list[str]:
    """List required intake fields absent from a draft."""
    normalized = normalize_work_draft(draft)
    checks = (
        ("作业名称", normalized["title"]),
        ("作业描述", normalized["description"]),
        ("作业地点", normalized["location"]),
        ("计划开始时间", normalized["planned_start"]),
        ("计划结束时间", normalized["planned_end"]),
        ("作业人数", normalized["people_count"] > 0),
        ("负责人", normalized["responsible_person"]),
        ("作业步骤", normalized["work_steps"]),
    )
    return [label for label, present in checks if not present]


def deterministic_precheck(draft: Mapping[str, Any]) -> dict[str, Any]:
    """Rules-only fallback: classify work/risk without producing safety advice."""
    normalized = normalize_work_draft(draft)
    corpus = " ".join(
        [normalized["title"], normalized["description"]]
        + normalized["work_steps"]
        + normalized["chemicals"]
    ).casefold()
    suggested_type = "其他高风险作业"
    for work_type, keywords in _WORK_TYPE_KEYWORDS:
        if any(keyword.casefold() in corpus for keyword in keywords):
            suggested_type = work_type
            break
    suggested_tags = [
        tag
        for tag, keywords in _RISK_KEYWORDS
        if any(keyword.casefold() in corpus for keyword in keywords)
    ]
    if normalized["contractor_involved"] and "承包商作业" not in suggested_tags:
        suggested_tags.append("承包商作业")
    missing = missing_intake_fields(normalized)
    questions = [f"请补充：{item}。" for item in missing]
    if normalized["chemicals"] and not any(
        str(item.get("source_type", "")) == "sds" for item in normalized["documents"]
    ):
        questions.append("请为已声明化学品选择或上传适用SDS。")
    return {
        "mode": "rules_retrieval",
        "suggested_work_type": suggested_type,
        "suggested_risk_tags": suggested_tags,
        "missing_fields": missing,
        "recommended_questions": questions,
    }


def normalize_citation(value: Mapping[str, Any]) -> dict[str, Any]:
    source_type = _text(value.get("source_type")) or "internal"
    if source_type not in SOURCE_TYPES:
        source_type = "internal"
    try:
        page = int(value.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    return {
        "evidence_id": _text(value.get("evidence_id")),
        "source_type": source_type,
        "source_name": _text(value.get("source_name") or value.get("source")),
        "version": _text(value.get("version")),
        "page": page,
        "snippet": " ".join(_text(value.get("snippet")).split()),
        "chunk_id": _text(value.get("chunk_id")),
        "locator": _text(value.get("locator")),
    }


def citation_is_complete(citation: Mapping[str, Any]) -> bool:
    item = normalize_citation(citation)
    if item["source_type"] == "user_input":
        return bool(item["evidence_id"] and item["snippet"] and item["locator"])
    if item["source_type"] in DOCUMENT_SOURCE_TYPES:
        return bool(
            item["evidence_id"]
            and item["source_name"]
            and item["page"] > 0
            and item["snippet"]
        )
    return False


def make_pack_item(
    text: object,
    *,
    origin: str = "ai",
    evidence_refs: Iterable[object] = (),
    requires_confirmation: bool = True,
) -> dict[str, Any]:
    return {
        "text": _text(text),
        "origin": origin if origin in {"user_input", "ai", "human"} else "ai",
        "evidence_refs": _unique(evidence_refs),
        "requires_confirmation": bool(requires_confirmation),
    }


def make_jsa_item(
    *,
    step_no: int,
    work_step: str,
    hazard: str,
    consequence: str = "",
    existing_controls: str = "",
    proposed_controls: str = "",
    evidence_refs: Iterable[object] = (),
) -> dict[str, Any]:
    """Create an unrated JSA candidate; AI-originated L/S are always blank."""
    return {
        "step_no": max(int(step_no), 1),
        "work_step": _text(work_step),
        "hazard": _text(hazard),
        "consequence": _text(consequence),
        "existing_controls": _text(existing_controls),
        "proposed_controls": _text(proposed_controls),
        "likelihood": None,
        "severity": None,
        "risk_score": None,
        "risk_level": "",
        "residual_likelihood": None,
        "residual_severity": None,
        "residual_risk_score": None,
        "residual_risk_level": "",
        "evidence_refs": _unique(evidence_refs),
        "confirmed_by": "",
    }


def apply_human_risk_rating(
    item: Mapping[str, Any],
    *,
    likelihood: int,
    severity: int,
    residual_likelihood: int,
    residual_severity: int,
    confirmed_by: str,
) -> dict[str, Any]:
    """Apply human L/S values using the existing shared JSA calculator."""
    result = deepcopy(dict(item))
    initial_score, initial_level = calculate_risk(int(likelihood), int(severity))
    residual_score, residual_level = calculate_risk(
        int(residual_likelihood), int(residual_severity)
    )
    result.update(
        {
            "likelihood": int(likelihood),
            "severity": int(severity),
            "risk_score": initial_score,
            "risk_level": initial_level,
            "residual_likelihood": int(residual_likelihood),
            "residual_severity": int(residual_severity),
            "residual_risk_score": residual_score,
            "residual_risk_level": residual_level,
            "confirmed_by": _text(confirmed_by),
        }
    )
    return result


def _evidence_map(pack: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["evidence_id"]: item
        for item in (
            normalize_citation(value)
            for value in (pack.get("evidence") or ())
            if isinstance(value, Mapping)
        )
        if item["evidence_id"]
    }


def pack_blockers(draft: Mapping[str, Any], pack: Mapping[str, Any]) -> list[str]:
    """Return every hard blocker that prevents EHS confirmation."""
    normalized = normalize_work_draft(draft)
    blockers = [f"缺少必填信息：{item}" for item in missing_intake_fields(normalized)]
    if not normalized["confirmed_risk_tags"]:
        blockers.append("风险标签尚未由人工确认")

    evidence = _evidence_map(pack)
    for item in evidence.values():
        if not citation_is_complete(item):
            blockers.append(f"Citation不完整：{item['evidence_id']}")

    sds_items = [item for item in evidence.values() if item["source_type"] == "sds"]
    if normalized["chemicals"] and not sds_items:
        blockers.append("作业涉及化学品，但没有匹配且可引用的SDS")

    def supported(item: Mapping[str, Any], allowed_types: set[str]) -> bool:
        refs = [str(value) for value in (item.get("evidence_refs") or ())]
        return any(
            ref in evidence and evidence[ref]["source_type"] in allowed_types
            for ref in refs
        )

    for index, item in enumerate(pack.get("controls") or (), start=1):
        if isinstance(item, Mapping) and not supported(item, {"sds", "sop", "internal"}):
            blockers.append(f"控制措施第{index}项缺少SDS、SOP或内部资料依据")
    if normalized["chemicals"]:
        for section_key, label in (
            ("ppe", "PPE"),
            ("emergency_requirements", "应急要求"),
        ):
            values = [
                item for item in (pack.get(section_key) or ()) if isinstance(item, Mapping)
            ]
            if not values:
                blockers.append(f"化学品相关{label}缺少匹配SDS证据")
            for index, item in enumerate(values, start=1):
                if not supported(item, {"sds"}):
                    blockers.append(f"{label}第{index}项缺少匹配SDS依据")

    for missing in pack.get("missing_items") or ():
        text = _text(missing.get("text") if isinstance(missing, Mapping) else missing)
        if text:
            blockers.append(text)
    for conflict in pack.get("conflicts") or ():
        if not isinstance(conflict, Mapping):
            blockers.append(_text(conflict))
            continue
        if not conflict.get("resolved"):
            blockers.append(_text(conflict.get("text")) or "存在未处理的资料冲突")

    if bool(pack.get("evidence_insufficient")):
        blockers.append("Evidence insufficient：证据不足，不能确认审核包")

    jsa_items = [dict(item) for item in (pack.get("jsa_draft") or ()) if isinstance(item, Mapping)]
    if not jsa_items:
        blockers.append("缺少JSA草稿")
    for index, item in enumerate(jsa_items, start=1):
        if not _text(item.get("work_step")) or not _text(item.get("hazard")):
            blockers.append(f"JSA第{index}项结构不完整")
        values = (
            item.get("likelihood"),
            item.get("severity"),
            item.get("residual_likelihood"),
            item.get("residual_severity"),
        )
        if any(value in (None, "") for value in values) or not _text(item.get("confirmed_by")):
            blockers.append(f"JSA第{index}项尚未由EHS填写并确认L/S与残余L/S")
            continue
        try:
            expected = apply_human_risk_rating(
                item,
                likelihood=int(item["likelihood"]),
                severity=int(item["severity"]),
                residual_likelihood=int(item["residual_likelihood"]),
                residual_severity=int(item["residual_severity"]),
                confirmed_by=_text(item.get("confirmed_by")),
            )
        except (TypeError, ValueError):
            blockers.append(f"JSA第{index}项L/S必须为1–5")
            continue
        if any(item.get(key) != expected[key] for key in (
            "risk_score", "risk_level", "residual_risk_score", "residual_risk_level"
        )):
            blockers.append(f"JSA第{index}项风险值与L×S不一致")
        if expected["risk_level"] in {"高风险", "重大风险"}:
            if not _text(item.get("proposed_controls")):
                blockers.append(f"JSA第{index}项高/重大风险缺少控制措施确认")
            if not supported(item, {"sds", "sop", "internal"}):
                blockers.append(f"JSA第{index}项高/重大风险控制措施缺少资料依据")

    return _unique(blockers)


def pack_can_confirm(draft: Mapping[str, Any], pack: Mapping[str, Any]) -> bool:
    return not pack_blockers(draft, pack)


__all__ = [
    "DOCUMENT_SOURCE_TYPES",
    "RISK_TAGS",
    "SOURCE_TYPES",
    "STATUSES",
    "STATUS_CONFIRMED",
    "STATUS_DRAFT",
    "STATUS_LABELS",
    "STATUS_NEEDS_INPUT",
    "STATUS_REVIEW_READY",
    "WORK_TYPES",
    "apply_human_risk_rating",
    "citation_is_complete",
    "deterministic_precheck",
    "make_jsa_item",
    "make_pack_item",
    "missing_intake_fields",
    "normalize_citation",
    "normalize_work_draft",
    "pack_blockers",
    "pack_can_confirm",
    "stamp",
]

"""Hazard management tools.

Thin wrappers over :mod:`hazards`.  Validation, ID generation and record
mutation stay in :mod:`hazards`; this module only normalises the loose
natural-language arguments and shapes the response.
"""

from __future__ import annotations

from datetime import date, timedelta

import hazards

from .context import ToolContext


CREATE_HAZARD_TOOL = "create_hazard"
UPDATE_HAZARD_TOOL = "update_hazard"

_RISK_ALIASES = {
    "低": "低",
    "低风险": "低",
    "中": "中",
    "中风险": "中",
    "高": "高",
    "高风险": "高",
    "重大": "重大",
    "重大风险": "重大",
}

_TYPE_KEYWORDS = (
    ("危化品管理", ("危化品", "化学品", "试剂", "酸碱")),
    ("PPE", ("ppe", "防护用品", "个体防护", "劳保")),
    ("消防", ("消防", "灭火", "火灾", "通道占用")),
    ("电气安全", ("电气", "配电", "用电", "漏电")),
    ("设备安全", ("设备", "机械", "点检")),
    ("作业现场", ("作业现场", "现场", "通道", "标识")),
    ("环境管理", ("环境", "废液", "固废", "废气", "排放")),
)


def normalize_hazard_type(value: str) -> str:
    """Map loose text onto one of ``hazards.HAZARD_TYPES``."""
    text = str(value or "").strip()
    if text in hazards.HAZARD_TYPES:
        return text
    lowered = text.casefold()
    for hazard_type, keywords in _TYPE_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return hazard_type
    return "其他"


def normalize_risk_level(value: str) -> str:
    """Map loose text onto one of ``hazards.RISK_LEVELS``."""
    return _RISK_ALIASES.get(str(value or "").strip(), "")


def _coerce_date(value: object, fallback: date) -> date | str:
    if value is None or str(value).strip() == "":
        return fallback
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return fallback


def create_hazard(
    context: ToolContext,
    *,
    description: str,
    hazard_type: str = "其他",
    risk_level: str = "中",
    owner: str = "待分配（AI工作流）",
    found_on: object = None,
    due_on: object = None,
    corrective_action: str = "",
    status: str = "待整改",
    hazard_id: str = "",
    store: bool = True,
) -> dict[str, object]:
    """Create one hazard record and (by default) append it to the session table."""
    found = _coerce_date(found_on, date.today())
    due = _coerce_date(due_on, found + timedelta(days=14))
    normalized_type = normalize_hazard_type(hazard_type)
    normalized_risk = normalize_risk_level(risk_level) or "中"
    normalized_status = (
        status if status in hazards.HAZARD_STATUSES else "待整改"
    )
    identifier = str(hazard_id or "").strip() or hazards.next_hazard_id(
        context.hazard_records
    )

    try:
        record = hazards.create_hazard_record(
            hazard_id=identifier,
            description=str(description or "").strip() or "待补充隐患描述（AI 工作流草稿）",
            hazard_type=normalized_type,
            risk_level=normalized_risk,
            owner=str(owner or "").strip() or "待分配（AI工作流）",
            found_on=found,
            due_on=due,
            corrective_action=str(corrective_action or "").strip()
            or "待补充整改措施（AI 工作流草稿，需人工确认）。",
            status=normalized_status,
            existing_ids=(
                str(item.get("隐患编号", "")) for item in context.hazard_records
            ),
        )
    except ValueError as exc:
        return {
            "tool": CREATE_HAZARD_TOOL,
            "status": "error",
            "message": str(exc),
            "hazard_id": identifier,
        }

    if store:
        context.hazard_records.append(record)

    return {
        "tool": CREATE_HAZARD_TOOL,
        "status": "ok",
        "hazard_id": record["隐患编号"],
        "record": record,
        "risk_level": record["风险等级"],
        "hazard_type": record["隐患类型"],
        "state": record["状态"],
        "stored": bool(store),
        "hazard_record_count": len(context.hazard_records),
    }


def update_hazard(
    context: ToolContext,
    *,
    hazard_id: str,
    status: str = "",
    risk_level: str = "",
    owner: str = "",
    description: str = "",
    hazard_type: str = "",
    corrective_action: str = "",
    due_on: object = None,
) -> dict[str, object]:
    """Update one existing hazard record in place and return the new values."""
    identifier = str(hazard_id or "").strip()
    updates: dict[str, object] = {}
    if status:
        updates["状态"] = status
    if risk_level:
        updates["风险等级"] = normalize_risk_level(risk_level) or risk_level
    if owner:
        updates["责任人"] = owner
    if description:
        updates["隐患描述"] = description
    if hazard_type:
        updates["隐患类型"] = normalize_hazard_type(hazard_type)
    if corrective_action:
        updates["整改措施"] = corrective_action
    if due_on not in (None, ""):
        updates["整改期限"] = _coerce_date(due_on, date.today())

    if not updates:
        return {
            "tool": UPDATE_HAZARD_TOOL,
            "status": "error",
            "message": "未提供任何要更新的字段。",
            "hazard_id": identifier,
        }

    try:
        record = hazards.update_hazard_record(
            context.hazard_records, identifier, updates
        )
    except KeyError as exc:
        return {
            "tool": UPDATE_HAZARD_TOOL,
            "status": "error",
            "message": str(exc).strip("'\""),
            "hazard_id": identifier,
        }
    except ValueError as exc:
        return {
            "tool": UPDATE_HAZARD_TOOL,
            "status": "error",
            "message": str(exc),
            "hazard_id": identifier,
        }

    return {
        "tool": UPDATE_HAZARD_TOOL,
        "status": "ok",
        "hazard_id": identifier,
        "changed_fields": sorted(updates),
        "record": record,
        "state": record["状态"],
        "risk_level": record["风险等级"],
    }

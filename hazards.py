"""Session-only helpers for EHS hazard corrective-action management."""

from __future__ import annotations

import csv
import re
from collections import Counter
from datetime import date, datetime, timedelta
from io import StringIO
from typing import Iterable, Mapping, MutableSequence


HAZARD_TYPES = (
    "危化品管理",
    "PPE",
    "消防",
    "电气安全",
    "设备安全",
    "作业现场",
    "环境管理",
    "其他",
)
RISK_LEVELS = ("低", "中", "高", "重大")
HAZARD_STATUSES = ("待整改", "整改中", "已关闭")
DEMO_DATA_LABEL = "模拟数据 / Demo，不代表真实企业记录"
USER_DATA_LABEL = "用户会话记录"

HAZARD_FIELDS = (
    "隐患编号",
    "隐患描述",
    "隐患类型",
    "风险等级",
    "责任人",
    "发现日期",
    "整改期限",
    "整改措施",
    "状态",
)


def _normalise_date(value: object, field_name: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name}必须为有效日期。") from exc


def validate_hazard_record(
    record: Mapping[str, object],
    *,
    existing_ids: Iterable[str] = (),
    current_id: str | None = None,
) -> tuple[str, ...]:
    """Return validation errors without mutating the supplied record."""
    errors: list[str] = []
    for field in HAZARD_FIELDS:
        if field not in record or not str(record[field]).strip():
            errors.append(f"{field}不能为空。")

    hazard_id = str(record.get("隐患编号", "")).strip()
    duplicate_ids = {str(value).strip() for value in existing_ids}
    if hazard_id and hazard_id in duplicate_ids and hazard_id != current_id:
        errors.append("隐患编号已存在。")

    if record.get("隐患类型") not in HAZARD_TYPES:
        errors.append("隐患类型不在允许范围内。")
    if record.get("风险等级") not in RISK_LEVELS:
        errors.append("风险等级不在允许范围内。")
    if record.get("状态") not in HAZARD_STATUSES:
        errors.append("状态不在允许范围内。")

    try:
        found_on = date.fromisoformat(str(record.get("发现日期", "")).strip())
        due_on = date.fromisoformat(str(record.get("整改期限", "")).strip())
        if due_on < found_on:
            errors.append("整改期限不能早于发现日期。")
    except ValueError:
        errors.append("发现日期和整改期限必须为有效日期。")

    return tuple(dict.fromkeys(errors))


def create_hazard_record(
    *,
    hazard_id: str,
    description: str,
    hazard_type: str,
    risk_level: str,
    owner: str,
    found_on: object,
    due_on: object,
    corrective_action: str,
    status: str,
    data_label: str = USER_DATA_LABEL,
    existing_ids: Iterable[str] = (),
) -> dict[str, object]:
    """Create and validate a dashboard-ready hazard record."""
    record: dict[str, object] = {
        "隐患编号": str(hazard_id).strip(),
        "隐患描述": str(description).strip(),
        "隐患类型": hazard_type,
        "风险等级": risk_level,
        "责任人": str(owner).strip(),
        "发现日期": _normalise_date(found_on, "发现日期"),
        "整改期限": _normalise_date(due_on, "整改期限"),
        "整改措施": str(corrective_action).strip(),
        "状态": status,
        "数据性质": str(data_label).strip() or USER_DATA_LABEL,
    }
    errors = validate_hazard_record(record, existing_ids=existing_ids)
    if errors:
        raise ValueError("；".join(errors))
    return record


def update_hazard_record(
    records: MutableSequence[dict[str, object]],
    hazard_id: str,
    updates: Mapping[str, object],
) -> dict[str, object]:
    """Validate and update one record in place, returning the updated record."""
    target = next(
        (record for record in records if record.get("隐患编号") == hazard_id),
        None,
    )
    if target is None:
        raise KeyError(f"未找到隐患记录：{hazard_id}")

    candidate = dict(target)
    candidate.update(updates)
    candidate["隐患编号"] = hazard_id
    if "发现日期" in candidate:
        candidate["发现日期"] = _normalise_date(candidate["发现日期"], "发现日期")
    if "整改期限" in candidate:
        candidate["整改期限"] = _normalise_date(candidate["整改期限"], "整改期限")

    errors = validate_hazard_record(
        candidate,
        existing_ids=(str(record.get("隐患编号", "")) for record in records),
        current_id=hazard_id,
    )
    if errors:
        raise ValueError("；".join(errors))
    target.clear()
    target.update(candidate)
    return target


def delete_hazard_record(
    records: MutableSequence[dict[str, object]], hazard_id: str
) -> bool:
    """Delete one record by ID and report whether a record was removed."""
    for index, record in enumerate(records):
        if record.get("隐患编号") == hazard_id:
            records.pop(index)
            return True
    return False


def calculate_completion_rate(records: Iterable[Mapping[str, object]]) -> float:
    """Return closed / total × 100, rounded to one decimal place."""
    rows = list(records)
    if not rows:
        return 0.0
    closed = sum(record.get("状态") == "已关闭" for record in rows)
    return round(closed / len(rows) * 100, 1)


def calculate_hazard_summary(
    records: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Return headline metrics and distributions reusable by a dashboard."""
    rows = list(records)
    status_distribution = Counter(str(row.get("状态", "")) for row in rows)
    risk_distribution = Counter(str(row.get("风险等级", "")) for row in rows)
    type_distribution = Counter(str(row.get("隐患类型", "")) for row in rows)
    return {
        "total": len(rows),
        "pending": status_distribution.get("待整改", 0),
        "in_progress": status_distribution.get("整改中", 0),
        "closed": status_distribution.get("已关闭", 0),
        "completion_rate": calculate_completion_rate(rows),
        "status_distribution": {
            status: status_distribution.get(status, 0) for status in HAZARD_STATUSES
        },
        "risk_distribution": {
            level: risk_distribution.get(level, 0) for level in RISK_LEVELS
        },
        "type_distribution": {
            hazard_type: type_distribution.get(hazard_type, 0)
            for hazard_type in HAZARD_TYPES
        },
    }


def next_hazard_id(records: Iterable[Mapping[str, object]]) -> str:
    """Generate the next session-only HZ-### identifier."""
    numbers: list[int] = []
    for record in records:
        match = re.fullmatch(r"HZ-(\d+)", str(record.get("隐患编号", "")))
        if match:
            numbers.append(int(match.group(1)))
    return f"HZ-{max(numbers, default=0) + 1:03d}"


def hazards_to_csv(records: Iterable[Mapping[str, object]]) -> bytes:
    """Serialize records as an Excel-friendly UTF-8 CSV."""
    rows = list(records)
    if not rows:
        return b""
    fieldnames = list(HAZARD_FIELDS)
    if any("数据性质" in row for row in rows):
        fieldnames.append("数据性质")
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def create_demo_hazard_records(today: date | None = None) -> list[dict[str, object]]:
    """Return eight clearly labelled synthetic EHS hazard records."""
    base = today or date.today()
    cases = (
        ("HF使用区域PPE配置不完整", "PPE", "重大", "待整改", "补充并核对适用PPE配置"),
        ("挥发性试剂使用后未及时密闭", "危化品管理", "高", "整改中", "提醒使用后立即密闭并复核"),
        ("危化品领用记录缺失", "危化品管理", "高", "待整改", "补录信息并核对领用记录"),
        ("酸碱废液标签信息不完整", "环境管理", "中", "整改中", "完善标签内容并检查同类容器"),
        ("消防通道存在临时物品占用", "消防", "高", "已关闭", "移除占用物并恢复通道畅通"),
        ("配电箱前堆放杂物", "电气安全", "高", "待整改", "清理配电箱前方区域"),
        ("设备点检记录未及时更新", "设备安全", "中", "已关闭", "补充点检记录并确认更新频次"),
        ("实验固废分类暂存不规范", "环境管理", "中", "待整改", "按类别重新整理暂存区域"),
    )
    records: list[dict[str, object]] = []
    for index, (description, hazard_type, risk, status, action) in enumerate(cases, 1):
        found_on = base - timedelta(days=9 - index)
        due_on = found_on + timedelta(days=7 if risk in {"高", "重大"} else 14)
        records.append(
            create_hazard_record(
                hazard_id=f"DEMO-HZ-{index:03d}",
                description=description,
                hazard_type=hazard_type,
                risk_level=risk,
                owner="待分配（Demo）",
                found_on=found_on,
                due_on=due_on,
                corrective_action=f"模拟整改措施：{action}",
                status=status,
                data_label=DEMO_DATA_LABEL,
            )
        )
    return records

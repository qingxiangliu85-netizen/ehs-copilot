"""Session-only domain core for V4 chemical non-routine job records.

This module is intentionally pure: it has no Streamlit, LLM or LangGraph
dependency.  It owns the job record schema, the job status machine and the
validation every transition must pass, so the same rules hold whether a
transition is triggered from the UI, the workflow graph or a test.

V4 business main line::

    草稿 → 待EHS确认 → 待审批 → 已批准 → 执行中 → 待复查 → 已关闭

with two additional branches:

    * 待EHS确认 → 草稿（退回修改）
    * 待审批 → 已驳回（审批不通过）

Risk values (L/S and the derived level), approval outcomes and closure are
deliberately *not* decided here.  They are produced by humans and/or the
existing deterministic module (``jsa.calculate_risk``); this module only stores
the resulting values on the job record.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Iterable, Mapping, MutableSequence

from hazards import USER_DATA_LABEL


JOB_STATUS_DRAFT = "草稿"
JOB_STATUS_AWAITING_EHS = "待EHS确认"
JOB_STATUS_AWAITING_APPROVAL = "待审批"
JOB_STATUS_APPROVED = "已批准"
JOB_STATUS_EXECUTING = "执行中"
JOB_STATUS_AWAITING_REVIEW = "待复查"
JOB_STATUS_CLOSED = "已关闭"
JOB_STATUS_REJECTED = "已驳回"

JOB_STATUSES: tuple[str, ...] = (
    JOB_STATUS_DRAFT,
    JOB_STATUS_AWAITING_EHS,
    JOB_STATUS_AWAITING_APPROVAL,
    JOB_STATUS_APPROVED,
    JOB_STATUS_EXECUTING,
    JOB_STATUS_AWAITING_REVIEW,
    JOB_STATUS_CLOSED,
    JOB_STATUS_REJECTED,
)

JOB_TRANSITIONS: dict[str, tuple[str, ...]] = {
    JOB_STATUS_DRAFT: (JOB_STATUS_AWAITING_EHS,),
    JOB_STATUS_AWAITING_EHS: (JOB_STATUS_AWAITING_APPROVAL, JOB_STATUS_DRAFT),
    JOB_STATUS_AWAITING_APPROVAL: (JOB_STATUS_APPROVED, JOB_STATUS_REJECTED),
    JOB_STATUS_APPROVED: (JOB_STATUS_EXECUTING,),
    JOB_STATUS_EXECUTING: (JOB_STATUS_AWAITING_REVIEW,),
    JOB_STATUS_AWAITING_REVIEW: (JOB_STATUS_CLOSED,),
    JOB_STATUS_CLOSED: (),
    JOB_STATUS_REJECTED: (),
}

JOB_TERMINAL_STATUSES: tuple[str, ...] = (JOB_STATUS_CLOSED, JOB_STATUS_REJECTED)

DEFAULT_JOB_TYPE = "危化品非例行作业"

JOB_FIELDS: tuple[str, ...] = (
    "job_id",
    "job_name",
    "job_type",
    "status",
    "chemicals",
    "steps",
    "public_evidence",
    "sds_evidence",
    "jsa_draft",
    "jsa_confirmation",
    "approvals",
    "execution_info",
    "linked_hazard_ids",
    "attachments",
    "closure_info",
    "status_history",
    "data_label",
    "created_at",
    "updated_at",
)


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _next_job_id(values: Iterable[object]) -> str:
    numbers: list[int] = []
    for value in values:
        match = re.fullmatch(r"JOB-(\d+)", str(value).strip())
        if match:
            numbers.append(int(match.group(1)))
    return f"JOB-{max(numbers, default=0) + 1:03d}"


def _as_dict_list(
    values: Iterable[Mapping[str, object]] | None,
    field_name: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, item in enumerate(list(values or ()), start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"{field_name} 第 {index} 项必须是一个对象。")
        rows.append(dict(item))
    return rows


def _normalise_chemicals(
    chemicals: Iterable[Mapping[str, object]] | None,
) -> list[dict[str, object]]:
    rows = _as_dict_list(chemicals, "chemicals")
    for index, row in enumerate(rows, start=1):
        name = str(row.get("name", "") or "").strip()
        if not name:
            raise ValueError(f"chemicals 第 {index} 项缺少化学品名称（name）。")
        aliases = row.get("aliases") or []
        if not isinstance(aliases, (list, tuple)):
            raise ValueError(f"chemicals 第 {index} 项的 aliases 必须是列表。")
        row["name"] = name
        row["sds_file"] = str(row.get("sds_file", "") or "").strip()
        row["aliases"] = [
            str(alias).strip() for alias in aliases if str(alias).strip()
        ]
    return rows


def _normalise_steps(
    steps: Iterable[Mapping[str, object]] | None,
) -> list[dict[str, object]]:
    rows = _as_dict_list(steps, "steps")
    for index, row in enumerate(rows, start=1):
        name = str(row.get("name", "") or row.get("步骤", "") or "").strip()
        if not name:
            raise ValueError(f"steps 第 {index} 项缺少步骤名称（name）。")
        row["name"] = name
        row["note"] = str(row.get("note", "") or "").strip()
        row.setdefault("order", index)
    return rows


def next_job_id(records: Iterable[Mapping[str, object]]) -> str:
    """Generate the next session-only ``JOB-###`` identifier."""
    return _next_job_id(record.get("job_id", "") for record in records)


def create_job(
    *,
    job_name: str,
    job_type: str = DEFAULT_JOB_TYPE,
    job_id: str = "",
    chemicals: Iterable[Mapping[str, object]] | None = None,
    steps: Iterable[Mapping[str, object]] | None = None,
    sds_evidence: Iterable[Mapping[str, object]] | None = None,
    jsa_draft: Mapping[str, object] | None = None,
    created_by: str = "",
    data_label: str = USER_DATA_LABEL,
    existing_ids: Iterable[str] = (),
    now: datetime | None = None,
) -> dict[str, object]:
    """Create one validated job record in the initial ``草稿`` status.

    ``chemicals`` entries follow ``{"name", "sds_file", "aliases"}``; ``steps``
    entries follow ``{"order", "name", "note"}``; the ``sds_evidence`` entries
    keep the shape produced by ``search_sds`` (``source`` / ``page`` /
    ``sections`` / ``snippet``).  Public-source evidence is attached later via
    ``job_review.attach_public_evidence`` so the two tracks stay separate.
    """
    name = str(job_name or "").strip()
    if not name:
        raise ValueError("job_name（作业名称）不能为空。")
    kind = str(job_type or "").strip()
    if not kind:
        raise ValueError("job_type（作业类型）不能为空。")

    known_ids = [str(value).strip() for value in existing_ids if str(value).strip()]
    identifier = str(job_id or "").strip() or _next_job_id(known_ids)
    if identifier in known_ids:
        raise ValueError("作业编号已存在。")

    if jsa_draft is not None and not isinstance(jsa_draft, Mapping):
        raise ValueError("jsa_draft 必须是一个对象。")

    created_at = _timestamp(now)
    record: dict[str, object] = {
        "job_id": identifier,
        "job_name": name,
        "job_type": kind,
        "status": JOB_STATUS_DRAFT,
        "chemicals": _normalise_chemicals(chemicals),
        "steps": _normalise_steps(steps),
        "public_evidence": [],
        "sds_evidence": _as_dict_list(sds_evidence, "sds_evidence"),
        "jsa_draft": dict(jsa_draft) if jsa_draft is not None else None,
        "jsa_confirmation": None,
        "approvals": [],
        "execution_info": {},
        "linked_hazard_ids": [],
        "attachments": [],
        "closure_info": {},
        "status_history": [
            {
                "from": "",
                "to": JOB_STATUS_DRAFT,
                "actor": str(created_by or "").strip(),
                "note": "创建作业单",
                "at": created_at,
            }
        ],
        "data_label": str(data_label).strip() or USER_DATA_LABEL,
        "created_at": created_at,
        "updated_at": created_at,
    }
    return record


def get_job(
    records: Iterable[Mapping[str, object]],
    job_id: str,
) -> dict[str, object] | None:
    """Return the live job record identified by ``job_id``, or ``None``.

    The live record is returned (not a copy) so callers such as
    :func:`transition_job` can update it in place.
    """
    target = str(job_id or "").strip()
    for record in records:
        if str(record.get("job_id", "")).strip() == target:
            return record  # type: ignore[return-value]
    return None


def can_transition(current_status: str, next_status: str) -> bool:
    """Return whether the status machine allows ``current → next``."""
    return str(next_status) in JOB_TRANSITIONS.get(str(current_status), ())


def transition_job(
    records: MutableSequence[dict[str, object]],
    job_id: str,
    next_status: str,
    *,
    actor: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    """Apply one validated status transition in place and return the record.

    Every transition records ``actor``, ``note`` and the timestamp in
    ``status_history``.  Unknown states, missing actors, unknown job ids and
    illegal transitions are rejected without touching the record.
    """
    target = get_job(records, job_id)
    if target is None:
        raise KeyError(f"未找到作业单：{job_id}")

    current = str(target.get("status", "")).strip()
    if current not in JOB_STATUSES:
        raise ValueError(f"作业单 {job_id} 的当前状态无法识别：{current!r}")

    desired = str(next_status or "").strip()
    if desired not in JOB_STATUSES:
        raise ValueError(f"未知作业状态：{next_status!r}")

    operator = str(actor or "").strip()
    if not operator:
        raise ValueError("状态迁移必须记录操作人（actor）。")

    if not can_transition(current, desired):
        raise ValueError(f"非法状态流转：{current} → {desired}。")

    stamp = _timestamp(now)
    target["status"] = desired
    target["updated_at"] = stamp
    history = target.setdefault("status_history", [])
    if not isinstance(history, list):
        raise ValueError("status_history 必须是列表。")
    history.append(
        {
            "from": current,
            "to": desired,
            "actor": operator,
            "note": str(note or "").strip(),
            "at": stamp,
        }
    )
    return target


def job_summary(records: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Return dashboard-ready job metrics and the status distribution."""
    rows = list(records)
    distribution = Counter(str(row.get("status", "")) for row in rows)
    closed = distribution.get(JOB_STATUS_CLOSED, 0)
    rejected = distribution.get(JOB_STATUS_REJECTED, 0)
    linked_hazards = sum(
        len(list(row.get("linked_hazard_ids") or ())) for row in rows
    )
    total = len(rows)
    return {
        "total": total,
        "active": total - closed - rejected,
        "closed": closed,
        "rejected": rejected,
        "awaiting_approval": distribution.get(JOB_STATUS_AWAITING_APPROVAL, 0),
        "awaiting_review": distribution.get(JOB_STATUS_AWAITING_REVIEW, 0),
        "completion_rate": round(closed / total * 100, 1) if total else 0.0,
        "status_distribution": {
            status: distribution.get(status, 0) for status in JOB_STATUSES
        },
        "linked_hazard_total": linked_hazards,
    }


__all__ = [
    "DEFAULT_JOB_TYPE",
    "JOB_FIELDS",
    "JOB_STATUSES",
    "JOB_STATUS_APPROVED",
    "JOB_STATUS_AWAITING_APPROVAL",
    "JOB_STATUS_AWAITING_EHS",
    "JOB_STATUS_AWAITING_REVIEW",
    "JOB_STATUS_CLOSED",
    "JOB_STATUS_DRAFT",
    "JOB_STATUS_EXECUTING",
    "JOB_STATUS_REJECTED",
    "JOB_TERMINAL_STATUSES",
    "JOB_TRANSITIONS",
    "can_transition",
    "create_job",
    "get_job",
    "job_summary",
    "next_job_id",
    "transition_job",
]

"""Second half of the job lifecycle: execution completion, linked hazards,
rectification, EHS review and closure.

Flow::

    已批准
      → start_job_execution（job_review）        → 执行中
      → 执行中发现隐患 → create_job_hazard       （人工审批 + Guardrail）
      → 指定责任人与期限 → update_hazard_rectification
      → 上传整改证据   → submit_rectification_evidence
      → EHS 复查       → review_hazard
      → 关闭隐患       → close_hazard（必须有证据 + 复查人 + 复查意见）
      → 执行完成       → complete_job_execution   → 待复查
      → 全部关联隐患关闭 → close_job              → 已关闭

Design rules:

* every job status change goes through ``jobs.transition_job``;
* hazard writes reuse ``workflow.guardrails`` findings and record the human
  approval on the job, so the V3 approval/guardrail logic is preserved;
* closing a hazard requires rectification evidence, a reviewer and a review
  note; closing a job requires every linked hazard to be closed first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, MutableSequence

import hazards
from hazards import RISK_LEVELS
from jobs import (
    JOB_STATUS_AWAITING_REVIEW,
    JOB_STATUS_CLOSED,
    JOB_STATUS_EXECUTING,
    get_job,
    transition_job,
)

from workflow import guardrails, hitl


RECTIFICATION_EVIDENCE_TYPES: tuple[str, ...] = (
    "照片",
    "记录",
    "检测报告",
    "验收单",
    "培训记录",
    "其他",
)

_WORK_JOB_STATUSES = (JOB_STATUS_EXECUTING, JOB_STATUS_AWAITING_REVIEW)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now()).isoformat(timespec="seconds")


def _job_or_raise(
    job_records: MutableSequence[dict[str, object]], job_id: str
) -> dict[str, object]:
    job = get_job(job_records, job_id)
    if job is None:
        raise KeyError(f"未找到作业单：{job_id}")
    return job


def _require_workable_job(job: Mapping[str, object]) -> None:
    status = str(job.get("status", ""))
    if status not in _WORK_JOB_STATUSES:
        raise ValueError(
            "作业单当前状态不允许整改操作："
            f"{status!r}；须处于「执行中」或「待复查」阶段。"
        )


def _find_hazard(
    hazard_records: MutableSequence[dict[str, object]], hazard_id: str
) -> dict[str, object] | None:
    target = str(hazard_id or "").strip()
    for record in hazard_records:
        if str(record.get("隐患编号", "")).strip() == target:
            return record
    return None


def _linked_hazard_or_raise(
    hazard_records: MutableSequence[dict[str, object]],
    job_id: str,
    hazard_id: str,
) -> dict[str, object]:
    hazard = _find_hazard(hazard_records, hazard_id)
    if hazard is None:
        raise KeyError(f"未找到隐患记录：{hazard_id}")
    if str(hazard.get("related_job_id", "")).strip() != str(job_id).strip():
        raise ValueError(f"隐患 {hazard_id} 未关联到作业 {job_id}，不能在本作业流程中操作。")
    return hazard


# --------------------------------------------------------------------------- #
# 1. Execution management
# --------------------------------------------------------------------------- #


def complete_job_execution(
    job_records: MutableSequence[dict[str, object]],
    job_id: str,
    *,
    actor: str,
    completed_at: datetime | None = None,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    """Close the execution phase: ``执行中 → 待复查``.

    Records the actual completion time and the recorder in ``execution_info``;
    the start time / executor were recorded by ``start_job_execution``.
    """
    job = _job_or_raise(job_records, job_id)
    operator = str(actor or "").strip()
    if not operator:
        raise ValueError("执行完成必须记录操作人（actor）。")

    moment = completed_at or now or datetime.now()
    transition_job(
        job_records,
        job_id,
        JOB_STATUS_AWAITING_REVIEW,
        actor=operator,
        note=str(note or "").strip() or "执行完成，进入待复查",
        now=moment,
    )
    info = job.setdefault("execution_info", {})
    if not isinstance(info, dict):
        raise ValueError("execution_info 必须是对象。")
    info["completed_at"] = _stamp(moment)
    info["completed_by"] = operator
    if str(note or "").strip():
        info["completion_note"] = str(note).strip()
    return job


# --------------------------------------------------------------------------- #
# 2. Linked hazards
# --------------------------------------------------------------------------- #


def draft_hazards_from_jsa(
    job: Mapping[str, object],
    *,
    limit: int | None = None,
) -> list[dict[str, object]]:
    """Return hazard drafts derived from the evidence-anchored JSA draft.

    Drafts are never written to the ledger and never guess: risk level, owner
    and due date are left empty for a human to fill in during rectification.
    """
    draft = dict(job.get("jsa_draft") or {})
    drafts: list[dict[str, object]] = []
    for candidate in draft.get("hazard_candidates") or ():
        if not isinstance(candidate, Mapping):
            continue
        drafts.append(
            {
                "description": str(candidate.get("text", "")),
                "hazard_type": "其他",
                "risk_level": "",
                "owner": "",
                "due_on": "",
                "corrective_action": "",
                "source_evidence": {
                    "evidence_id": str(candidate.get("evidence_id", "")),
                    "evidence_track": str(candidate.get("evidence_track", "")),
                    "track_label": str(candidate.get("track_label", "")),
                    "source_title": str(candidate.get("source_title", "")),
                    "source_url": str(candidate.get("source_url", "")),
                    "source": str(candidate.get("source", "")),
                    "page": candidate.get("page", ""),
                    "section": str(candidate.get("section", "")),
                },
                "needs_review": True,
                "status": "draft",
            }
        )
    if limit is not None:
        drafts = drafts[: int(limit)]
    return drafts


def create_job_hazard(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
    job_id: str,
    *,
    description: str,
    risk_level: str,
    actor: str,
    approved_by: str,
    hazard_type: str = "其他",
    owner: str = "待分配（执行中发现）",
    due_on: object = None,
    corrective_action: str = "",
    hazard_id: str = "",
    generated_from: Mapping[str, object] | None = None,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    """Write one hazard linked to the job, preserving the approval/guardrail gate.

    The write reuses ``workflow.guardrails.write_violations``; because creating a
    hazard is always a gated write, a non-empty ``approved_by`` is required and
    the approval (with the guardrail codes) is recorded on the job.
    """
    job = _job_or_raise(job_records, job_id)
    _require_workable_job(job)

    creator = str(actor or "").strip()
    if not creator:
        raise ValueError("隐患写入必须记录创建人（actor）。")

    description_text = str(description or "").strip()
    if not description_text:
        raise ValueError("隐患描述不能为空。")

    level = str(risk_level or "").strip()
    if level not in RISK_LEVELS:
        raise ValueError(f"风险等级必须是 {RISK_LEVELS} 之一，当前为 {risk_level!r}。")

    arguments = {
        "description": description_text,
        "hazard_type": hazard_type,
        "risk_level": level,
        "owner": owner,
        "due_on": due_on,
        "status": "待整改",
        "corrective_action": corrective_action,
    }
    violations = guardrails.write_violations(
        "create_hazard", arguments, hazard_records
    )
    approver = str(approved_by or "").strip()
    if violations and not approver:
        codes = "、".join(item.code for item in violations)
        raise ValueError(
            f"隐患写入必须经过人工审批（命中规则：{codes}）；"
            "请提供 approved_by。"
        )

    moment = now or datetime.now()
    discovered_on = moment.date()
    identifier = str(hazard_id or "").strip() or hazards.next_hazard_id(
        hazard_records
    )
    if _find_hazard(hazard_records, identifier) is not None:
        raise ValueError("隐患编号已存在。")

    record = hazards.create_hazard_record(
        hazard_id=identifier,
        description=description_text,
        hazard_type=hazard_type if hazard_type in hazards.HAZARD_TYPES else "其他",
        risk_level=level,
        owner=str(owner or "").strip() or "待分配（执行中发现）",
        found_on=discovered_on,
        due_on=due_on if due_on not in (None, "") else discovered_on,
        corrective_action=str(corrective_action or "").strip()
        or "待补充整改措施（执行中发现）。",
        status="待整改",
        existing_ids=(
            str(item.get("隐患编号", "")) for item in hazard_records
        ),
        related_job_id=job_id,
    )
    if generated_from is not None:
        record["generated_from"] = dict(generated_from)
    record["created_by"] = creator

    hazard_records.append(record)
    job["linked_hazard_ids"].append(identifier)

    if approver or violations:
        primary = hitl.primary_operation([item.operation for item in violations])
        job["approvals"].append(
            {
                "gate_id": f"hazard:{identifier}:create",
                "phase": "execution",
                "operation": primary,
                "operation_label": hitl.operation_label(primary),
                "action": hitl.ACTION_APPROVE,
                "action_label": hitl.ACTION_LABELS[hitl.ACTION_APPROVE],
                "auto": False,
                "note": str(note or "").strip()
                or "执行中发现隐患，经人工审批后写入台账。",
                "round": 1,
                "guard_codes": [item.code for item in violations],
                "actor": approver or creator,
                "decided_at": _stamp(moment),
            }
        )
    return record


# --------------------------------------------------------------------------- #
# 3. Rectification management
# --------------------------------------------------------------------------- #


def update_hazard_rectification(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
    job_id: str,
    hazard_id: str,
    *,
    actor: str,
    owner: str = "",
    due_on: object = None,
    corrective_action: str = "",
    rectification_note: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    """Assign the owner / due date and record the rectification note."""
    job = _job_or_raise(job_records, job_id)
    _require_workable_job(job)
    hazard = _linked_hazard_or_raise(hazard_records, job_id, hazard_id)
    if str(hazard.get("状态", "")) == "已关闭":
        raise ValueError(f"隐患 {hazard_id} 已关闭，不能修改整改信息。")

    operator = str(actor or "").strip()
    if not operator:
        raise ValueError("整改更新必须记录操作人（actor）。")

    updates: dict[str, object] = {}
    if str(owner or "").strip():
        updates["责任人"] = str(owner).strip()
    if due_on not in (None, ""):
        updates["整改期限"] = due_on
    if str(corrective_action or "").strip():
        updates["整改措施"] = str(corrective_action).strip()
    if str(rectification_note or "").strip():
        updates["rectification_note"] = str(rectification_note).strip()
    if not updates:
        raise ValueError("未提供任何整改信息（责任人 / 期限 / 整改措施 / 整改说明）。")

    updates["rectification_updated_by"] = operator
    updates["rectification_updated_at"] = _stamp(now)
    return hazards.update_hazard_record(hazard_records, hazard_id, updates)


# --------------------------------------------------------------------------- #
# 4. Rectification evidence
# --------------------------------------------------------------------------- #


def submit_rectification_evidence(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
    job_id: str,
    hazard_id: str,
    *,
    file_name: str,
    uploaded_by: str,
    evidence_type: str = "照片",
    note: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    """Attach one rectification evidence record (session-only metadata)."""
    job = _job_or_raise(job_records, job_id)
    _require_workable_job(job)
    hazard = _linked_hazard_or_raise(hazard_records, job_id, hazard_id)
    if str(hazard.get("状态", "")) == "已关闭":
        raise ValueError(f"隐患 {hazard_id} 已关闭，不能再上传整改证据。")

    name = str(file_name or "").strip()
    if not name:
        raise ValueError("整改证据必须包含文件名（file_name）。")
    uploader = str(uploaded_by or "").strip()
    if not uploader:
        raise ValueError("整改证据必须记录上传人（uploaded_by）。")
    kind = str(evidence_type or "").strip() or "其他"
    if kind not in RECTIFICATION_EVIDENCE_TYPES:
        raise ValueError(
            f"证据类型必须是 {RECTIFICATION_EVIDENCE_TYPES} 之一，当前为 {evidence_type!r}。"
        )

    entry = {
        "file_name": name,
        "evidence_type": kind,
        "note": str(note or "").strip(),
        "uploaded_by": uploader,
        "uploaded_at": _stamp(now),
    }
    existing = list(hazard.get("rectification_evidence") or ())
    existing.append(entry)
    updates: dict[str, object] = {"rectification_evidence": existing}
    if str(hazard.get("状态", "")) == "待整改":
        updates["状态"] = "整改中"
    return hazards.update_hazard_record(hazard_records, hazard_id, updates)


# --------------------------------------------------------------------------- #
# 5. EHS review and hazard closure
# --------------------------------------------------------------------------- #


def review_hazard(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
    job_id: str,
    hazard_id: str,
    *,
    reviewer: str,
    review_note: str,
    review_date: object = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Record the EHS review opinion, reviewer and review time."""
    job = _job_or_raise(job_records, job_id)
    _require_workable_job(job)
    hazard = _linked_hazard_or_raise(hazard_records, job_id, hazard_id)
    if str(hazard.get("状态", "")) == "已关闭":
        raise ValueError(f"隐患 {hazard_id} 已关闭，不能重复复查。")

    reviewer_name = str(reviewer or "").strip()
    if not reviewer_name:
        raise ValueError("复查必须记录复查人（reviewer）。")
    note = str(review_note or "").strip()
    if not note:
        raise ValueError("复查必须记录复查意见（review_note）。")

    resolved_date = (
        review_date
        if review_date not in (None, "")
        else (now or datetime.now()).date()
    )
    return hazards.update_hazard_record(
        hazard_records,
        hazard_id,
        {
            "reviewer": reviewer_name,
            "review_date": resolved_date,
            "review_note": note,
            "reviewed_at": _stamp(now),
        },
    )


def close_hazard(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
    job_id: str,
    hazard_id: str,
    *,
    closed_by: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    """Close one hazard: requires evidence, a reviewer and a review note."""
    job = _job_or_raise(job_records, job_id)
    _require_workable_job(job)
    hazard = _linked_hazard_or_raise(hazard_records, job_id, hazard_id)
    if str(hazard.get("状态", "")) == "已关闭":
        raise ValueError(f"隐患 {hazard_id} 已关闭，不能重复关闭。")

    evidence = list(hazard.get("rectification_evidence") or ())
    if not evidence:
        raise ValueError(f"隐患 {hazard_id} 没有整改证据，不允许关闭。")
    reviewer = str(hazard.get("reviewer", "")).strip()
    if not reviewer:
        raise ValueError(f"隐患 {hazard_id} 没有复查人，不允许关闭。")
    if not str(hazard.get("review_note", "")).strip():
        raise ValueError(f"隐患 {hazard_id} 没有复查意见，不允许关闭。")

    closer = str(closed_by or "").strip() or reviewer
    moment = now or datetime.now()
    return hazards.update_hazard_record(
        hazard_records,
        hazard_id,
        {
            "状态": "已关闭",
            "closed_by": closer,
            "closed_at": moment.date().isoformat(),
            "closed_at_timestamp": _stamp(moment),
        },
    )


# --------------------------------------------------------------------------- #
# 6. Job closure
# --------------------------------------------------------------------------- #


def close_job(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
    job_id: str,
    *,
    actor: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    """Close the job once every linked hazard is closed (``待复查 → 已关闭``)."""
    job = _job_or_raise(job_records, job_id)
    status = str(job.get("status", ""))
    if status != JOB_STATUS_AWAITING_REVIEW:
        raise ValueError(
            f"只有待复查状态的作业才能关闭，当前状态：{status!r}。"
        )

    linked_ids = [str(item) for item in (job.get("linked_hazard_ids") or ())]
    open_ids: list[str] = []
    for hazard_id in linked_ids:
        hazard = _find_hazard(hazard_records, hazard_id)
        if hazard is None:
            raise ValueError(f"关联隐患 {hazard_id} 不存在，无法关闭作业。")
        if str(hazard.get("状态", "")) != "已关闭":
            open_ids.append(hazard_id)
    if open_ids:
        raise ValueError(
            "存在未关闭的关联隐患，不允许关闭作业：" + "、".join(open_ids)
        )

    transition_job(
        job_records,
        job_id,
        JOB_STATUS_CLOSED,
        actor=actor,
        note=str(note or "").strip() or "全部关联隐患已关闭，作业闭环",
        now=now,
    )
    job["closure_info"] = {
        "closed_by": str(actor or "").strip(),
        "closed_at": _stamp(now),
        "closure_note": str(note or "").strip(),
        "linked_hazard_count": len(linked_ids),
        "all_hazards_closed": True,
    }
    return job


__all__ = [
    "RECTIFICATION_EVIDENCE_TYPES",
    "close_hazard",
    "close_job",
    "complete_job_execution",
    "create_job_hazard",
    "draft_hazards_from_jsa",
    "review_hazard",
    "submit_rectification_evidence",
    "update_hazard_rectification",
]

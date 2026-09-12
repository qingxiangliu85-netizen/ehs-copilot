"""Job review flow: evidence attachment, JSA drafting and human confirmation.

This module connects the V4 job record (``jobs.py``), the two evidence tracks
and the V3 human-in-the-loop primitives (``workflow.hitl``) into the first half
of the job lifecycle::

    新建作业（草稿）
      → 挂接安全证据（公开来源 / SDS 两条独立轨道）
      → 生成 JSA 草稿（证据候选 + 待EHS确认项，不做任何猜测）
      → EHS 人工确认（修改步骤 / 危害 / 措施 / L-S，保留 AI 原稿与人工最终版）
      → 待审批
      → 批准 / 修改后批准 / 驳回（复用 V3 ApprovalDecision）
      → 批准后允许进入执行

Design rules:

* **Two evidence tracks stay separate.**  Public-source evidence (NIOSH / OSHA
  verbatim quotes) is never called SDS and can never be attached to
  ``job["sds_evidence"]``.  SDS evidence requires a real uploaded-PDF snapshot
  (file name + page + snippet).
* **No guessed risk values.**  L/S are chosen by a human during confirmation
  and R is computed by ``jsa.calculate_risk``; this module never invents
  hazards, consequences or scores.
* **Every status change goes through** ``jobs.transition_job``.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Mapping, MutableSequence

import jsa
from jobs import (
    JOB_STATUS_APPROVED,
    JOB_STATUS_AWAITING_APPROVAL,
    JOB_STATUS_AWAITING_EHS,
    JOB_STATUS_DRAFT,
    JOB_STATUS_EXECUTING,
    JOB_STATUS_REJECTED,
    get_job,
    transition_job,
)

from workflow import hitl


EVIDENCE_TRACK_PUBLIC = "public_sources"
EVIDENCE_TRACK_SDS = "sds"

EVIDENCE_TRACK_LABELS: dict[str, str] = {
    EVIDENCE_TRACK_PUBLIC: "公开来源证据（非 SDS）",
    EVIDENCE_TRACK_SDS: "SDS 证据（需合法 SDS 原文）",
}

JOB_APPROVAL_GUARD = "job_approval_required"

OPEN_ITEM_PREFIX = "待EHS确认："

_PUBLIC_HAZARD_TOPICS = frozenset(
    {
        "hazard_identification",
        "exposure_routes",
        "physical_properties",
        "storage_incompatibility",
    }
)
_PUBLIC_CONSEQUENCE_TOPICS = frozenset({"hazard_identification"})
_PUBLIC_CONTROL_TOPICS = frozenset(
    {
        "ppe",
        "respiratory_protection",
        "spill_response",
        "firefighting",
        "storage_incompatibility",
    }
)

_SDS_HAZARD_SECTIONS = frozenset({"2", "3", "11"})
_SDS_CONTROL_SECTIONS = frozenset({"5", "6", "7", "8"})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _job_or_raise(
    records: MutableSequence[dict[str, object]], job_id: str
) -> dict[str, object]:
    job = get_job(records, job_id)
    if job is None:
        raise KeyError(f"未找到作业单：{job_id}")
    return job


def _normalise_public_item(item: Mapping[str, object], index: int) -> dict[str, Any]:
    if "source" in item and "page" in item and not item.get("source_url"):
        raise ValueError(
            f"public_evidence 第 {index} 条是 SDS 证据（含 source/page），"
            "不得挂到公开来源轨道，两条证据轨道必须分开。"
        )
    required = (
        "evidence_id",
        "source_title",
        "source_url",
        "organization",
        "passage",
        "retrieved_at",
    )
    missing = [field for field in required if not str(item.get(field, "")).strip()]
    if missing:
        raise ValueError(
            f"public_evidence 第 {index} 条缺少字段：{'、'.join(missing)}。"
        )
    url = str(item["source_url"]).strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"public_evidence 第 {index} 条的来源链接无效：{url!r}。")
    return dict(item)


def _normalise_sds_item(item: Mapping[str, object], index: int) -> dict[str, Any]:
    if str(item.get("source_url", "")).strip():
        raise ValueError(
            f"sds_evidence 第 {index} 条带有 source_url，属于公开来源证据，"
            "不得挂到 SDS 证据轨道。"
        )
    source = str(item.get("source", "")).strip()
    if not source:
        raise ValueError(f"sds_evidence 第 {index} 条缺少来源文件（source）。")
    if not source.lower().endswith(".pdf"):
        raise ValueError(
            f"sds_evidence 第 {index} 条来源 {source!r} 不是 PDF；"
            "SDS 证据只能来自用户上传的合法 SDS PDF。"
        )
    try:
        page = int(item.get("page", 0) or 0)
    except (TypeError, ValueError):
        page = 0
    if page <= 0:
        raise ValueError(f"sds_evidence 第 {index} 条缺少有效页码（page）。")
    snippet = str(item.get("snippet", "") or item.get("passage", "")).strip()
    if not snippet:
        raise ValueError(f"sds_evidence 第 {index} 条没有原文片段。")
    return dict(item)


# --------------------------------------------------------------------------- #
# Evidence attachment
# --------------------------------------------------------------------------- #


def attach_public_evidence(
    job: dict[str, object],
    items: Any,
) -> dict[str, object]:
    """Replace the job's public-source evidence track.

    Public evidence is *not* SDS: items must carry a source URL and are labelled
    as ``公开来源证据（非 SDS）``.  SDS-shaped items are rejected.
    """
    if items is not None and not isinstance(items, (list, tuple)):
        raise ValueError("public_evidence 必须是证据条目列表。")
    normalised = [
        _normalise_public_item(item, index)
        for index, item in enumerate(list(items or ()), start=1)
    ]
    job["public_evidence"] = normalised
    return job


def attach_sds_evidence(job: dict[str, object], items: Any) -> dict[str, object]:
    """Replace the job's SDS evidence track with uploaded-PDF snapshots.

    Every item must reference a real ``*.pdf`` source with a page and snippet;
    public-source items (with ``source_url``) are rejected.
    """
    if items is not None and not isinstance(items, (list, tuple)):
        raise ValueError("sds_evidence 必须是证据条目列表。")
    normalised = [
        _normalise_sds_item(item, index)
        for index, item in enumerate(list(items or ()), start=1)
    ]
    job["sds_evidence"] = normalised
    return job


# --------------------------------------------------------------------------- #
# JSA draft
# --------------------------------------------------------------------------- #


def _public_candidate(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "text": str(item.get("passage", "")).strip(),
        "evidence_id": str(item.get("evidence_id", "")),
        "source_title": str(item.get("source_title", "")),
        "source_url": str(item.get("source_url", "")),
        "organization": str(item.get("organization", "")),
        "section": str(item.get("section", "")),
        "evidence_track": EVIDENCE_TRACK_PUBLIC,
        "track_label": EVIDENCE_TRACK_LABELS[EVIDENCE_TRACK_PUBLIC],
        "status": "draft_from_evidence",
        "awaiting_confirmation": True,
    }


def _sds_candidate(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "text": str(item.get("snippet", "") or item.get("passage", "")).strip(),
        "source": str(item.get("source", "")),
        "page": int(item.get("page", 0) or 0),
        "sections": str(item.get("sections", "")),
        "evidence_id": str(
            item.get("evidence_id", "")
            or f"sds:{item.get('source', '')}:{item.get('page', '')}"
        ),
        "evidence_track": EVIDENCE_TRACK_SDS,
        "track_label": EVIDENCE_TRACK_LABELS[EVIDENCE_TRACK_SDS],
        "status": "draft_from_evidence",
        "awaiting_confirmation": True,
    }


def _sds_candidate_category(item: Mapping[str, Any]) -> str:
    sections = {
        token.strip()
        for token in str(item.get("sections", "")).split(",")
        if token.strip()
    }
    if sections & _SDS_HAZARD_SECTIONS:
        return "hazard"
    if sections & _SDS_CONTROL_SECTIONS:
        return "control"
    return "reference"


def _open_items(
    steps: list[dict[str, object]],
    public_count: int,
    sds_count: int,
) -> list[str]:
    items = [
        OPEN_ITEM_PREFIX
        + "L/S 与残余 L/S 未确定，须由 EHS 按现场条件填写；"
        "风险值与等级由 jsa.calculate_risk 按 R=L×S 计算。",
        OPEN_ITEM_PREFIX
        + "下方危害 / 后果 / 控制措施均为证据候选草稿，须由 EHS 逐条确认、修改或删除。",
    ]
    if not steps:
        items.append(OPEN_ITEM_PREFIX + "作业步骤缺失，须由 EHS 补充。")
    if public_count == 0 and sds_count == 0:
        items.append(
            OPEN_ITEM_PREFIX + "尚无任何安全证据，不得生成安全结论。"
        )
    if sds_count == 0:
        items.append(
            OPEN_ITEM_PREFIX
            + "SDS 证据轨道为空：当前结论只能引用公开来源证据（非 SDS），"
            "不得称为 SDS 结论；需上传合法 SDS 后才能生成 SDS 证据。"
        )
    else:
        items.append(
            OPEN_ITEM_PREFIX + "SDS 证据已挂接，须核对来源文件与页码后再使用。"
        )
    return items


def draft_jsa(
    records: MutableSequence[dict[str, object]],
    job_id: str,
    *,
    actor: str = "AI工作流",
    now: datetime | None = None,
) -> dict[str, object]:
    """Build an evidence-anchored JSA draft and submit it for EHS confirmation.

    The draft never guesses: hazards / consequences / controls are candidate
    quotes taken from the attached evidence, everything unresolved is listed in
    ``open_items`` with a ``待EHS确认：`` prefix, and no L/S or risk value is
    produced.  A successful draft moves the job ``草稿 → 待EHS确认``.
    """
    job = _job_or_raise(records, job_id)
    status = str(job.get("status", ""))
    if status != JOB_STATUS_DRAFT:
        raise ValueError(
            f"只有草稿状态的作业才能生成 JSA 草稿，当前状态：{status!r}。"
        )

    steps = [
        {
            "order": int(step.get("order", index) or index),
            "name": str(step.get("name", "")),
            "note": str(step.get("note", "")),
        }
        for index, step in enumerate(job.get("steps") or (), start=1)
    ]

    hazard_candidates: list[dict[str, Any]] = []
    consequence_candidates: list[dict[str, Any]] = []
    control_candidates: list[dict[str, Any]] = []
    reference_candidates: list[dict[str, Any]] = []

    for item in job.get("public_evidence") or ():
        candidate = _public_candidate(item)
        topic = str(item.get("topic", ""))
        if topic in _PUBLIC_HAZARD_TOPICS:
            hazard_candidates.append(candidate)
        if topic in _PUBLIC_CONSEQUENCE_TOPICS:
            consequence_candidates.append(dict(candidate))
        if topic in _PUBLIC_CONTROL_TOPICS:
            control_candidates.append(dict(candidate))
        if (
            topic not in _PUBLIC_HAZARD_TOPICS
            and topic not in _PUBLIC_CONSEQUENCE_TOPICS
            and topic not in _PUBLIC_CONTROL_TOPICS
        ):
            reference_candidates.append(dict(candidate))

    for item in job.get("sds_evidence") or ():
        candidate = _sds_candidate(item)
        category = _sds_candidate_category(item)
        if category == "hazard":
            hazard_candidates.append(candidate)
            consequence_candidates.append(dict(candidate))
        elif category == "control":
            control_candidates.append(candidate)
        else:
            reference_candidates.append(candidate)

    suggested: dict[str, Any] = {
        "job_step": "；".join(step["name"] for step in steps),
        "hazard": hazard_candidates[0]["text"] if hazard_candidates else "",
        "consequence": (
            consequence_candidates[0]["text"] if consequence_candidates else ""
        ),
        "existing_controls": "",
        "suggested_controls": (
            control_candidates[0]["text"] if control_candidates else ""
        ),
        "likelihood": None,
        "severity": None,
        "residual_likelihood": None,
        "residual_severity": None,
    }

    draft: dict[str, Any] = {
        "draft_version": 1,
        "generated_at": _stamp(now),
        "generated_by": "job_review.draft_jsa",
        "job_id": job.get("job_id", ""),
        "job_name": job.get("job_name", ""),
        "steps": steps,
        "hazard_candidates": hazard_candidates,
        "consequence_candidates": consequence_candidates,
        "control_candidates": control_candidates,
        "reference_candidates": reference_candidates,
        "suggested": suggested,
        "risk_inputs": None,
        "evidence_summary": {
            "public_sources": len(job.get("public_evidence") or ()),
            "sds": len(job.get("sds_evidence") or ()),
            "labels": dict(EVIDENCE_TRACK_LABELS),
        },
        "open_items": _open_items(
            steps,
            len(job.get("public_evidence") or ()),
            len(job.get("sds_evidence") or ()),
        ),
    }

    job["jsa_draft"] = draft
    transition_job(
        records,
        job_id,
        JOB_STATUS_AWAITING_EHS,
        actor=str(actor).strip() or "AI工作流",
        note="AI 生成 JSA 草稿并提交 EHS 确认",
        now=now,
    )
    return job


# --------------------------------------------------------------------------- #
# EHS confirmation
# --------------------------------------------------------------------------- #


def _changed_fields(
    suggested: Mapping[str, Any], final_inputs: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    changed: dict[str, dict[str, Any]] = {}
    for key, value in final_inputs.items():
        original = suggested.get(key)
        if key in {"job_step", "hazard", "consequence", "existing_controls",
                   "suggested_controls"}:
            original = str(original or "").strip()
            value = str(value or "").strip()
        if original != value:
            changed[key] = {"ai_draft": original, "final": value}
    return changed


def confirm_jsa(
    records: MutableSequence[dict[str, object]],
    job_id: str,
    *,
    confirmed_by: str,
    job_step: str,
    hazard: str,
    consequence: str,
    suggested_controls: str,
    existing_controls: str = "",
    likelihood: int,
    severity: int,
    residual_likelihood: int,
    residual_severity: int,
    change_note: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    """Apply the EHS human confirmation and move the job to ``待审批``.

    The AI draft is kept untouched on the job; the human final version, the
    confirmer, the timestamp and the change list are stored on
    ``job["jsa_confirmation"]``.  Risk values are computed by
    :func:`jsa.create_jsa_record` (``R = L × S``).
    """
    job = _job_or_raise(records, job_id)
    status = str(job.get("status", ""))
    if status != JOB_STATUS_AWAITING_EHS:
        raise ValueError(
            "作业单当前状态不允许 EHS 确认："
            f"{status!r}；必须先由 AI 生成 JSA 草稿并进入待EHS确认。"
        )
    if job.get("jsa_draft") is None:
        raise ValueError("作业单缺少 JSA 草稿，不能进行 EHS 确认。")

    confirmer = str(confirmed_by or "").strip()
    if not confirmer:
        raise ValueError("EHS 确认必须记录确认人（confirmed_by）。")

    required_texts = {
        "job_step（作业步骤）": job_step,
        "hazard（危害因素）": hazard,
        "consequence（可能后果）": consequence,
    }
    missing = [label for label, value in required_texts.items() if not str(value).strip()]
    if missing:
        raise ValueError("EHS 确认缺少必填内容：" + "、".join(missing) + "。")

    try:
        final_record = jsa.create_jsa_record(
            job_name=str(job.get("job_name", "")),
            job_step=str(job_step),
            hazard=str(hazard),
            consequence=str(consequence),
            likelihood=int(likelihood),
            severity=int(severity),
            existing_controls=str(existing_controls or ""),
            suggested_controls=str(suggested_controls or ""),
            residual_likelihood=int(residual_likelihood),
            residual_severity=int(residual_severity),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"EHS 确认的风险输入无效：{exc}") from exc

    suggested = dict((job.get("jsa_draft") or {}).get("suggested") or {})
    final_inputs = {
        "job_step": str(job_step),
        "hazard": str(hazard),
        "consequence": str(consequence),
        "existing_controls": str(existing_controls or ""),
        "suggested_controls": str(suggested_controls or ""),
        "likelihood": int(likelihood),
        "severity": int(severity),
        "residual_likelihood": int(residual_likelihood),
        "residual_severity": int(residual_severity),
    }

    stamp = _stamp(now)
    confirmation: dict[str, Any] = {
        "confirmed_by": confirmer,
        "confirmed_at": stamp,
        "change_note": str(change_note or "").strip(),
        "ai_draft": deepcopy(job.get("jsa_draft")),
        "final": final_record,
        "changed_fields": _changed_fields(suggested, final_inputs),
        "risk_source": "jsa.calculate_risk",
    }

    transition_job(
        records,
        job_id,
        JOB_STATUS_AWAITING_APPROVAL,
        actor=confirmer,
        note=str(change_note or "").strip() or "EHS 确认 JSA 与风险输入",
        now=now,
    )
    job["jsa_confirmation"] = confirmation
    return job


# --------------------------------------------------------------------------- #
# Job approval (reuses the V3 HITL primitives)
# --------------------------------------------------------------------------- #


def build_job_approval_request(job: Mapping[str, object]) -> hitl.ApprovalRequest:
    """Build the approval request for a job that is waiting for approval."""
    status = str(job.get("status", ""))
    if status != JOB_STATUS_AWAITING_APPROVAL:
        raise ValueError(
            "作业单当前状态不允许审批："
            f"{status!r}；必须先完成 EHS 确认并进入待审批。"
        )
    confirmation = dict(job.get("jsa_confirmation") or {})
    final = dict(confirmation.get("final") or {})
    if not final:
        raise ValueError("作业单缺少 EHS 确认后的 JSA 正式版本，不能进入审批。")

    identifier = str(job.get("job_id", ""))
    gate_id = f"job:{identifier}:approval"
    calls = (
        {
            "call_id": gate_id,
            "tool": "approve_job",
            "arguments": {"job_id": identifier, "note": ""},
            "operations": [hitl.OP_APPROVE_JOB],
            "reasons": ["作业单批准属于写操作，必须由人工确认后才能进入执行。"],
        },
    )
    current = {
        "job_id": identifier,
        "job_name": str(job.get("job_name", "")),
        "确认人": str(confirmation.get("confirmed_by", "")),
        "风险值R": final.get("风险值R"),
        "风险等级": final.get("风险等级"),
        "残余风险R": final.get("残余风险R"),
        "残余风险等级": final.get("残余风险等级"),
    }
    return hitl.ApprovalRequest(
        gate_id=gate_id,
        operation=hitl.OP_APPROVE_JOB,
        reason=(
            "作业单已完成 EHS 确认；批准后方可进入执行，"
            "未批准或被驳回的作业单不能执行。"
        ),
        summary=f"{job.get('job_name', '')}｜{identifier}",
        calls=calls,
        current=current,
        proposed={"status": JOB_STATUS_APPROVED},
        guard_codes=(JOB_APPROVAL_GUARD,),
        round=1,
        kind="plan",
    )


def _as_decision(decision: Any) -> hitl.ApprovalDecision | None:
    if isinstance(decision, hitl.ApprovalDecision):
        return decision
    if isinstance(decision, Mapping):
        return hitl.ApprovalDecision.from_payload(decision)
    return None


def _validate_modification(
    request: hitl.ApprovalRequest, decision: hitl.ApprovalDecision
) -> None:
    if not decision.calls:
        raise ValueError("「修改后批准」必须提供修改内容。")
    originals = {
        str(item.get("call_id", "")): dict(item) for item in request.calls
    }
    for entry in decision.calls:
        call_id = str(entry.get("call_id", ""))
        if call_id not in originals:
            raise ValueError(f"修改引用了不存在的审批项：{call_id!r}。")
        tool = str(entry.get("tool") or originals[call_id].get("tool", ""))
        if tool != str(originals[call_id].get("tool", "")):
            raise ValueError("作业审批的「修改后批准」不允许更换操作类型。")
        arguments = dict(entry.get("arguments") or {})
        extra = sorted(set(arguments) - {"note"})
        if extra:
            raise ValueError(
                "作业审批的「修改后批准」只允许调整审批备注，"
                f"不允许修改 {extra}（JSA、风险值与作业状态不可在此变更）。"
            )


def decide_job_approval(
    records: MutableSequence[dict[str, object]],
    job_id: str,
    decision: hitl.ApprovalDecision | Mapping[str, Any],
    *,
    actor: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Apply a V3-style human decision (approve / modify / reject) to a job.

    * approve / modify → 待审批 → 已批准（modify 只能调整审批备注）
    * reject           → 待审批 → 已驳回（终态，不能执行）
    """
    job = _job_or_raise(records, job_id)
    status = str(job.get("status", ""))
    if status != JOB_STATUS_AWAITING_APPROVAL:
        raise ValueError(
            "作业单当前状态不允许审批："
            f"{status!r}；必须先完成 EHS 确认并进入待审批。"
        )

    parsed = _as_decision(decision)
    if parsed is None:
        raise ValueError("审批内容无法识别，已拒绝处理。")
    if parsed.round > hitl.MAX_APPROVAL_ROUNDS:
        raise ValueError("审批轮次超出上限，已拒绝处理。")

    request = build_job_approval_request(job)
    if parsed.gate_id and parsed.gate_id != request.gate_id:
        raise ValueError(
            f"审批编号不匹配（收到 {parsed.gate_id!r}，"
            f"当前需要 {request.gate_id!r}），已拒绝处理。"
        )

    operator = str(actor or "").strip()
    if not operator:
        raise ValueError("审批必须记录操作人（actor）。")

    stamp = _stamp(now)
    effective_note = str(parsed.note or "").strip()

    if parsed.is_reject:
        entry = hitl.approval_record(request, parsed, phase="plan")
        entry["actor"] = operator
        entry["decided_at"] = stamp
        transition_job(
            records,
            job_id,
            JOB_STATUS_REJECTED,
            actor=operator,
            note=effective_note or "审批驳回",
            now=now,
        )
        job["approvals"].append(entry)
        return job

    if parsed.is_modify:
        _validate_modification(request, parsed)
        for call in parsed.calls:
            modified_note = str((call.get("arguments") or {}).get("note", "")).strip()
            if modified_note:
                effective_note = modified_note
                break

    entry = hitl.approval_record(request, parsed, phase="plan")
    entry["actor"] = operator
    entry["decided_at"] = stamp
    if effective_note:
        entry["note"] = effective_note
    transition_job(
        records,
        job_id,
        JOB_STATUS_APPROVED,
        actor=operator,
        note=effective_note or parsed.action_label,
        now=now,
    )
    job["approvals"].append(entry)
    return job


def start_job_execution(
    records: MutableSequence[dict[str, object]],
    job_id: str,
    *,
    actor: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    """Move an approved job to ``执行中``; every other status is refused."""
    job = _job_or_raise(records, job_id)
    if job.get("jsa_confirmation") is None:
        raise ValueError("作业单尚未完成 EHS 确认，不能进入执行。")
    approved = any(
        str(item.get("action")) in {hitl.ACTION_APPROVE, hitl.ACTION_MODIFY}
        for item in job.get("approvals") or ()
    )
    if not approved:
        raise ValueError("作业单尚未获得人工批准（或已被驳回），不能进入执行。")
    moment = now or datetime.now()
    transition_job(
        records,
        job_id,
        JOB_STATUS_EXECUTING,
        actor=actor,
        note=str(note or "").strip() or "批准后开始执行",
        now=moment,
    )
    info = job.setdefault("execution_info", {})
    if not isinstance(info, dict):
        raise ValueError("execution_info 必须是对象。")
    info["executor"] = str(actor).strip()
    info["started_at"] = _stamp(moment)
    return job


__all__ = [
    "EVIDENCE_TRACK_LABELS",
    "EVIDENCE_TRACK_PUBLIC",
    "EVIDENCE_TRACK_SDS",
    "JOB_APPROVAL_GUARD",
    "OPEN_ITEM_PREFIX",
    "attach_public_evidence",
    "attach_sds_evidence",
    "build_job_approval_request",
    "confirm_jsa",
    "decide_job_approval",
    "draft_jsa",
    "start_job_execution",
]

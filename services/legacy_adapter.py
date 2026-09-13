"""Adapters between the frozen V4 domain dicts and the V5 model.

Nothing in this module touches the database or Streamlit: it only converts
``jobs.py`` JobRecords and ``hazards.py`` hazard dicts into the V5 permit /
hazard shapes and back.  The V4 modules therefore stay untouched while the V5
layer can import existing demo data and legacy records.

The V5-only statuses have no exact V4 counterpart; those mappings are
intentionally lossy and documented in :mod:`workflow.permit_state` and
:mod:`workflow.hazard_state`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from hazards import USER_DATA_LABEL
from jobs import JOB_FIELDS
from workflow import hazard_state, permit_state


EVIDENCE_TRACK_PUBLIC = "public_sources"
EVIDENCE_TRACK_SDS = "sds"


def is_demo_label(value: Any) -> bool:
    """Return whether a data label marks simulated/demo content."""
    text = str(value or "").strip()
    return bool(text) and text != USER_DATA_LABEL


def job_status_to_v5(status: Any) -> str:
    """Map a V4 job status onto the canonical V5 permit status."""
    return permit_state.V4_JOB_STATUS_TO_V5.get(str(status).strip(), "")


def v5_status_to_job(status: Any) -> str:
    """Map a V5 permit status back onto a V4-compatible status label."""
    return permit_state.V5_TO_V4_JOB_STATUS.get(str(status).strip(), "")


def hazard_status_to_v5(status: Any) -> str:
    """Map a V4 hazard status onto the canonical V5 hazard status."""
    return hazard_state.V4_HAZARD_STATUS_TO_V5.get(str(status).strip(), "")


def v5_status_to_hazard(status: Any) -> str:
    """Map a V5 hazard status back onto the V4-compatible label."""
    return hazard_state.V5_TO_V4_HAZARD_STATUS.get(str(status).strip(), "")


def _normalise_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "").strip()


def _normalise_risk_label(value: Any) -> str:
    """Map V4 JSA labels (``低风险``) onto the canonical levels (``低``)."""
    text = str(value or "").strip()
    if text.endswith("风险"):
        text = text[: -len("风险")].strip()
    return text


def _public_evidence_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": str(item.get("evidence_id", "")),
        "source_title": str(item.get("source_title", "")),
        "source_url": str(item.get("source_url", "")),
        "organization": str(item.get("organization", "")),
        "snippet": str(item.get("passage", "") or item.get("snippet", "")),
        "topic": str(item.get("topic", "")),
        "section": str(item.get("section", "")),
        "captured_at": str(item.get("retrieved_at", "") or item.get("captured_at", "")),
        "data_label": str(item.get("data_label", "")),
    }


def _sds_evidence_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": str(item.get("evidence_id", "")),
        "source": str(item.get("source", "")),
        "page": item.get("page", 0),
        "sections": str(item.get("sections", "")),
        "snippet": str(item.get("snippet", "") or item.get("passage", "")),
        "topic": str(item.get("topic", "")),
        "captured_at": str(item.get("captured_at", "")),
        "data_label": str(item.get("data_label", "")),
    }


def _jsa_items_from_job(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    confirmation = dict(job.get("jsa_confirmation") or {})
    final = dict(confirmation.get("final") or {})
    if not final:
        return []
    return [
        {
            "step_no": 1,
            "work_step": str(final.get("作业步骤", "")),
            "hazard": str(final.get("危害因素", "")),
            "consequence": str(final.get("可能后果", "")),
            "likelihood": final.get("可能性L"),
            "severity": final.get("严重度S"),
            "existing_controls": str(final.get("现有控制措施", "")),
            "proposed_controls": str(final.get("建议控制措施", "")),
            "residual_likelihood": final.get("控制后可能性L"),
            "residual_severity": final.get("控制后严重度S"),
            "confirmed_by": str(confirmation.get("confirmed_by", "")),
            "confirmed_at": str(confirmation.get("confirmed_at", "")),
        }
    ]


def _first_actor(job: Mapping[str, Any]) -> str:
    history = list(job.get("status_history") or ())
    for entry in history:
        actor = str(entry.get("actor", "")).strip()
        if actor:
            return actor
    info = dict(job.get("execution_info") or {})
    return str(info.get("executor", "") or "")


def job_to_permit(job: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one V4 JobRecord into V5 permit fields plus child rows."""
    confirmation = dict(job.get("jsa_confirmation") or {})
    final = dict(confirmation.get("final") or {})
    execution = dict(job.get("execution_info") or {})
    closure = dict(job.get("closure_info") or {})
    data_label = str(job.get("data_label", ""))

    evidence: list[dict[str, Any]] = []
    for item in job.get("public_evidence") or ():
        if isinstance(item, Mapping):
            evidence.append(
                {"track": EVIDENCE_TRACK_PUBLIC, **_public_evidence_item(item)}
            )
    for item in job.get("sds_evidence") or ():
        if isinstance(item, Mapping):
            evidence.append({"track": EVIDENCE_TRACK_SDS, **_sds_evidence_item(item)})

    chemicals = [
        {
            "chemical_name": str(item.get("name", "")),
            "aliases": list(item.get("aliases") or ()),
            "sds_file": str(item.get("sds_file", "")),
            "sds_status": str(item.get("sds_status", "")),
        }
        for item in job.get("chemicals") or ()
        if isinstance(item, Mapping)
    ]

    steps = [
        {
            "step_no": int(step.get("order", index) or index),
            "name": str(step.get("name", "")),
            "note": str(step.get("note", "")),
        }
        for index, step in enumerate(job.get("steps") or (), start=1)
        if isinstance(step, Mapping)
    ]

    return {
        "permit_id": str(job.get("job_id", "")),
        "title": str(job.get("job_name", "")),
        "permit_type": str(job.get("job_type", "")),
        "status": job_status_to_v5(job.get("status", "")),
        "applicant_id": _first_actor(job),
        "owner_id": str(execution.get("executor", "") or ""),
        "area": str(job.get("area", "")),
        "risk_level": _normalise_risk_label(final.get("风险等级", "")),
        "residual_risk_level": _normalise_risk_label(final.get("残余风险等级", "")),
        "control_measures": str(
            final.get("建议控制措施", "") or final.get("现有控制措施", "")
        ),
        "valid_from": _normalise_date(job.get("valid_from", "")),
        "valid_to": _normalise_date(job.get("valid_to", "")),
        "handback_note": str(execution.get("completion_note", "")),
        "closed_by": str(closure.get("closed_by", "")),
        "closed_at": str(closure.get("closed_at", "")),
        "closure_note": str(closure.get("closure_note", "")),
        "data_label": data_label,
        "is_demo": is_demo_label(data_label),
        "created_at": str(job.get("created_at", "")),
        "updated_at": str(job.get("updated_at", "")),
        "chemicals": chemicals,
        "steps": steps,
        "evidence": evidence,
        "jsa_items": _jsa_items_from_job(job),
    }


def permit_to_job(permit: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a V5 permit aggregate back into a V4-compatible job dict."""
    job: dict[str, Any] = {field: "" for field in JOB_FIELDS}
    job["job_id"] = str(permit.get("id", ""))
    job["job_name"] = str(permit.get("title", ""))
    job["job_type"] = str(permit.get("permit_type", ""))
    job["status"] = v5_status_to_job(permit.get("status", ""))
    job["chemicals"] = [
        {
            "name": str(item.get("chemical_name", "")),
            "aliases": list(item.get("aliases") or ()),
            "sds_file": str(item.get("sds_file", "")),
            "sds_status": str(item.get("sds_status", "")),
        }
        for item in permit.get("chemicals") or ()
    ]
    job["steps"] = [
        {
            "order": int(step.get("step_no", index) or index),
            "name": str(step.get("name", "")),
            "note": str(step.get("note", "")),
        }
        for index, step in enumerate(permit.get("steps") or (), start=1)
    ]
    evidence = permit.get("evidence") or {}
    job["public_evidence"] = [
        {
            "evidence_id": str(item.get("evidence_id", "")),
            "source_title": str(item.get("source_title", "")),
            "source_url": str(item.get("source_url", "")),
            "organization": str(item.get("organization", "")),
            "passage": str(item.get("snippet", "")),
            "topic": str(item.get("topic", "")),
            "section": str(item.get("section", "")),
            "retrieved_at": str(item.get("captured_at", "")),
        }
        for item in evidence.get(EVIDENCE_TRACK_PUBLIC, [])
    ]
    job["sds_evidence"] = [
        {
            "evidence_id": str(item.get("evidence_id", "")),
            "source": str(item.get("source", "")),
            "page": item.get("page", 0),
            "sections": str(item.get("sections", "")),
            "snippet": str(item.get("snippet", "")),
            "topic": str(item.get("topic", "")),
        }
        for item in evidence.get(EVIDENCE_TRACK_SDS, [])
    ]
    items = list(permit.get("jsa_items") or ())
    if items:
        final = items[0]
        job["jsa_confirmation"] = {
            "confirmed_by": str(final.get("confirmed_by", "")),
            "confirmed_at": str(final.get("confirmed_at", "")),
            "final": {
                "作业步骤": str(final.get("work_step", "")),
                "危害因素": str(final.get("hazard", "")),
                "可能后果": str(final.get("consequence", "")),
                "可能性L": final.get("likelihood"),
                "严重度S": final.get("severity"),
                "风险值R": final.get("risk_score"),
                "风险等级": str(final.get("risk_level", "")),
                "现有控制措施": str(final.get("existing_controls", "")),
                "建议控制措施": str(final.get("proposed_controls", "")),
                "控制后可能性L": final.get("residual_likelihood"),
                "控制后严重度S": final.get("residual_severity"),
                "残余风险R": final.get("residual_risk_score"),
                "残余风险等级": str(final.get("residual_risk_level", "")),
            },
        }
    execution: dict[str, Any] = {}
    if str(permit.get("handback_note", "")).strip():
        execution["completion_note"] = str(permit.get("handback_note", ""))
    if str(permit.get("closed_at", "")).strip():
        execution["completed_at"] = str(permit.get("closed_at", ""))
    job["execution_info"] = execution
    job["linked_hazard_ids"] = list(permit.get("linked_hazard_ids") or ())
    if str(permit.get("closed_at", "")).strip():
        job["closure_info"] = {
            "closed_by": str(permit.get("closed_by", "")),
            "closed_at": str(permit.get("closed_at", "")),
            "closure_note": str(permit.get("closure_note", "")),
            "linked_hazard_count": len(job["linked_hazard_ids"]),
        }
    job["data_label"] = str(permit.get("data_label", ""))
    job["created_at"] = str(permit.get("created_at", ""))
    job["updated_at"] = str(permit.get("updated_at", ""))
    return job


def _corrective_action_status(status: Any) -> str:
    return {
        "已关闭": "completed",
        "整改中": "in_progress",
    }.get(str(status).strip(), "planned")


def legacy_hazard_to_v5(record: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one V4 hazard dict into V5 hazard fields."""
    actions: list[dict[str, Any]] = []
    measure = str(record.get("整改措施", "")).strip()
    if measure:
        actions.append(
            {
                "action_text": measure,
                "owner_id": str(record.get("责任人", "")),
                "due_at": _normalise_date(record.get("整改期限", "")),
                "status": _corrective_action_status(record.get("状态", "")),
            }
        )
    evidence: list[dict[str, Any]] = []
    for item in record.get("rectification_evidence") or ():
        if isinstance(item, Mapping):
            evidence.append(
                {
                    "file_name": str(item.get("file_name", "")),
                    "evidence_type": str(item.get("evidence_type", "其他")),
                    "note": str(item.get("note", "")),
                    "uploaded_by": str(item.get("uploaded_by", "")),
                    "uploaded_at": str(item.get("uploaded_at", "")),
                }
            )
    verification: dict[str, Any] | None = None
    reviewer = str(record.get("reviewer", "")).strip()
    review_note = str(record.get("review_note", "")).strip()
    if str(record.get("状态", "")).strip() == "已关闭" and (reviewer or review_note):
        verification = {
            "result": "pass",
            "notes": review_note,
            "verifier_id": reviewer,
            "verified_at": _normalise_date(
                record.get("review_date", "") or record.get("closed_at", "")
            ),
        }
    data_label = str(record.get("数据性质", ""))
    return {
        "hazard_id": str(record.get("隐患编号", "")),
        "permit_id": str(record.get("related_job_id", "")),
        "title": str(record.get("隐患描述", "")),
        "description": str(record.get("隐患描述", "")),
        "hazard_type": str(record.get("隐患类型", "其他")),
        "risk_level": str(record.get("风险等级", "中")),
        "owner_id": str(record.get("责任人", "")),
        "reported_by_id": "",
        "due_at": _normalise_date(record.get("整改期限", "")),
        "status": hazard_status_to_v5(record.get("状态", "")),
        "verification_due_at": "",
        "verification": verification,
        "closed_by": str(record.get("closed_by", "")),
        "closed_at": _normalise_date(record.get("closed_at", "")),
        "corrective_actions": actions,
        "evidence": evidence,
        "data_label": data_label,
        "is_demo": is_demo_label(data_label),
        "created_at": _normalise_date(record.get("发现日期", "")),
    }


def v5_hazard_to_legacy(hazard: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a V5 hazard aggregate back into a V4-compatible dict."""
    actions = list(hazard.get("corrective_actions") or ())
    record: dict[str, Any] = {
        "隐患编号": str(hazard.get("id", "")),
        "隐患描述": str(hazard.get("description", "") or hazard.get("title", "")),
        "隐患类型": str(hazard.get("hazard_type", "其他")),
        "风险等级": str(hazard.get("risk_level", "中")),
        "责任人": str(hazard.get("owner_id", "")),
        "发现日期": _normalise_date(hazard.get("created_at", "")),
        "整改期限": _normalise_date(hazard.get("due_at", "")),
        "整改措施": "；".join(
            str(item.get("action_text", "")) for item in actions if item.get("action_text")
        ),
        "状态": v5_status_to_hazard(hazard.get("status", "")),
        "数据性质": str(hazard.get("data_label", "")),
    }
    if str(hazard.get("permit_id", "")).strip():
        record["related_job_id"] = str(hazard.get("permit_id", ""))
    if actions:
        record["rectification_evidence"] = [
            {
                "file_name": str(item.get("file_name", "")),
                "evidence_type": str(item.get("evidence_type", "其他")),
                "note": str(item.get("note", "")),
                "uploaded_by": str(item.get("uploaded_by", "")),
                "uploaded_at": str(item.get("uploaded_at", "")),
            }
            for item in hazard.get("evidence") or ()
        ]
    verifier = str(hazard.get("verifier_id", "")).strip()
    if verifier:
        record["reviewer"] = verifier
        record["review_date"] = _normalise_date(hazard.get("verified_at", ""))
        record["review_note"] = str(hazard.get("verification_notes", ""))
    if str(hazard.get("closed_by", "")).strip():
        record["closed_by"] = str(hazard.get("closed_by", ""))
    if str(hazard.get("closed_at", "")).strip():
        record["closed_at"] = _normalise_date(hazard.get("closed_at", ""))
    return record


__all__ = [
    "EVIDENCE_TRACK_PUBLIC",
    "EVIDENCE_TRACK_SDS",
    "hazard_status_to_v5",
    "is_demo_label",
    "job_status_to_v5",
    "job_to_permit",
    "legacy_hazard_to_v5",
    "permit_to_job",
    "v5_hazard_to_legacy",
    "v5_status_to_hazard",
    "v5_status_to_job",
]

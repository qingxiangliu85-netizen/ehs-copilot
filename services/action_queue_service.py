"""Derived action queue service.

The queue is generated on demand from permits, hazards, assignments and
persisted deadlines, then filtered with the same permission rules the services
enforce.  Nothing is stored: after a business command completes, the old item
simply stops being generated and the next-stage item appears.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Mapping

from workflow import action_queue, hazard_state, permit_state, permissions, roles

from . import hazard_service, permit_service


def _is_assigned(
    user: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    """Return whether the item is addressed to this user (or to everyone)."""
    assigned = str(candidate.get("assigned_to", "") or "").strip()
    if not assigned:
        return True
    if permissions.user_role(user) == roles.ROLE_DEMO_ADMIN:
        return True
    return assigned == permissions.user_id(user)


def _permit_candidates(permit: Mapping[str, Any]) -> list[dict[str, Any]]:
    status = str(permit.get("status", ""))
    title = str(permit.get("title", ""))
    entity_id = str(permit.get("id", ""))
    risk = str(
        permit.get("residual_risk_level", "") or permit.get("risk_level", "") or ""
    )
    approval_due = str(permit.get("approval_due_at", "") or "")
    owner = str(permit.get("owner_id", "") or permit.get("applicant_id", "") or "")
    reviewer = str(permit.get("ehs_reviewer_id", "") or "")
    approver = str(permit.get("designated_approver_id", "") or "")
    base = {
        "entity_type": "permit",
        "entity_id": entity_id,
        "title": title,
        "risk_level": risk,
        "target_page": "permit_detail",
        "target_id": entity_id,
    }
    items: list[dict[str, Any]] = []
    if status == permit_state.PERMIT_EHS_REVIEW:
        items.append(
            {
                **base,
                "action_type": action_queue.ACTION_EHS_REVIEW,
                "reason": "待EHS审核：核对 SDS/JSA 并给出结论",
                "assigned_to": reviewer,
                "due_at": approval_due,
                "permission": permissions.PERMIT_EHS_REVIEW,
            }
        )
    elif status == permit_state.PERMIT_APPROVAL_PENDING:
        items.append(
            {
                **base,
                "action_type": action_queue.ACTION_APPROVE,
                "reason": "待审批：批准后方可开工",
                "assigned_to": approver,
                "due_at": approval_due,
                "permission": permissions.PERMIT_APPROVE,
            }
        )
    elif status == permit_state.PERMIT_APPROVED:
        items.append(
            {
                **base,
                "action_type": action_queue.ACTION_PRESTART_CONFIRM,
                "reason": "待开工确认：完成开工前检查并激活作业",
                "assigned_to": owner,
                "due_at": "",
                "permission": permissions.PERMIT_PRESTART_CONFIRM,
            }
        )
    elif status == permit_state.PERMIT_SUSPENDED:
        items.append(
            {
                **base,
                "action_type": action_queue.ACTION_RESUME,
                "reason": "作业已暂停：确认现场条件后恢复作业",
                "assigned_to": owner,
                "due_at": "",
                "permission": permissions.PERMIT_RESUME,
            }
        )
    elif status == permit_state.PERMIT_CLOSEOUT_REVIEW:
        items.append(
            {
                **base,
                "action_type": action_queue.ACTION_CLOSE,
                "reason": "待关闭：确认关联隐患全部关闭后闭合许可",
                "assigned_to": reviewer,
                "due_at": "",
                "permission": permissions.PERMIT_CLOSE,
            }
        )
    return items


def _hazard_candidates(hazard: Mapping[str, Any]) -> list[dict[str, Any]]:
    status = str(hazard.get("status", ""))
    title = str(hazard.get("title", ""))
    entity_id = str(hazard.get("id", ""))
    risk = str(hazard.get("risk_level", "") or "")
    owner = str(hazard.get("owner_id", "") or "")
    verifier = str(hazard.get("verifier_id", "") or "")
    rectification_due = str(hazard.get("due_at", "") or "")
    verification_due = str(hazard.get("verification_due_at", "") or "")
    base = {
        "entity_type": "hazard",
        "entity_id": entity_id,
        "title": title,
        "risk_level": risk,
        "target_page": "hazard_detail",
        "target_id": entity_id,
    }
    items: list[dict[str, Any]] = []
    if status in (hazard_state.HAZARD_OPEN, hazard_state.HAZARD_REOPENED):
        items.append(
            {
                **base,
                "action_type": action_queue.ACTION_ASSIGN_HAZARD,
                "reason": "待指派整改负责人与整改期限",
                "assigned_to": "",
                "due_at": rectification_due,
                "permission": permissions.HAZARD_ASSIGN,
            }
        )
    elif status == hazard_state.HAZARD_ASSIGNED:
        items.append(
            {
                **base,
                "action_type": action_queue.ACTION_START_RECTIFICATION,
                "reason": "待整改：按整改措施执行并留存证据",
                "assigned_to": owner,
                "due_at": rectification_due,
                "permission": permissions.HAZARD_START_RECTIFICATION,
            }
        )
    elif status == hazard_state.HAZARD_IN_PROGRESS:
        items.append(
            {
                **base,
                "action_type": action_queue.ACTION_SUBMIT_RECTIFICATION,
                "reason": "待提交整改证据并送 EHS 验证",
                "assigned_to": owner,
                "due_at": rectification_due,
                "permission": permissions.HAZARD_SUBMIT_RECTIFICATION,
            }
        )
    elif status == hazard_state.HAZARD_VERIFICATION_PENDING:
        items.append(
            {
                **base,
                "action_type": action_queue.ACTION_VERIFY,
                "reason": "待验证：复查整改有效性与证据",
                "assigned_to": verifier,
                "due_at": verification_due,
                "permission": permissions.HAZARD_VERIFY,
            }
        )
    return items


def build_action_queue(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    *,
    now: datetime | None = None,
    view: str | None = action_queue.QUEUE_VIEW_ALL,
) -> list[dict[str, Any]]:
    """Return the permission-filtered, ranked action queue for one user."""
    moment = now or datetime.now()
    candidates: list[dict[str, Any]] = []
    for row in permit_service.list_permits(connection, limit=1000):
        permit = permit_service.get_permit(connection, str(row["id"]))
        if permit is None:
            continue
        for candidate in _permit_candidates(permit):
            if not _is_assigned(user, candidate):
                continue
            if permissions.can(user, str(candidate.get("permission", "")), entity=permit):
                candidates.append(candidate)
    for row in hazard_service.list_hazards(connection, limit=1000):
        hazard = hazard_service.get_hazard(connection, str(row["id"]))
        if hazard is None:
            continue
        for candidate in _hazard_candidates(hazard):
            if not _is_assigned(user, candidate):
                continue
            if permissions.can(user, str(candidate.get("permission", "")), entity=hazard):
                candidates.append(candidate)
    ranked = action_queue.build_action_queue(candidates, now=moment)
    return [item.to_dict() for item in action_queue.filter_view(ranked, view)]


def queue_summary(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Return small counts for a UI header (still derived, never stored)."""
    items = build_action_queue(connection, user, now=now)
    overdue = sum(1 for item in items if item["is_overdue"])
    due = sum(
        1
        for item in items
        if item["deadline_status"] in ("due_soon", "due_today")
    )
    return {
        "total": len(items),
        "overdue": overdue,
        "due_soon": due,
    }


__all__ = ["build_action_queue", "queue_summary"]

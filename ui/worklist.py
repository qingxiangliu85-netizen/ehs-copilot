"""「我的待办」 for the business UI.

The list is derived, never stored.  It combines two read-only sources:

1. the P0B derived action queue (already permission-filtered, deadline-classified
   and ranked) — review, approval, start-work, rectification and verification;
2. the permits the signed-in persona owns and still has to move forward
   (draft → submit, returned → resubmit), where the allowed actions come from
   ``workflow.actions`` so nothing is duplicated here.

Every row carries the business action buttons the persona may press, taken from
the same helper the detail page uses.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Mapping

from services import action_queue_service, permit_service
from workflow import action_queue, hazard_state, permit_state, roles, sla
from workflow import actions as workflow_actions

from . import common

FILTER_ALL = "全部"
FILTER_REVIEW = "待审核/审批"
FILTER_RECTIFY = "待整改"
FILTER_VERIFY = "待验证"
FILTER_DUE = "即将到期"
FILTER_OVERDUE = "已逾期"

WORKLIST_FILTERS: tuple[str, ...] = (
    FILTER_ALL,
    FILTER_REVIEW,
    FILTER_RECTIFY,
    FILTER_VERIFY,
    FILTER_DUE,
    FILTER_OVERDUE,
)

CATEGORY_REVIEW = "待审核/审批"
CATEGORY_RECTIFY = "待整改"
CATEGORY_VERIFY = "待验证"
CATEGORY_KICKOFF = "待开工/关闭"
CATEGORY_SUBMIT = "待提交"

_CATEGORY_BY_ACTION: dict[str, str] = {
    action_queue.ACTION_EHS_REVIEW: CATEGORY_REVIEW,
    action_queue.ACTION_APPROVE: CATEGORY_REVIEW,
    action_queue.ACTION_ASSIGN_HAZARD: CATEGORY_RECTIFY,
    action_queue.ACTION_START_RECTIFICATION: CATEGORY_RECTIFY,
    action_queue.ACTION_SUBMIT_RECTIFICATION: CATEGORY_RECTIFY,
    action_queue.ACTION_VERIFY: CATEGORY_VERIFY,
    action_queue.ACTION_PRESTART_CONFIRM: CATEGORY_KICKOFF,
    action_queue.ACTION_RESUME: CATEGORY_KICKOFF,
    action_queue.ACTION_CLOSE: CATEGORY_KICKOFF,
    "submit": CATEGORY_SUBMIT,
    "resubmit": CATEGORY_SUBMIT,
}

# The button the queue's own task description refers to.  Without this the
# Demo Admin (who may do everything) would be offered a different action than
# the one the row is actually about.
_PREFERRED_ACTION: dict[str, str] = {
    action_queue.ACTION_EHS_REVIEW: "confirm",
    action_queue.ACTION_APPROVE: "approve",
    action_queue.ACTION_PRESTART_CONFIRM: "prestart_confirm",
    action_queue.ACTION_RESUME: "resume",
    action_queue.ACTION_CLOSE: "close",
    action_queue.ACTION_ASSIGN_HAZARD: "assign",
    action_queue.ACTION_START_RECTIFICATION: "start",
    action_queue.ACTION_SUBMIT_RECTIFICATION: "submit_rectification",
    action_queue.ACTION_VERIFY: "verify_pass",
    "submit": "submit",
    "resubmit": "resubmit",
}

ACTION_TYPE_LABELS: dict[str, str] = {
    action_queue.ACTION_EHS_REVIEW: "待EHS审核",
    action_queue.ACTION_APPROVE: "待审批",
    action_queue.ACTION_PRESTART_CONFIRM: "待开工确认",
    action_queue.ACTION_RESUME: "待恢复作业",
    action_queue.ACTION_CLOSE: "待关闭",
    action_queue.ACTION_ASSIGN_HAZARD: "待指派整改",
    action_queue.ACTION_START_RECTIFICATION: "待整改",
    action_queue.ACTION_SUBMIT_RECTIFICATION: "待提交整改证据",
    action_queue.ACTION_VERIFY: "待验证",
    "submit": "待提交",
    "resubmit": "待重新提交",
}

# Where a row's primary button takes the reader.
_TARGET_PAGE: dict[str, str] = {
    "permit": common.PAGE_PERMITS,
    "hazard": common.PAGE_HAZARDS,
}


def label_for_action_type(action_type: Any) -> str:
    """Return the business label of one queue action type."""
    text = str(action_type or "").strip()
    return ACTION_TYPE_LABELS.get(text, text or "待处理")


def category_for_action_type(action_type: Any) -> str:
    """Return the UI filter category of one queue action type."""
    text = str(action_type or "").strip()
    return _CATEGORY_BY_ACTION.get(text, CATEGORY_KICKOFF)


def target_page(entity_type: Any) -> str:
    """Return the detail page that owns one entity type."""
    return _TARGET_PAGE.get(str(entity_type or "").strip(), common.PAGE_TODAY)


def actions_for(
    entity_type: str,
    record: Mapping[str, Any] | None,
    user: Mapping[str, Any],
) -> list[workflow_actions.Action]:
    """Return the actions one persona may press on one record."""
    try:
        return workflow_actions.get_available_actions(entity_type, record, user)
    except ValueError:
        return []


def _load_record(
    connection: sqlite3.Connection, entity_type: str, entity_id: str
) -> dict[str, Any] | None:
    if entity_type == "permit":
        return permit_service.get_permit(connection, entity_id)
    if entity_type == "hazard":
        from services import hazard_service

        return hazard_service.get_hazard(connection, entity_id)
    return None


def _rank_actions(
    action_type: str, actions: list[workflow_actions.Action]
) -> list[workflow_actions.Action]:
    """Put the action the row is about first, keeping the rest in order."""
    preferred = _PREFERRED_ACTION.get(str(action_type or "").strip())
    if not preferred:
        return actions
    head = [action for action in actions if action.code == preferred]
    if not head:
        return actions
    return head + [action for action in actions if action.code != preferred]


def _row(
    item: Mapping[str, Any],
    actions: list[workflow_actions.Action],
) -> dict[str, Any]:
    action_type = str(item.get("action_type", ""))
    actions = _rank_actions(action_type, actions)
    return {
        "entity_type": str(item.get("entity_type", "")),
        "entity_id": str(item.get("entity_id", "")),
        "title": str(item.get("title", "")),
        "action_type": action_type,
        "action_label": label_for_action_type(action_type),
        "category": category_for_action_type(action_type),
        "reason": str(item.get("reason", "")),
        "risk_level": str(item.get("risk_level", "")),
        "assigned_to": str(item.get("assigned_to", "")),
        "due_at": str(item.get("due_at", "")),
        "deadline_status": str(item.get("deadline_status", "")),
        "is_overdue": bool(item.get("is_overdue")),
        "priority_score": int(item.get("priority_score", 0) or 0),
        "target_page": target_page(item.get("entity_type")),
        "actions": actions,
        "primary": workflow_actions.primary_action(actions),
    }


def _own_permit_rows(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    moment: datetime,
    seen: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Return the permits the persona still has to submit or resubmit."""
    user_id = str(user.get("id", ""))
    is_admin = str(user.get("role", "")) == roles.ROLE_DEMO_ADMIN
    rows: list[dict[str, Any]] = []
    for summary in permit_service.list_permits(connection, limit=500):
        permit_id = str(summary["id"])
        if ("permit", permit_id) in seen:
            continue
        status = str(summary.get("status", ""))
        if status not in (permit_state.PERMIT_DRAFT, permit_state.PERMIT_RETURNED):
            continue
        applicant = str(summary.get("applicant_id", "") or "")
        if not is_admin and applicant != user_id:
            continue
        record = permit_service.get_permit(connection, permit_id)
        actions = actions_for("permit", record, user)
        if not actions or record is None:
            continue
        action_type = (
            "submit" if status == permit_state.PERMIT_DRAFT else "resubmit"
        )
        risk = str(
            record.get("residual_risk_level", "") or record.get("risk_level", "")
        )
        reason = (
            "待提交EHS审核：确认化学品、SDS证据与JSA后提交"
            if action_type == "submit"
            else "审批已驳回：修改后重新提交EHS审核"
        )
        item = {
            "entity_type": "permit",
            "entity_id": permit_id,
            "title": str(record.get("title", "")),
            "action_type": action_type,
            "risk_level": risk,
            "reason": reason,
            "assigned_to": applicant or user_id,
            "due_at": str(record.get("approval_due_at", "") or ""),
            "deadline_status": sla.classify_deadline(
                record.get("approval_due_at", ""), moment
            ),
            "is_overdue": sla.is_overdue(record.get("approval_due_at", ""), moment),
            "priority_score": sla.priority_score(
                risk, sla.classify_deadline(record.get("approval_due_at", ""), moment)
            ),
        }
        row = _row(item, actions)
        row["is_own_draft"] = True
        rows.append(row)
    return rows


def build_worklist(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    *,
    moment: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return the ranked, permission-filtered 「我的待办」 for one persona."""
    stamp = moment or common.now()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in action_queue_service.build_action_queue(connection, user, now=stamp):
        entity_type = str(item.get("entity_type", ""))
        entity_id = str(item.get("entity_id", ""))
        record = _load_record(connection, entity_type, entity_id)
        if record is None:
            continue
        seen.add((entity_type, entity_id))
        rows.append(_row(item, actions_for(entity_type, record, user)))
    rows.extend(_own_permit_rows(connection, user, stamp, seen))
    rows.sort(
        key=lambda row: (
            -int(row["priority_score"]),
            str(row["due_at"] or "9999"),
            str(row["entity_id"]),
            str(row["action_type"]),
        )
    )
    return rows


def filter_worklist(
    rows: list[dict[str, Any]], selection: str
) -> list[dict[str, Any]]:
    """Apply one 「我的待办」 filter chip."""
    choice = str(selection or FILTER_ALL).strip()
    if choice in ("", FILTER_ALL):
        return list(rows)
    if choice == FILTER_OVERDUE:
        return [row for row in rows if row["is_overdue"]]
    if choice == FILTER_DUE:
        return [
            row
            for row in rows
            if not row["is_overdue"]
            and row["deadline_status"] in (sla.DEADLINE_DUE_SOON, sla.DEADLINE_DUE_TODAY)
        ]
    return [row for row in rows if row["category"] == choice]


def worklist_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Return the four headline numbers of the 今日工作台."""
    return {
        "total": len(rows),
        "overdue": sum(1 for row in rows if row["is_overdue"]),
        "due_today": sum(
            1 for row in rows if row["deadline_status"] == sla.DEADLINE_DUE_TODAY
        ),
        "high_risk": sum(1 for row in rows if common.is_high_risk(row["risk_level"])),
    }


def status_label_for_row(
    connection: sqlite3.Connection, row: Mapping[str, Any]
) -> str:
    """Return the current business status of one worklist row's entity."""
    entity_type = str(row.get("entity_type", ""))
    entity_id = str(row.get("entity_id", ""))
    if entity_type == "permit":
        permit = permit_service.get_permit(connection, entity_id)
        return common.permit_status_label(permit.get("status")) if permit else "—"
    if entity_type == "hazard":
        from services import hazard_service

        hazard = hazard_service.get_hazard(connection, entity_id)
        return common.hazard_status_label(hazard.get("status")) if hazard else "—"
    return "—"


__all__ = [
    "ACTION_TYPE_LABELS",
    "CATEGORY_KICKOFF",
    "CATEGORY_RECTIFY",
    "CATEGORY_REVIEW",
    "CATEGORY_SUBMIT",
    "CATEGORY_VERIFY",
    "FILTER_ALL",
    "FILTER_DUE",
    "FILTER_OVERDUE",
    "FILTER_RECTIFY",
    "FILTER_REVIEW",
    "FILTER_VERIFY",
    "WORKLIST_FILTERS",
    "actions_for",
    "build_worklist",
    "category_for_action_type",
    "filter_worklist",
    "label_for_action_type",
    "status_label_for_row",
    "target_page",
    "worklist_summary",
]

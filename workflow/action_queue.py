"""Read-only action queue projection.

The queue is **not** a second source of truth: it is derived on every call
from permits, hazards, assignments and persisted deadlines.  This module keeps
the pure ranking rules (dedupe, deadline classification, priority ordering)
while :mod:`services.action_queue_service` performs the database queries and
permission filtering.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from . import sla


ACTION_EHS_REVIEW = "ehs_review"
ACTION_APPROVE = "approve"
ACTION_PRESTART_CONFIRM = "prestart_confirm"
ACTION_CLOSE = "close"
ACTION_ASSIGN_HAZARD = "assign_hazard"
ACTION_START_RECTIFICATION = "start_rectification"
ACTION_SUBMIT_RECTIFICATION = "submit_rectification"
ACTION_VERIFY = "verify"

ACTION_TYPES: tuple[str, ...] = (
    ACTION_EHS_REVIEW,
    ACTION_APPROVE,
    ACTION_PRESTART_CONFIRM,
    ACTION_CLOSE,
    ACTION_ASSIGN_HAZARD,
    ACTION_START_RECTIFICATION,
    ACTION_SUBMIT_RECTIFICATION,
    ACTION_VERIFY,
)

QUEUE_VIEW_ALL = "all"
QUEUE_VIEW_DUE = "due"
QUEUE_VIEW_OVERDUE = "overdue"

QUEUE_VIEWS: tuple[str, ...] = (QUEUE_VIEW_ALL, QUEUE_VIEW_DUE, QUEUE_VIEW_OVERDUE)


@dataclass(frozen=True)
class ActionItem:
    """One readable row of the derived action queue."""

    entity_type: str
    entity_id: str
    title: str
    action_type: str
    reason: str
    risk_level: str
    assigned_to: str
    due_at: str
    deadline_status: str
    is_overdue: bool
    priority_score: int
    target_page: str
    target_id: str
    permission: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "title": self.title,
            "action_type": self.action_type,
            "reason": self.reason,
            "risk_level": self.risk_level,
            "assigned_to": self.assigned_to,
            "due_at": self.due_at,
            "deadline_status": self.deadline_status,
            "is_overdue": self.is_overdue,
            "priority_score": self.priority_score,
            "target_page": self.target_page,
            "target_id": self.target_id,
        }


def item_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return the dedupe key of one candidate item."""
    return (
        str(item.get("entity_type", "")).strip(),
        str(item.get("entity_id", "")).strip(),
        str(item.get("action_type", "")).strip(),
    )


def _as_item(candidate: Mapping[str, Any], now: datetime | None) -> ActionItem:
    due_at = str(candidate.get("due_at", "") or "").strip()
    status = sla.classify_deadline(due_at, now)
    entity_id = str(candidate.get("entity_id", "")).strip()
    return ActionItem(
        entity_type=str(candidate.get("entity_type", "")).strip(),
        entity_id=entity_id,
        title=str(candidate.get("title", "")).strip(),
        action_type=str(candidate.get("action_type", "")).strip(),
        reason=str(candidate.get("reason", "")).strip(),
        risk_level=str(candidate.get("risk_level", "")).strip(),
        assigned_to=str(candidate.get("assigned_to", "")).strip(),
        due_at=due_at,
        deadline_status=status,
        is_overdue=status == sla.DEADLINE_OVERDUE,
        priority_score=sla.priority_score(
            str(candidate.get("risk_level", "")), status
        ),
        target_page=str(candidate.get("target_page", "")).strip(),
        target_id=str(candidate.get("target_id", "") or entity_id).strip(),
        permission=str(candidate.get("permission", "")).strip(),
    )


def deduplicate(candidates: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Keep the first candidate for each entity/action pair (stable order)."""
    seen: set[tuple[str, str, str]] = set()
    unique: list[Mapping[str, Any]] = []
    for candidate in candidates:
        key = item_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def build_action_queue(
    candidates: Iterable[Mapping[str, Any]], *, now: datetime | None = None
) -> tuple[ActionItem, ...]:
    """Deduplicate, classify and rank candidate items into the queue."""
    items = [_as_item(candidate, now) for candidate in deduplicate(candidates)]
    return tuple(
        sorted(
            items,
            key=lambda item: (
                -item.priority_score,
                item.due_at or "9999",
                item.entity_type,
                item.entity_id,
                item.action_type,
            ),
        )
    )


def filter_view(
    items: Iterable[ActionItem], view: str | None = None
) -> tuple[ActionItem, ...]:
    """Filter the ranked queue for a UI view (all / due / overdue)."""
    value = str(view or QUEUE_VIEW_ALL).strip()
    rows = tuple(items)
    if value in ("", QUEUE_VIEW_ALL):
        return rows
    if value == QUEUE_VIEW_OVERDUE:
        return tuple(item for item in rows if item.is_overdue)
    if value == QUEUE_VIEW_DUE:
        return tuple(
            item
            for item in rows
            if item.deadline_status
            in (sla.DEADLINE_DUE_SOON, sla.DEADLINE_DUE_TODAY)
        )
    raise ValueError(f"未知待办视图：{view!r}。")


__all__ = [
    "ACTION_APPROVE",
    "ACTION_ASSIGN_HAZARD",
    "ACTION_CLOSE",
    "ACTION_EHS_REVIEW",
    "ACTION_PRESTART_CONFIRM",
    "ACTION_START_RECTIFICATION",
    "ACTION_SUBMIT_RECTIFICATION",
    "ACTION_TYPES",
    "ACTION_VERIFY",
    "ActionItem",
    "QUEUE_VIEWS",
    "QUEUE_VIEW_ALL",
    "QUEUE_VIEW_DUE",
    "QUEUE_VIEW_OVERDUE",
    "build_action_queue",
    "deduplicate",
    "filter_view",
    "item_key",
]

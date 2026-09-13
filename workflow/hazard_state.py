"""Pure V5 hazard state machine.

States::

    open → assigned → in_progress → verification_pending → closed
    verification_pending → reopened → assigned
    closed → reopened (reason required)

Rectification and verification are separate steps: the person who executes the
rectification can never verify or close it, and a closed hazard can only be
re-opened through an explicit command that records a reason.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import roles as role_codes
from .permit_state import (
    CODE_ILLEGAL_TRANSITION,
    CODE_OK,
    CODE_PERMISSION_DENIED,
    CODE_REASON_REQUIRED,
    CODE_UNKNOWN_STATUS,
    TransitionResult,
)


HAZARD_OPEN = "open"
HAZARD_ASSIGNED = "assigned"
HAZARD_IN_PROGRESS = "in_progress"
HAZARD_VERIFICATION_PENDING = "verification_pending"
HAZARD_CLOSED = "closed"
HAZARD_REOPENED = "reopened"

HAZARD_STATUSES: tuple[str, ...] = (
    HAZARD_OPEN,
    HAZARD_ASSIGNED,
    HAZARD_IN_PROGRESS,
    HAZARD_VERIFICATION_PENDING,
    HAZARD_CLOSED,
    HAZARD_REOPENED,
)

HAZARD_TERMINAL_STATUSES: tuple[str, ...] = (HAZARD_CLOSED,)

HAZARD_TRANSITIONS: dict[str, dict[str, str]] = {
    HAZARD_OPEN: {"assign": HAZARD_ASSIGNED},
    HAZARD_ASSIGNED: {
        "assign": HAZARD_ASSIGNED,
        "start": HAZARD_IN_PROGRESS,
    },
    HAZARD_IN_PROGRESS: {"submit_rectification": HAZARD_VERIFICATION_PENDING},
    HAZARD_VERIFICATION_PENDING: {
        "verify_pass": HAZARD_CLOSED,
        "verify_fail": HAZARD_REOPENED,
    },
    HAZARD_CLOSED: {"reopen": HAZARD_REOPENED},
    HAZARD_REOPENED: {"assign": HAZARD_ASSIGNED},
}

CODE_MISSING_OWNER = "missing_owner"
CODE_OWNER_MISMATCH = "owner_mismatch"
CODE_SEPARATION_REQUIRED = "separation_required"
CODE_EVIDENCE_REQUIRED = "evidence_required"
CODE_VERIFICATION_REQUIRED = "verification_required"

HAZARD_ACTION_ROLES: dict[str, tuple[str, ...]] = {
    "assign": (role_codes.ROLE_EHS_REVIEWER, role_codes.ROLE_DEMO_ADMIN),
    "start": (role_codes.ROLE_ACTION_OWNER, role_codes.ROLE_DEMO_ADMIN),
    "submit_rectification": (
        role_codes.ROLE_ACTION_OWNER,
        role_codes.ROLE_DEMO_ADMIN,
    ),
    "verify_pass": (role_codes.ROLE_EHS_REVIEWER, role_codes.ROLE_DEMO_ADMIN),
    "verify_fail": (role_codes.ROLE_EHS_REVIEWER, role_codes.ROLE_DEMO_ADMIN),
    "reopen": (role_codes.ROLE_EHS_REVIEWER, role_codes.ROLE_DEMO_ADMIN),
}

HAZARD_REASON_ACTIONS: frozenset[str] = frozenset({"verify_fail", "reopen"})

_OWNER_ONLY_ACTIONS = frozenset({"start", "submit_rectification"})
_SEPARATION_ACTIONS = frozenset({"verify_pass", "verify_fail"})


def _count(context: Mapping[str, Any], key: str) -> int:
    try:
        return int(context.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def validate_hazard_transition(
    from_status: str,
    action: str,
    *,
    actor_role: str = "",
    actor_id: str = "",
    owner_id: str = "",
    reason: str = "",
    context: Mapping[str, Any] | None = None,
) -> TransitionResult:
    """Return whether ``action`` may move a hazard out of ``from_status``."""
    current = str(from_status or "").strip()
    desired = str(action or "").strip()
    values = dict(context or {})
    actor = str(actor_id or "").strip()
    owner = str(owner_id or "").strip()
    demo_admin = str(actor_role or "").strip() == role_codes.ROLE_DEMO_ADMIN

    if current not in HAZARD_STATUSES:
        return TransitionResult(
            False, CODE_UNKNOWN_STATUS, f"未知隐患状态：{current!r}。"
        )

    transitions = HAZARD_TRANSITIONS.get(current, {})
    if desired not in transitions:
        return TransitionResult(
            False,
            CODE_ILLEGAL_TRANSITION,
            f"非法状态流转：{current} 不允许执行「{desired}」。",
        )

    allowed_roles = HAZARD_ACTION_ROLES.get(desired, ())
    role = str(actor_role or "").strip()
    if allowed_roles and role not in allowed_roles:
        return TransitionResult(
            False,
            CODE_PERMISSION_DENIED,
            f"角色 {role or '（未提供）'} 无权执行「{desired}」。",
        )

    if desired in HAZARD_REASON_ACTIONS and not str(reason or "").strip():
        return TransitionResult(
            False,
            CODE_REASON_REQUIRED,
            f"执行「{desired}」必须填写原因（reason）。",
        )

    if desired == "assign":
        target = str(values.get("target_owner_id", "") or "").strip()
        if not target:
            return TransitionResult(
                False, CODE_MISSING_OWNER, "指派隐患必须提供整改负责人（owner）。"
            )

    if desired in _OWNER_ONLY_ACTIONS and not demo_admin:
        if not owner:
            return TransitionResult(
                False,
                CODE_MISSING_OWNER,
                "该操作只能由整改负责人执行，但隐患尚未指派负责人。",
            )
        if actor != owner:
            return TransitionResult(
                False,
                CODE_OWNER_MISMATCH,
                f"该操作只能由整改负责人 {owner} 执行，当前操作人为 {actor or '（未提供）'}。",
            )

    if desired in _SEPARATION_ACTIONS and owner and actor == owner:
        return TransitionResult(
            False,
            CODE_SEPARATION_REQUIRED,
            f"整改负责人 {owner} 不能验证或关闭自己提交的整改。",
        )

    if desired == "submit_rectification":
        if _count(values, "corrective_action_count") < 1:
            return TransitionResult(
                False, CODE_EVIDENCE_REQUIRED, "提交整改前至少需要 1 条整改措施。"
            )
        if _count(values, "evidence_count") < 1:
            return TransitionResult(
                False, CODE_EVIDENCE_REQUIRED, "提交整改前至少需要 1 条整改证据。"
            )

    if desired == "verify_pass" and not str(
        values.get("verification_notes", "") or ""
    ).strip():
        return TransitionResult(
            False, CODE_VERIFICATION_REQUIRED, "验证通过必须填写验证说明。"
        )

    if desired == "verify_fail" and not (
        str(reason or "").strip()
        or str(values.get("verification_notes", "") or "").strip()
    ):
        return TransitionResult(
            False, CODE_VERIFICATION_REQUIRED, "验证不通过必须填写原因或验证说明。"
        )

    target = transitions[desired]
    return TransitionResult(True, CODE_OK, f"允许 {current} → {target}。", target)


def can_transition(from_status: str, action: str) -> bool:
    """Return whether the action exists for the status, ignoring context."""
    return str(action) in HAZARD_TRANSITIONS.get(str(from_status), {})


V4_HAZARD_STATUS_TO_V5: dict[str, str] = {
    "待整改": HAZARD_OPEN,
    "整改中": HAZARD_IN_PROGRESS,
    "已关闭": HAZARD_CLOSED,
}

# V4 only knows three statuses; the V5-only states collapse to their closest
# legacy meaning for compatibility display.
V5_TO_V4_HAZARD_STATUS: dict[str, str] = {
    HAZARD_OPEN: "待整改",
    HAZARD_ASSIGNED: "待整改",
    HAZARD_IN_PROGRESS: "整改中",
    HAZARD_VERIFICATION_PENDING: "整改中",
    HAZARD_REOPENED: "整改中",
    HAZARD_CLOSED: "已关闭",
}


__all__ = [
    "CODE_EVIDENCE_REQUIRED",
    "CODE_MISSING_OWNER",
    "CODE_OWNER_MISMATCH",
    "CODE_SEPARATION_REQUIRED",
    "CODE_VERIFICATION_REQUIRED",
    "HAZARD_ACTION_ROLES",
    "HAZARD_ASSIGNED",
    "HAZARD_CLOSED",
    "HAZARD_IN_PROGRESS",
    "HAZARD_OPEN",
    "HAZARD_REASON_ACTIONS",
    "HAZARD_REOPENED",
    "HAZARD_STATUSES",
    "HAZARD_TERMINAL_STATUSES",
    "HAZARD_TRANSITIONS",
    "HAZARD_VERIFICATION_PENDING",
    "V4_HAZARD_STATUS_TO_V5",
    "V5_TO_V4_HAZARD_STATUS",
    "can_transition",
    "validate_hazard_transition",
]

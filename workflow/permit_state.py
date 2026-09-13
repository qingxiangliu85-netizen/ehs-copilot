"""Pure V5 permit state machine.

The machine is data-driven and side-effect free: callers ask
``validate_permit_transition`` whether one action is allowed and why, while the
service layer is the only component that persists the new state and writes the
audit event.

Canonical statuses are English identifiers.  The mapping tables at the bottom
keep the frozen V4 ``jobs.py`` records compatible without modifying that module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping

from hazards import RISK_LEVELS

from . import roles as role_codes


PERMIT_DRAFT = "draft"
PERMIT_EHS_REVIEW = "ehs_review"
PERMIT_APPROVAL_PENDING = "approval_pending"
PERMIT_APPROVED = "approved"
PERMIT_ACTIVE = "active"
PERMIT_CLOSEOUT_REVIEW = "closeout_review"
PERMIT_CLOSED = "closed"
PERMIT_RETURNED = "returned"
PERMIT_SUSPENDED = "suspended"
PERMIT_CANCELLED = "cancelled"
PERMIT_EXPIRED = "expired"

PERMIT_STATUSES: tuple[str, ...] = (
    PERMIT_DRAFT,
    PERMIT_EHS_REVIEW,
    PERMIT_APPROVAL_PENDING,
    PERMIT_APPROVED,
    PERMIT_ACTIVE,
    PERMIT_SUSPENDED,
    PERMIT_CLOSEOUT_REVIEW,
    PERMIT_CLOSED,
    PERMIT_RETURNED,
    PERMIT_CANCELLED,
    PERMIT_EXPIRED,
)

PERMIT_TERMINAL_STATUSES: tuple[str, ...] = (
    PERMIT_CLOSED,
    PERMIT_CANCELLED,
    PERMIT_EXPIRED,
)

PERMIT_ACTIVE_STATUSES: tuple[str, ...] = (PERMIT_ACTIVE, PERMIT_SUSPENDED)

HIGH_RISK_LEVELS: tuple[str, ...] = ("高", "重大")

# from_status -> {action: to_status}
PERMIT_TRANSITIONS: dict[str, dict[str, str]] = {
    PERMIT_DRAFT: {
        "submit": PERMIT_EHS_REVIEW,
        "cancel": PERMIT_CANCELLED,
    },
    PERMIT_EHS_REVIEW: {
        "return_draft": PERMIT_DRAFT,
        "confirm": PERMIT_APPROVAL_PENDING,
        "cancel": PERMIT_CANCELLED,
    },
    PERMIT_APPROVAL_PENDING: {
        "approve": PERMIT_APPROVED,
        "reject": PERMIT_RETURNED,
        "cancel": PERMIT_CANCELLED,
    },
    PERMIT_RETURNED: {
        "resubmit": PERMIT_EHS_REVIEW,
        "cancel": PERMIT_CANCELLED,
    },
    PERMIT_APPROVED: {
        "prestart_confirm": PERMIT_ACTIVE,
        "cancel": PERMIT_CANCELLED,
        "expire": PERMIT_EXPIRED,
    },
    PERMIT_ACTIVE: {
        "suspend": PERMIT_SUSPENDED,
        "complete_work": PERMIT_CLOSEOUT_REVIEW,
        "expire": PERMIT_EXPIRED,
    },
    PERMIT_SUSPENDED: {
        "resume": PERMIT_ACTIVE,
        "expire": PERMIT_EXPIRED,
    },
    PERMIT_CLOSEOUT_REVIEW: {
        "close": PERMIT_CLOSED,
        "return_to_active": PERMIT_ACTIVE,
    },
    PERMIT_CLOSED: {},
    PERMIT_CANCELLED: {},
    PERMIT_EXPIRED: {},
}

PERMIT_ACTION_ROLES: dict[str, tuple[str, ...]] = {
    "submit": (role_codes.ROLE_APPLICANT, role_codes.ROLE_DEMO_ADMIN),
    "return_draft": (role_codes.ROLE_EHS_REVIEWER, role_codes.ROLE_DEMO_ADMIN),
    "confirm": (role_codes.ROLE_EHS_REVIEWER, role_codes.ROLE_DEMO_ADMIN),
    "approve": (role_codes.ROLE_APPROVER, role_codes.ROLE_DEMO_ADMIN),
    "reject": (role_codes.ROLE_APPROVER, role_codes.ROLE_DEMO_ADMIN),
    "resubmit": (role_codes.ROLE_APPLICANT, role_codes.ROLE_DEMO_ADMIN),
    "prestart_confirm": (
        role_codes.ROLE_APPLICANT,
        role_codes.ROLE_ACTION_OWNER,
        role_codes.ROLE_DEMO_ADMIN,
    ),
    "suspend": (
        role_codes.ROLE_APPLICANT,
        role_codes.ROLE_EHS_REVIEWER,
        role_codes.ROLE_ACTION_OWNER,
        role_codes.ROLE_DEMO_ADMIN,
    ),
    "resume": (
        role_codes.ROLE_APPLICANT,
        role_codes.ROLE_EHS_REVIEWER,
        role_codes.ROLE_ACTION_OWNER,
        role_codes.ROLE_DEMO_ADMIN,
    ),
    "complete_work": (
        role_codes.ROLE_APPLICANT,
        role_codes.ROLE_ACTION_OWNER,
        role_codes.ROLE_DEMO_ADMIN,
    ),
    "close": (role_codes.ROLE_EHS_REVIEWER, role_codes.ROLE_DEMO_ADMIN),
    "return_to_active": (
        role_codes.ROLE_EHS_REVIEWER,
        role_codes.ROLE_DEMO_ADMIN,
    ),
    "cancel": (
        role_codes.ROLE_APPLICANT,
        role_codes.ROLE_EHS_REVIEWER,
        role_codes.ROLE_DEMO_ADMIN,
    ),
    "expire": (role_codes.ROLE_SYSTEM, role_codes.ROLE_DEMO_ADMIN),
}

PERMIT_REASON_ACTIONS: frozenset[str] = frozenset(
    {
        "return_draft",
        "reject",
        "suspend",
        "complete_work",
        "return_to_active",
        "cancel",
    }
)

CODE_OK = "ok"
CODE_UNKNOWN_STATUS = "unknown_status"
CODE_ILLEGAL_TRANSITION = "illegal_transition"
CODE_PERMISSION_DENIED = "permission_denied"
CODE_REASON_REQUIRED = "reason_required"
CODE_PRECONDITION_FAILED = "precondition_failed"


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of one state-machine question, safe to render or test."""

    allowed: bool
    code: str
    message: str = ""
    to_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "message": self.message,
            "to_status": self.to_status,
        }


def _count(context: Mapping[str, Any], key: str) -> int:
    try:
        return int(context.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _as_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.combine(date.fromisoformat(text), datetime.min.time())
    except ValueError:
        return None


def _submit_precondition(context: Mapping[str, Any], *, now: datetime | None) -> str:
    if _count(context, "chemical_count") < 1:
        return "提交前至少需要 1 个化学品。"
    if _count(context, "sds_evidence_count") < 1:
        return "提交前至少需要 1 条适用 SDS 证据；公开来源安全证据不能替代 SDS。"
    if _count(context, "jsa_item_count") < 1:
        return "提交前至少需要 1 条 JSA 记录。"
    return ""


def _confirm_precondition(context: Mapping[str, Any], *, now: datetime | None) -> str:
    level = str(context.get("residual_risk_level", "") or "").strip()
    if level not in RISK_LEVELS:
        return f"EHS 确认前必须填写有效的残余风险等级（当前：{level or '空'}）。"
    if level in HIGH_RISK_LEVELS and not str(
        context.get("control_measures", "") or ""
    ).strip():
        return "高/重大残余风险必须有明确控制措施。"
    return ""


def _approve_precondition(context: Mapping[str, Any], *, now: datetime | None) -> str:
    level = str(context.get("residual_risk_level", "") or "").strip()
    if level not in RISK_LEVELS:
        return "未完成 EHS 风险确认的作业许可不能批准。"
    return ""


def _prestart_precondition(context: Mapping[str, Any], *, now: datetime | None) -> str:
    if not bool(context.get("prestart_passed")):
        return "开工前检查未全部通过，不能激活作业许可。"
    valid_to = _as_datetime(context.get("valid_to", ""))
    if valid_to is None:
        return "作业许可缺少有效期（valid_to），不能激活。"
    moment = now or datetime.now()
    if moment > valid_to:
        return "作业许可已超过有效期，不能激活。"
    return ""


def _close_precondition(context: Mapping[str, Any], *, now: datetime | None) -> str:
    open_count = _count(context, "open_hazard_count")
    if open_count > 0:
        return f"仍有 {open_count} 条未关闭关联隐患，不能关闭作业许可。"
    return ""


def _expire_precondition(context: Mapping[str, Any], *, now: datetime | None) -> str:
    valid_to = _as_datetime(context.get("valid_to", ""))
    if valid_to is None:
        return "作业许可缺少有效期（valid_to），不能判定过期。"
    moment = now or datetime.now()
    if moment <= valid_to:
        return "作业许可尚未超过有效期，不能标记过期。"
    return ""


_PRECONDITIONS: dict[str, Callable[..., str]] = {
    "submit": _submit_precondition,
    "confirm": _confirm_precondition,
    "approve": _approve_precondition,
    "prestart_confirm": _prestart_precondition,
    "close": _close_precondition,
    "expire": _expire_precondition,
}


def validate_permit_transition(
    from_status: str,
    action: str,
    *,
    actor_role: str = "",
    actor_id: str = "",
    reason: str = "",
    context: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> TransitionResult:
    """Return whether ``action`` may move a permit out of ``from_status``."""
    current = str(from_status or "").strip()
    desired = str(action or "").strip()
    values = dict(context or {})

    if current not in PERMIT_STATUSES:
        return TransitionResult(
            False, CODE_UNKNOWN_STATUS, f"未知作业许可状态：{current!r}。"
        )

    transitions = PERMIT_TRANSITIONS.get(current, {})
    if desired not in transitions:
        return TransitionResult(
            False,
            CODE_ILLEGAL_TRANSITION,
            f"非法状态流转：{current} 不允许执行「{desired}」。",
        )

    allowed_roles = PERMIT_ACTION_ROLES.get(desired, ())
    actor = str(actor_role or "").strip()
    if allowed_roles and actor not in allowed_roles:
        return TransitionResult(
            False,
            CODE_PERMISSION_DENIED,
            f"角色 {actor or '（未提供）'} 无权执行「{desired}」。",
        )

    if desired in PERMIT_REASON_ACTIONS and not str(reason or "").strip():
        return TransitionResult(
            False,
            CODE_REASON_REQUIRED,
            f"执行「{desired}」必须填写原因（reason）。",
        )

    precondition = _PRECONDITIONS.get(desired)
    if precondition is not None:
        message = precondition(values, now=now)
        if message:
            return TransitionResult(False, CODE_PRECONDITION_FAILED, message)

    target = transitions[desired]
    return TransitionResult(
        True, CODE_OK, f"允许 {current} → {target}。", target
    )


def can_transition(from_status: str, action: str) -> bool:
    """Return whether the action exists for the status, ignoring context."""
    return str(action) in PERMIT_TRANSITIONS.get(str(from_status), {})


def highest_risk_level(levels: Any) -> str:
    """Return the highest risk level from a collection of level labels."""
    order = {level: index for index, level in enumerate(RISK_LEVELS)}
    best = ""
    best_index = -1
    for value in levels or ():
        text = str(value or "").strip()
        index = order.get(text, -1)
        if index > best_index:
            best = text
            best_index = index
    return best


V4_JOB_STATUS_TO_V5: dict[str, str] = {
    "草稿": PERMIT_DRAFT,
    "待EHS确认": PERMIT_EHS_REVIEW,
    "待审批": PERMIT_APPROVAL_PENDING,
    "已批准": PERMIT_APPROVED,
    "执行中": PERMIT_ACTIVE,
    "待复查": PERMIT_CLOSEOUT_REVIEW,
    "已关闭": PERMIT_CLOSED,
    "已驳回": PERMIT_RETURNED,
}

# V5-only statuses have no exact V4 equivalent; the mapping is intentionally
# lossy and documented so a legacy consumer sees a terminal-ish label instead of
# crashing on an unknown status.
V5_TO_V4_JOB_STATUS: dict[str, str] = {
    PERMIT_DRAFT: "草稿",
    PERMIT_EHS_REVIEW: "待EHS确认",
    PERMIT_APPROVAL_PENDING: "待审批",
    PERMIT_APPROVED: "已批准",
    PERMIT_ACTIVE: "执行中",
    PERMIT_SUSPENDED: "执行中",
    PERMIT_CLOSEOUT_REVIEW: "待复查",
    PERMIT_CLOSED: "已关闭",
    PERMIT_RETURNED: "已驳回",
    PERMIT_CANCELLED: "已驳回",
    PERMIT_EXPIRED: "已驳回",
}


__all__ = [
    "CODE_ILLEGAL_TRANSITION",
    "CODE_OK",
    "CODE_PERMISSION_DENIED",
    "CODE_PRECONDITION_FAILED",
    "CODE_REASON_REQUIRED",
    "CODE_UNKNOWN_STATUS",
    "HIGH_RISK_LEVELS",
    "PERMIT_ACTION_ROLES",
    "PERMIT_ACTIVE",
    "PERMIT_ACTIVE_STATUSES",
    "PERMIT_APPROVAL_PENDING",
    "PERMIT_APPROVED",
    "PERMIT_CANCELLED",
    "PERMIT_CLOSED",
    "PERMIT_CLOSEOUT_REVIEW",
    "PERMIT_DRAFT",
    "PERMIT_EHS_REVIEW",
    "PERMIT_EXPIRED",
    "PERMIT_REASON_ACTIONS",
    "PERMIT_RETURNED",
    "PERMIT_STATUSES",
    "PERMIT_SUSPENDED",
    "PERMIT_TERMINAL_STATUSES",
    "PERMIT_TRANSITIONS",
    "TransitionResult",
    "V4_JOB_STATUS_TO_V5",
    "V5_TO_V4_JOB_STATUS",
    "can_transition",
    "highest_risk_level",
    "validate_permit_transition",
]

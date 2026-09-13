"""Central V5 permission layer.

The state machine answers "can this status perform this action"; this module
answers "is this actor allowed to attempt the action".  The service layer uses
:func:`assert_can` and any UI uses :func:`can`, so hiding a button can never be
the only gate: both paths evaluate exactly the same rules.

Demo users are plain mappings (``{"id", "display_name", "role", "active"}``).
There is no password, SSO or real identity provider here.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import roles


PERMIT_CREATE = "permit:create"
PERMIT_EDIT = "permit:edit"
PERMIT_SUBMIT = "permit:submit"
PERMIT_EHS_REVIEW = "permit:ehs_review"
PERMIT_APPROVE = "permit:approve"
PERMIT_PRESTART_CONFIRM = "permit:prestart_confirm"
PERMIT_START = "permit:start"
PERMIT_SUSPEND = "permit:suspend"
PERMIT_RESUME = "permit:resume"
PERMIT_COMPLETE = "permit:complete"
PERMIT_CLOSE = "permit:close"
PERMIT_CANCEL = "permit:cancel"
PERMIT_EXPIRE = "permit:expire"

HAZARD_CREATE = "hazard:create"
HAZARD_ASSIGN = "hazard:assign"
HAZARD_START_RECTIFICATION = "hazard:start_rectification"
HAZARD_SUBMIT_RECTIFICATION = "hazard:submit_rectification"
HAZARD_VERIFY = "hazard:verify"
HAZARD_REOPEN = "hazard:reopen"

AUDIT_READ = "audit:read"

PERMISSIONS: tuple[str, ...] = (
    PERMIT_CREATE,
    PERMIT_EDIT,
    PERMIT_SUBMIT,
    PERMIT_EHS_REVIEW,
    PERMIT_APPROVE,
    PERMIT_PRESTART_CONFIRM,
    PERMIT_START,
    PERMIT_SUSPEND,
    PERMIT_RESUME,
    PERMIT_COMPLETE,
    PERMIT_CLOSE,
    PERMIT_CANCEL,
    PERMIT_EXPIRE,
    HAZARD_CREATE,
    HAZARD_ASSIGN,
    HAZARD_START_RECTIFICATION,
    HAZARD_SUBMIT_RECTIFICATION,
    HAZARD_VERIFY,
    HAZARD_REOPEN,
    AUDIT_READ,
)

PERMISSION_LABELS: dict[str, str] = {
    PERMIT_CREATE: "新建作业许可",
    PERMIT_EDIT: "编辑/指派作业许可",
    PERMIT_SUBMIT: "提交作业许可",
    PERMIT_EHS_REVIEW: "EHS 审核/退回",
    PERMIT_APPROVE: "审批作业许可",
    PERMIT_PRESTART_CONFIRM: "开工前确认",
    PERMIT_START: "开始作业",
    PERMIT_SUSPEND: "暂停作业",
    PERMIT_RESUME: "恢复作业",
    PERMIT_COMPLETE: "完成作业并交还现场",
    PERMIT_CLOSE: "关闭作业许可",
    PERMIT_CANCEL: "取消作业许可",
    PERMIT_EXPIRE: "标记作业许可过期",
    HAZARD_CREATE: "创建隐患",
    HAZARD_ASSIGN: "指派隐患整改/验证负责人",
    HAZARD_START_RECTIFICATION: "开始整改",
    HAZARD_SUBMIT_RECTIFICATION: "提交整改证据",
    HAZARD_VERIFY: "验证整改",
    HAZARD_REOPEN: "重新打开隐患",
    AUDIT_READ: "查看审计记录",
}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    roles.ROLE_APPLICANT: frozenset(
        {
            PERMIT_CREATE,
            PERMIT_EDIT,
            PERMIT_SUBMIT,
            PERMIT_PRESTART_CONFIRM,
            PERMIT_START,
            PERMIT_SUSPEND,
            PERMIT_RESUME,
            PERMIT_COMPLETE,
            PERMIT_CANCEL,
            HAZARD_CREATE,
            AUDIT_READ,
        }
    ),
    roles.ROLE_EHS_REVIEWER: frozenset(
        {
            PERMIT_EDIT,
            PERMIT_EHS_REVIEW,
            PERMIT_SUSPEND,
            PERMIT_RESUME,
            PERMIT_CLOSE,
            PERMIT_CANCEL,
            HAZARD_CREATE,
            HAZARD_ASSIGN,
            HAZARD_VERIFY,
            HAZARD_REOPEN,
            AUDIT_READ,
        }
    ),
    roles.ROLE_APPROVER: frozenset({PERMIT_APPROVE, AUDIT_READ}),
    roles.ROLE_ACTION_OWNER: frozenset(
        {
            PERMIT_PRESTART_CONFIRM,
            PERMIT_START,
            PERMIT_SUSPEND,
            PERMIT_RESUME,
            PERMIT_COMPLETE,
            HAZARD_CREATE,
            HAZARD_START_RECTIFICATION,
            HAZARD_SUBMIT_RECTIFICATION,
            AUDIT_READ,
        }
    ),
    roles.ROLE_DEMO_ADMIN: frozenset(PERMISSIONS),
    roles.ROLE_SYSTEM: frozenset(),
}

PERMIT_ACTION_PERMISSIONS: dict[str, str] = {
    "submit": PERMIT_SUBMIT,
    "resubmit": PERMIT_SUBMIT,
    "return_draft": PERMIT_EHS_REVIEW,
    "confirm": PERMIT_EHS_REVIEW,
    "approve": PERMIT_APPROVE,
    "reject": PERMIT_APPROVE,
    "prestart_confirm": PERMIT_PRESTART_CONFIRM,
    "suspend": PERMIT_SUSPEND,
    "resume": PERMIT_RESUME,
    "complete_work": PERMIT_COMPLETE,
    "close": PERMIT_CLOSE,
    "return_to_active": PERMIT_EHS_REVIEW,
    "cancel": PERMIT_CANCEL,
    "expire": PERMIT_EXPIRE,
}

HAZARD_ACTION_PERMISSIONS: dict[str, str] = {
    "assign": HAZARD_ASSIGN,
    "start": HAZARD_START_RECTIFICATION,
    "submit_rectification": HAZARD_SUBMIT_RECTIFICATION,
    "verify_pass": HAZARD_VERIFY,
    "verify_fail": HAZARD_VERIFY,
    "reopen": HAZARD_REOPEN,
}

# Actions whose entity rule is "only the assigned person may act".
_OWNER_BOUND_PERMISSIONS = frozenset(
    {HAZARD_START_RECTIFICATION, HAZARD_SUBMIT_RECTIFICATION}
)


class PermissionDenied(ValueError):
    """Raised when a user lacks a permission or fails an entity-level rule."""


def permission_for_permit_action(action: str) -> str:
    """Return the permission required by one permit transition action."""
    return PERMIT_ACTION_PERMISSIONS.get(str(action or "").strip(), "")


def permission_for_hazard_action(action: str) -> str:
    """Return the permission required by one hazard transition action."""
    return HAZARD_ACTION_PERMISSIONS.get(str(action or "").strip(), "")


def has_permission(role: str, permission: str) -> bool:
    """Return whether one role grants one permission."""
    return str(permission) in ROLE_PERMISSIONS.get(str(role), frozenset())


def allowed_roles(permission: str) -> tuple[str, ...]:
    """Return every role that grants one permission, in stable order."""
    value = str(permission or "").strip()
    return tuple(
        role for role in roles.ROLES if has_permission(role, value)
    )


def user_id(user: Mapping[str, Any] | None) -> str:
    """Return the demo user id from a user mapping."""
    if not isinstance(user, Mapping):
        return ""
    return str(user.get("id", "") or user.get("user_id", "")).strip()


def user_role(user: Mapping[str, Any] | None) -> str:
    """Return the demo role from a user mapping."""
    if not isinstance(user, Mapping):
        return ""
    return str(user.get("role", "")).strip()


def user_is_active(user: Mapping[str, Any] | None) -> bool:
    """Return whether a demo user is active (defaults to active)."""
    if not isinstance(user, Mapping):
        return False
    return bool(user.get("active", True))


def _entity_rule(
    user: Mapping[str, Any], permission: str, entity: Mapping[str, Any] | None
) -> str:
    if entity is None:
        return ""
    actor = user_id(user)
    if user_role(user) == roles.ROLE_DEMO_ADMIN:
        return ""

    if permission == PERMIT_APPROVE:
        applicant = str(entity.get("applicant_id", "") or "").strip()
        if applicant and applicant == actor:
            return "申请人不能审批自己的作业许可。"
        designated = str(entity.get("designated_approver_id", "") or "").strip()
        if designated and designated != actor:
            return f"该作业许可指定批准人为 {designated}，当前用户无权审批。"

    if permission == PERMIT_EHS_REVIEW:
        reviewer = str(entity.get("ehs_reviewer_id", "") or "").strip()
        if reviewer and reviewer != actor:
            return f"该作业许可指定 EHS 审核人为 {reviewer}，当前用户无权审核。"

    if permission in _OWNER_BOUND_PERMISSIONS:
        owner = str(entity.get("owner_id", "") or "").strip()
        if not owner:
            return "该隐患尚未指派整改负责人，当前用户无权执行整改动作。"
        if owner != actor:
            return "只有该隐患的整改负责人可以执行此整改动作。"

    if permission == HAZARD_VERIFY:
        owner = str(entity.get("owner_id", "") or "").strip()
        if owner and owner == actor:
            return "整改负责人不能验证自己提交的整改。"
        verifier = str(entity.get("verifier_id", "") or "").strip()
        if verifier and verifier != actor:
            return f"该隐患指定验证人为 {verifier}，当前用户无权验证。"

    return ""


def _evaluate(
    user: Mapping[str, Any] | None,
    permission: str,
    entity: Mapping[str, Any] | None = None,
) -> str:
    if not isinstance(user, Mapping):
        return "缺少 Demo 用户身份，无法校验权限。"
    if not user_is_active(user):
        return "该 Demo 用户已停用，无法执行操作。"
    value = str(permission or "").strip()
    if value not in PERMISSIONS:
        return f"未知权限：{permission!r}。"
    role = user_role(user)
    if not has_permission(role, value):
        return f"角色 {role or '（未提供）'} 缺少权限 {value}。"
    return _entity_rule(user, value, entity)


def can(
    user: Mapping[str, Any] | None,
    permission: str,
    entity: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether the user may attempt the action (same rule as assert_can)."""
    return _evaluate(user, permission, entity) == ""


def explain(
    user: Mapping[str, Any] | None,
    permission: str,
    entity: Mapping[str, Any] | None = None,
) -> str:
    """Return the denial reason, or an empty string when allowed."""
    return _evaluate(user, permission, entity)


def assert_can(
    user: Mapping[str, Any] | None,
    permission: str,
    entity: Mapping[str, Any] | None = None,
) -> None:
    """Raise :class:`PermissionDenied` unless the user may attempt the action."""
    reason = _evaluate(user, permission, entity)
    if reason:
        raise PermissionDenied(reason)


__all__ = [
    "AUDIT_READ",
    "HAZARD_ACTION_PERMISSIONS",
    "HAZARD_ASSIGN",
    "HAZARD_CREATE",
    "HAZARD_REOPEN",
    "HAZARD_START_RECTIFICATION",
    "HAZARD_SUBMIT_RECTIFICATION",
    "HAZARD_VERIFY",
    "PERMISSIONS",
    "PERMISSION_LABELS",
    "PERMIT_ACTION_PERMISSIONS",
    "PERMIT_APPROVE",
    "PERMIT_CANCEL",
    "PERMIT_CLOSE",
    "PERMIT_COMPLETE",
    "PERMIT_CREATE",
    "PERMIT_EDIT",
    "PERMIT_EHS_REVIEW",
    "PERMIT_EXPIRE",
    "PERMIT_PRESTART_CONFIRM",
    "PERMIT_RESUME",
    "PERMIT_START",
    "PERMIT_SUBMIT",
    "PERMIT_SUSPEND",
    "PermissionDenied",
    "ROLE_PERMISSIONS",
    "allowed_roles",
    "assert_can",
    "can",
    "explain",
    "has_permission",
    "permission_for_hazard_action",
    "permission_for_permit_action",
    "user_id",
    "user_is_active",
    "user_role",
]

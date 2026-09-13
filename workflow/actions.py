"""Business action catalog for the V5 UI.

The state machine owns "which status allows which action"; this module adds the
business label and the permission check, so a page can ask one function for the
exact buttons one persona may press.  Service commands still re-validate both
layers, so hiding a button is never the security boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import hazard_state, permit_state, permissions


KIND_PRIMARY = "primary"
KIND_SECONDARY = "secondary"
KIND_DANGER = "danger"

_KIND_ORDER = {KIND_PRIMARY: 0, KIND_SECONDARY: 1, KIND_DANGER: 2}


@dataclass(frozen=True)
class Action:
    """One business action offered to the current persona."""

    code: str
    label: str
    kind: str
    permission: str
    requires_reason: bool = False

    @property
    def is_primary(self) -> bool:
        return self.kind == KIND_PRIMARY


@dataclass(frozen=True)
class _Spec:
    label: str
    kind: str


PERMIT_ACTION_SPECS: dict[str, _Spec] = {
    "submit": _Spec("提交EHS审核", KIND_PRIMARY),
    "return_draft": _Spec("退回申请人", KIND_SECONDARY),
    "confirm": _Spec("确认并提交审批", KIND_PRIMARY),
    "approve": _Spec("批准作业", KIND_PRIMARY),
    "reject": _Spec("驳回作业", KIND_DANGER),
    "resubmit": _Spec("重新提交审核", KIND_PRIMARY),
    "prestart_confirm": _Spec("完成开工检查并开始作业", KIND_PRIMARY),
    "suspend": _Spec("暂停作业", KIND_SECONDARY),
    "resume": _Spec("恢复作业", KIND_PRIMARY),
    "complete_work": _Spec("作业完成并交还现场", KIND_PRIMARY),
    "return_to_active": _Spec("退回继续执行", KIND_SECONDARY),
    "close": _Spec("关闭作业许可", KIND_PRIMARY),
    "cancel": _Spec("取消作业许可", KIND_DANGER),
}

HAZARD_ACTION_SPECS: dict[str, _Spec] = {
    "assign": _Spec("指派整改负责人", KIND_PRIMARY),
    "start": _Spec("开始整改", KIND_PRIMARY),
    "submit_rectification": _Spec("提交整改证据", KIND_PRIMARY),
    "verify_pass": _Spec("验证通过", KIND_PRIMARY),
    "verify_fail": _Spec("退回整改", KIND_DANGER),
    "reopen": _Spec("重新打开隐患", KIND_DANGER),
}


def _rank(action: Action) -> tuple[int, int]:
    return (_KIND_ORDER.get(action.kind, 9), 0)


def get_available_actions(
    entity_type: str,
    record: Mapping[str, Any] | None,
    user: Mapping[str, Any] | None,
) -> list[Action]:
    """Return the actions the persona may attempt on this entity, ranked."""
    if not record:
        return []
    status = str(record.get("status", "")).strip()
    kind = str(entity_type or "").strip()
    if kind == "permit":
        transitions = permit_state.PERMIT_TRANSITIONS.get(status, {})
        specs = PERMIT_ACTION_SPECS
        reason_actions = permit_state.PERMIT_REASON_ACTIONS
        permission_for = permissions.permission_for_permit_action
    elif kind == "hazard":
        transitions = hazard_state.HAZARD_TRANSITIONS.get(status, {})
        specs = HAZARD_ACTION_SPECS
        reason_actions = hazard_state.HAZARD_REASON_ACTIONS
        permission_for = permissions.permission_for_hazard_action
    else:
        raise ValueError(f"未知实体类型：{entity_type!r}。")

    actions: list[Action] = []
    for code in transitions:
        spec = specs.get(code)
        if spec is None:
            continue
        permission = permission_for(code)
        if not permissions.can(user, permission, record):
            continue
        actions.append(
            Action(
                code=code,
                label=spec.label,
                kind=spec.kind,
                permission=permission,
                requires_reason=code in reason_actions,
            )
        )
    return sorted(actions, key=_rank)


def primary_action(actions: list[Action]) -> Action | None:
    """Return the most prominent action, if any."""
    for action in actions:
        if action.is_primary:
            return action
    return actions[0] if actions else None


def secondary_actions(actions: list[Action], limit: int = 2) -> list[Action]:
    """Return up to ``limit`` non-primary actions."""
    primary = primary_action(actions)
    rest = [action for action in actions if action is not primary]
    return rest[:limit]


__all__ = [
    "Action",
    "HAZARD_ACTION_SPECS",
    "KIND_DANGER",
    "KIND_PRIMARY",
    "KIND_SECONDARY",
    "PERMIT_ACTION_SPECS",
    "get_available_actions",
    "primary_action",
    "secondary_actions",
]

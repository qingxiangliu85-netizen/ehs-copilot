"""Forward one pressed business button to the matching service command.

This module is a thin adapter and nothing more: it maps a business action code
(produced by ``workflow.actions``) onto the existing ``services.*`` command and
returns ``(ok, message)`` instead of raising.  The service still validates the
permission and the state machine, so a stale page can never force a transition.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from services import hazard_service, permit_service
from workflow import permissions


def _failure(exc: BaseException) -> tuple[bool, str]:
    if isinstance(exc, permissions.PermissionDenied):
        return False, f"该操作被权限规则拒绝：{exc}"
    if isinstance(exc, KeyError):
        return False, f"记录不存在或已被删除：{exc}"
    if isinstance(exc, ValueError):
        return False, str(exc)
    return False, f"{type(exc).__name__}: {exc}"


def execute_permit(
    connection: sqlite3.Connection,
    permit_id: str,
    code: str,
    *,
    user: Mapping[str, Any],
    reason: str = "",
    checks: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[bool, str]:
    """Run one permit action and return ``(ok, message)``."""
    text = str(reason or "").strip()
    try:
        if code == "submit":
            permit_service.submit_permit(connection, permit_id, user=user)
        elif code == "resubmit":
            permit_service.transition_permit(
                connection, permit_id, "resubmit", user=user
            )
        elif code == "return_draft":
            permit_service.complete_ehs_review(
                connection, permit_id, decision="return_draft", reason=text, user=user
            )
        elif code == "confirm":
            permit_service.complete_ehs_review(
                connection, permit_id, decision="confirm", reason=text, user=user
            )
        elif code == "approve":
            permit_service.decide_approval(
                connection, permit_id, decision="approve", comment=text, user=user
            )
        elif code == "reject":
            permit_service.decide_approval(
                connection, permit_id, decision="reject", comment=text, user=user
            )
        elif code == "prestart_confirm":
            permit_service.confirm_prestart(
                connection, permit_id, list(checks or ()), user=user
            )
        elif code == "suspend":
            permit_service.suspend_permit(
                connection, permit_id, reason=text, user=user
            )
        elif code == "resume":
            permit_service.resume_permit(connection, permit_id, user=user)
        elif code == "complete_work":
            permit_service.complete_work(
                connection, permit_id, handback_note=text, user=user
            )
        elif code == "return_to_active":
            permit_service.transition_permit(
                connection, permit_id, "return_to_active", reason=text, user=user
            )
        elif code == "close":
            permit_service.close_permit(connection, permit_id, note=text, user=user)
        elif code == "cancel":
            permit_service.cancel_permit(
                connection, permit_id, reason=text, user=user
            )
        else:
            return False, f"未支持的操作：{code}。"
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        return _failure(exc)
    return True, "操作已完成。"


def execute_hazard(
    connection: sqlite3.Connection,
    hazard_id: str,
    code: str,
    *,
    user: Mapping[str, Any],
    reason: str = "",
    owner_id: str = "",
    due_at: str = "",
    evidence: Iterable[Mapping[str, Any]] = (),
) -> tuple[bool, str]:
    """Run one hazard action and return ``(ok, message)``."""
    text = str(reason or "").strip()
    try:
        if code == "assign":
            hazard_service.assign_hazard(
                connection,
                hazard_id,
                owner_id=owner_id,
                due_at=str(due_at or "").strip(),
                user=user,
            )
        elif code == "start":
            hazard_service.start_rectification(connection, hazard_id, user=user)
        elif code == "submit_rectification":
            hazard_service.submit_rectification(
                connection,
                hazard_id,
                evidence=list(evidence),
                notes=text,
                user=user,
            )
        elif code == "verify_pass":
            hazard_service.verify_hazard(
                connection, hazard_id, result="pass", notes=text, user=user
            )
        elif code == "verify_fail":
            hazard_service.verify_hazard(
                connection, hazard_id, result="fail", notes=text, user=user
            )
        elif code == "reopen":
            hazard_service.reopen_hazard(
                connection, hazard_id, reason=text, user=user
            )
        else:
            return False, f"未支持的操作：{code}。"
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        return _failure(exc)
    return True, "操作已完成。"


def execute(
    connection: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    code: str,
    *,
    user: Mapping[str, Any],
    reason: str = "",
    checks: Sequence[Mapping[str, Any]] | None = None,
    owner_id: str = "",
    due_at: str = "",
    evidence: Iterable[Mapping[str, Any]] = (),
) -> tuple[bool, str]:
    """Run one action for either entity type."""
    kind = str(entity_type or "").strip()
    if kind == "permit":
        return execute_permit(
            connection, entity_id, code, user=user, reason=reason, checks=checks
        )
    if kind == "hazard":
        return execute_hazard(
            connection,
            entity_id,
            code,
            user=user,
            reason=reason,
            owner_id=owner_id,
            due_at=due_at,
            evidence=evidence,
        )
    return False, f"未知实体类型：{entity_type!r}。"


__all__ = ["execute", "execute_hazard", "execute_permit"]

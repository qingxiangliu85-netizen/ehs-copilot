"""Human-in-the-loop approval primitives for the EHS Copilot workflow.

The workflow never performs a write on its own authority.  When
:mod:`workflow.guardrails` decides that an operation needs a human, the graph
pauses with an :class:`ApprovalRequest` and only continues once an
:class:`ApprovalDecision` comes back from the operator.

Three actions are supported:

``approve``
    继续执行原参数。
``modify``
    用修改后的参数继续；参数必须仍通过校验（见 ``workflow.graph``）。
``reject``
    停止本次流程，任何写操作都不会执行。

The objects here are deliberately plain dataclasses with ``to_payload`` /
``from_payload`` helpers so they can travel through LangGraph's ``interrupt()``
(resume values must be JSON-serialisable) and be rendered by Streamlit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #

ACTION_APPROVE = "approve"
ACTION_MODIFY = "modify"
ACTION_REJECT = "reject"

ACTIONS: tuple[str, ...] = (ACTION_APPROVE, ACTION_MODIFY, ACTION_REJECT)

ACTION_LABELS: dict[str, str] = {
    ACTION_APPROVE: "已批准",
    ACTION_MODIFY: "已修改参数后批准",
    ACTION_REJECT: "已拒绝",
}

ACTION_ICONS: dict[str, str] = {
    ACTION_APPROVE: "✅",
    ACTION_MODIFY: "✏️",
    ACTION_REJECT: "⛔",
}

AUTO_ACTION = "auto"
AUTO_ACTION_LABEL = "已自动放行（非交互式调用）"

# The UI may hand back a fresh decision only this many times for the same gate;
# beyond it the operation is treated as rejected.  Guards against a caller
# looping a modify decision forever.
MAX_APPROVAL_ROUNDS = 3


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #

OP_CREATE_HAZARD = "create_hazard"
OP_UPDATE_HAZARD = "update_hazard"
OP_CLOSE_HAZARD = "close_hazard"
OP_MAJOR_RISK_WRITE = "major_risk_write"
OP_RISK_DOWNGRADE = "risk_downgrade"
OP_SDS_INSUFFICIENT = "sds_insufficient_evidence"
OP_SDS_CONFLICT = "sds_source_conflict"
OP_APPROVE_JOB = "approve_job"

OPERATION_LABELS: dict[str, str] = {
    OP_CREATE_HAZARD: "创建隐患",
    OP_UPDATE_HAZARD: "修改隐患",
    OP_CLOSE_HAZARD: "关闭隐患",
    OP_MAJOR_RISK_WRITE: "重大风险写操作",
    OP_RISK_DOWNGRADE: "AI 建议降低风险等级",
    OP_SDS_INSUFFICIENT: "SDS 证据不足后继续",
    OP_SDS_CONFLICT: "SDS 来源冲突后继续",
    OP_APPROVE_JOB: "批准作业单",
}

# When one plan carries several risky calls, the aggregated request is labelled
# by the most severe operation it contains.
OPERATION_PRIORITY: tuple[str, ...] = (
    OP_MAJOR_RISK_WRITE,
    OP_RISK_DOWNGRADE,
    OP_CLOSE_HAZARD,
    OP_CREATE_HAZARD,
    OP_UPDATE_HAZARD,
    OP_SDS_CONFLICT,
    OP_SDS_INSUFFICIENT,
)


def operation_label(operation: str) -> str:
    """Return the human-readable label for an operation constant."""
    return OPERATION_LABELS.get(str(operation), str(operation))


def primary_operation(operations: object) -> str:
    """Pick the most severe operation from a collection of operation codes."""
    values = [str(item) for item in (operations or ()) if item]
    for candidate in OPERATION_PRIORITY:
        if candidate in values:
            return candidate
    return values[0] if values else ""


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item))
    return ()


def _as_int(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ApprovalRequest:
    """One human decision the workflow is waiting for."""

    gate_id: str
    operation: str
    reason: str = ""
    summary: str = ""
    calls: tuple[dict[str, Any], ...] = ()
    current: dict[str, Any] = field(default_factory=dict)
    proposed: dict[str, Any] = field(default_factory=dict)
    guard_codes: tuple[str, ...] = ()
    round: int = 1
    kind: str = "plan"

    @property
    def operation_label(self) -> str:
        return operation_label(self.operation)

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(str(item.get("tool", "")) for item in self.calls)

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-serialisable form passed to ``interrupt()``."""
        return {
            "gate_id": self.gate_id,
            "kind": self.kind,
            "operation": self.operation,
            "operation_label": self.operation_label,
            "reason": self.reason,
            "summary": self.summary,
            "calls": [dict(item) for item in self.calls],
            "current": dict(self.current),
            "proposed": dict(self.proposed),
            "guard_codes": list(self.guard_codes),
            "round": self.round,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> ApprovalRequest | None:
        """Rebuild a request from its payload, or return ``None`` when invalid."""
        data = _as_dict(payload)
        gate_id = str(data.get("gate_id", ""))
        if not gate_id:
            return None
        calls = tuple(
            _as_dict(item) for item in (data.get("calls") or ()) if isinstance(item, dict)
        )
        return cls(
            gate_id=gate_id,
            operation=str(data.get("operation", "")),
            reason=str(data.get("reason", "")),
            summary=str(data.get("summary", "")),
            calls=calls,
            current=_as_dict(data.get("current")),
            proposed=_as_dict(data.get("proposed")),
            guard_codes=_as_tuple(data.get("guard_codes")),
            round=_as_int(data.get("round"), 1),
            kind=str(data.get("kind", "plan")),
        )


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ApprovalDecision:
    """The operator's answer to an :class:`ApprovalRequest`."""

    gate_id: str
    action: str = ACTION_APPROVE
    calls: tuple[dict[str, Any], ...] = ()
    note: str = ""
    round: int = 1

    @property
    def is_approve(self) -> bool:
        return self.action == ACTION_APPROVE

    @property
    def is_reject(self) -> bool:
        return self.action == ACTION_REJECT

    @property
    def is_modify(self) -> bool:
        return self.action == ACTION_MODIFY

    @property
    def action_label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-serialisable form accepted by ``resume_workflow``."""
        return {
            "gate_id": self.gate_id,
            "action": self.action,
            "calls": [dict(item) for item in self.calls],
            "note": self.note,
            "round": self.round,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> ApprovalDecision | None:
        """Rebuild a decision, or return ``None`` when the payload is invalid."""
        data = _as_dict(payload)
        action = str(data.get("action", ""))
        if action not in ACTIONS:
            return None
        return cls(
            gate_id=str(data.get("gate_id", "")),
            action=action,
            calls=tuple(
                _as_dict(item)
                for item in (data.get("calls") or ())
                if isinstance(item, dict)
            ),
            note=str(data.get("note", "")),
            round=_as_int(data.get("round"), 1),
        )


def auto_decision(gate_id: str, note: str = "") -> ApprovalDecision:
    """Return the decision recorded when the caller runs without a human."""
    return ApprovalDecision(gate_id=gate_id, action=ACTION_APPROVE, note=note)


def approval_record(
    request: ApprovalRequest,
    decision: ApprovalDecision,
    *,
    phase: str = "plan",
    auto: bool = False,
) -> dict[str, Any]:
    """Build the state record that documents one approval decision.

    ``phase`` tells the timeline whether the gate happened before tool
    execution (``plan``) or after the SDS retrieval (``evidence``).
    """
    action = AUTO_ACTION if auto else decision.action
    return {
        "gate_id": request.gate_id,
        "phase": phase,
        "operation": request.operation,
        "operation_label": request.operation_label,
        "action": action,
        "action_label": AUTO_ACTION_LABEL if auto else decision.action_label,
        "auto": bool(auto),
        "note": str(decision.note or ""),
        "round": int(decision.round or 1),
        "calls": [dict(item) for item in request.calls],
        "guard_codes": list(request.guard_codes),
    }


__all__ = [
    "ACTION_APPROVE",
    "ACTION_ICONS",
    "ACTION_LABELS",
    "ACTION_MODIFY",
    "ACTION_REJECT",
    "ACTIONS",
    "AUTO_ACTION",
    "AUTO_ACTION_LABEL",
    "ApprovalDecision",
    "ApprovalRequest",
    "MAX_APPROVAL_ROUNDS",
    "OPERATION_LABELS",
    "OPERATION_PRIORITY",
    "OP_APPROVE_JOB",
    "OP_CLOSE_HAZARD",
    "OP_CREATE_HAZARD",
    "OP_MAJOR_RISK_WRITE",
    "OP_RISK_DOWNGRADE",
    "OP_SDS_CONFLICT",
    "OP_SDS_INSUFFICIENT",
    "OP_UPDATE_HAZARD",
    "approval_record",
    "auto_decision",
    "operation_label",
    "primary_operation",
]

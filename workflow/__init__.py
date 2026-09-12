"""EHS Copilot V3 workflow layer.

Public surface:

* :mod:`workflow.router`      — deterministic natural-language → task/tool routing
* :mod:`workflow.state`       — the shared graph state and result value objects
* :mod:`workflow.hitl`        — human-in-the-loop approval requests and decisions
* :mod:`workflow.guardrails`  — pure guardrail checks (evidence, risk, chemicals)
* :mod:`workflow.timeline`    — derived execution timeline for the UI
* :mod:`workflow.graph`       — the LangGraph orchestration
* :mod:`workflow.summary`     — deterministic answer composition
* :mod:`workflow.ui`          — the Streamlit "AI 工作流助手" page
"""

from __future__ import annotations

from .router import RouteDecision, ToolCallPlan, classify, route
from .state import (
    STATUS_AWAITING_APPROVAL,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_REJECTED,
    TASK_DASHBOARD_SUMMARY,
    TASK_HAZARD_MANAGEMENT,
    TASK_JSA_RISK,
    TASK_LABELS,
    TASK_SDS_QUERY,
    WorkflowResult,
    WorkflowState,
    task_label,
)

__all__ = [
    "RouteDecision",
    "STATUS_AWAITING_APPROVAL",
    "STATUS_BLOCKED",
    "STATUS_COMPLETED",
    "STATUS_REJECTED",
    "TASK_DASHBOARD_SUMMARY",
    "TASK_HAZARD_MANAGEMENT",
    "TASK_JSA_RISK",
    "TASK_LABELS",
    "TASK_SDS_QUERY",
    "ToolCallPlan",
    "WorkflowResult",
    "WorkflowState",
    "classify",
    "route",
    "task_label",
]

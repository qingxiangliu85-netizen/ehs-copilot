"""State schema and shared value objects for the EHS Copilot workflow graph.

The graph is intentionally thin: every node reads from and writes to this one
``WorkflowState``, which keeps the routing decision, the execution plan, the
guardrail findings, the human approvals, the step trace and the tool results in
a single, inspectable place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages

from tools import TOOL_SPECS


# --------------------------------------------------------------------------- #
# Task types
# --------------------------------------------------------------------------- #

TASK_SDS_QUERY = "sds_query"
TASK_JSA_RISK = "jsa_risk"
TASK_HAZARD_MANAGEMENT = "hazard_management"
TASK_DASHBOARD_SUMMARY = "dashboard_summary"

TASK_ORDER: tuple[str, ...] = (
    TASK_SDS_QUERY,
    TASK_JSA_RISK,
    TASK_HAZARD_MANAGEMENT,
    TASK_DASHBOARD_SUMMARY,
)

TASK_LABELS: dict[str, str] = {
    TASK_SDS_QUERY: "SDS 查询",
    TASK_JSA_RISK: "JSA 风险评估",
    TASK_HAZARD_MANAGEMENT: "隐患管理",
    TASK_DASHBOARD_SUMMARY: "仪表盘汇总",
}

COMBINED_TASK_LABEL = "组合任务"

TOOL_LABELS: dict[str, str] = {
    name: spec.label for name, spec in TOOL_SPECS.items()
}

# Step statuses used by the UI trace.
STEP_OK = "ok"
STEP_WARN = "warn"
STEP_INFO = "info"

# Run statuses.
STATUS_COMPLETED = "completed"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_REJECTED = "rejected"
STATUS_BLOCKED = "blocked"


def task_label(task_types: tuple[str, ...] | list[str]) -> str:
    """Return the human-readable label for a routing decision."""
    labels = [TASK_LABELS.get(item, item) for item in task_types]
    if not labels:
        return "未能识别（未匹配到已知任务）"
    if len(labels) == 1:
        return labels[0]
    return f"{COMBINED_TASK_LABEL}（" + " + ".join(labels) + "）"


# --------------------------------------------------------------------------- #
# Reducers
# --------------------------------------------------------------------------- #


def extend_list(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    """Reducer that appends node output instead of overwriting it."""
    return list(left or []) + list(right or [])


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


class WorkflowState(TypedDict, total=False):
    """Shared state threaded through every node of the workflow graph."""

    user_input: str
    thread_id: str
    auto_approve: bool
    status: str
    route_label: str
    task_types: list[str]
    matched_keywords: dict[str, list[str]]
    parameters: dict[str, Any]
    plan: list[dict[str, Any]]
    messages: Annotated[list[Any], add_messages]
    steps: Annotated[list[dict[str, Any]], extend_list]
    tool_results: Annotated[list[dict[str, Any]], extend_list]
    errors: Annotated[list[str], extend_list]
    guardrails: Annotated[list[dict[str, Any]], extend_list]
    approvals: Annotated[list[dict[str, Any]], extend_list]
    tool_overrides: dict[str, dict[str, Any]]
    final_answer: str


def new_state(
    user_input: str,
    *,
    auto_approve: bool = True,
    thread_id: str = "",
) -> WorkflowState:
    """Return the initial state for one workflow run.

    ``auto_approve`` keeps the first-phase programmatic behaviour (no human in
    the loop) and is what the non-interactive tests rely on.  The Streamlit page
    always passes ``auto_approve=False`` so that a real operator must confirm
    every gated operation.
    """
    return WorkflowState(
        user_input=str(user_input or ""),
        thread_id=str(thread_id or ""),
        auto_approve=bool(auto_approve),
        status="",
        route_label="",
        task_types=[],
        matched_keywords={},
        parameters={},
        plan=[],
        messages=[],
        steps=[],
        tool_results=[],
        errors=[],
        guardrails=[],
        approvals=[],
        tool_overrides={},
        final_answer="",
    )


def make_step(
    order: int,
    node: str,
    title: str,
    detail: str = "",
    status: str = STEP_INFO,
) -> dict[str, Any]:
    """Build one entry of the execution trace shown in the UI."""
    return {
        "order": order,
        "node": node,
        "title": title,
        "detail": detail,
        "status": status,
    }


# --------------------------------------------------------------------------- #
# Result object returned to the UI / tests
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorkflowResult:
    """Immutable snapshot of one workflow run (completed or suspended)."""

    user_input: str
    route_label: str
    task_types: tuple[str, ...] = ()
    matched_keywords: dict[str, list[str]] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    plan: tuple[dict[str, Any], ...] = ()
    steps: tuple[dict[str, Any], ...] = ()
    tool_results: tuple[dict[str, Any], ...] = ()
    final_answer: str = ""
    errors: tuple[str, ...] = ()
    engine: str = "langgraph"
    elapsed_ms: int = 0
    thread_id: str = ""
    status: str = STATUS_COMPLETED
    pending_approval: dict[str, Any] | None = None
    approvals: tuple[dict[str, Any], ...] = ()
    guardrails: tuple[dict[str, Any], ...] = ()
    timeline: tuple[dict[str, Any], ...] = ()
    citations_ok: bool = True

    @property
    def awaiting_approval(self) -> bool:
        """Return whether the run is paused for a human decision."""
        return self.pending_approval is not None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view (used by the session-state history)."""
        return {
            "user_input": self.user_input,
            "route_label": self.route_label,
            "task_types": list(self.task_types),
            "matched_keywords": self.matched_keywords,
            "parameters": self.parameters,
            "plan": list(self.plan),
            "steps": list(self.steps),
            "tool_results": list(self.tool_results),
            "final_answer": self.final_answer,
            "errors": list(self.errors),
            "engine": self.engine,
            "elapsed_ms": self.elapsed_ms,
            "thread_id": self.thread_id,
            "status": self.status,
            "pending_approval": self.pending_approval,
            "approvals": list(self.approvals),
            "guardrails": list(self.guardrails),
            "timeline": list(self.timeline),
            "citations_ok": self.citations_ok,
        }

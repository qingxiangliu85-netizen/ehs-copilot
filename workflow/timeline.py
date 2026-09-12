"""Execution timeline for the workflow assistant.

The timeline is *derived* from the finished (or suspended) workflow state rather
than accumulated by side effects.  That keeps it deterministic: rebuilding it
from the same state always produces the same events, which is what the unit
tests assert.

Required events, in the order an operator expects to see them:

已识别任务 → 已生成计划 → 等待审批/已批准/已拒绝 → 已调用工具 → 已获取证据 →
当前风险等级 → 已生成 JSA → 已执行写操作 → 已完成

When the run is suspended on a human gate, the "等待审批" event is always the
last one: nothing after the pause has happened yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from . import hitl
from .state import TOOL_LABELS

# --------------------------------------------------------------------------- #
# Event statuses
# --------------------------------------------------------------------------- #

STATUS_DONE = "done"
STATUS_PENDING = "pending"
STATUS_REJECTED = "rejected"
STATUS_BLOCKED = "blocked"
STATUS_WARNING = "warning"
STATUS_INFO = "info"

STATUS_ICONS: dict[str, str] = {
    STATUS_DONE: "✅",
    STATUS_PENDING: "⏳",
    STATUS_REJECTED: "⛔",
    STATUS_BLOCKED: "🚫",
    STATUS_WARNING: "⚠️",
    STATUS_INFO: "ℹ️",
}

# Event keys
KEY_TASK = "task_recognized"
KEY_PLAN = "plan_created"
KEY_TOOL = "tool_called"
KEY_EVIDENCE = "evidence_collected"
KEY_JSA = "jsa_drafted"
KEY_RISK = "risk_level"
KEY_APPROVAL_PENDING = "approval_pending"
KEY_APPROVAL_DONE = "approval_decided"
KEY_WRITE = "write_executed"
KEY_COMPLETED = "completed"
KEY_GUARDRAIL = "guardrail"

TIMELINE_LABELS: dict[str, str] = {
    KEY_TASK: "已识别任务",
    KEY_PLAN: "已生成计划",
    KEY_TOOL: "已调用工具",
    KEY_EVIDENCE: "已获取证据",
    KEY_JSA: "已生成 JSA",
    KEY_RISK: "当前风险等级",
    KEY_APPROVAL_PENDING: "等待审批",
    KEY_APPROVAL_DONE: "已批准/已拒绝",
    KEY_WRITE: "已执行写操作",
    KEY_COMPLETED: "已完成",
    KEY_GUARDRAIL: "安全规则",
}

_WRITE_RESULT_TOOLS = frozenset({"create_hazard", "update_hazard"})

_SEVERITY_STATUS = {
    "block": STATUS_BLOCKED,
    # A real pause is reported by its own "等待审批" event, so an
    # approval-severity finding is rendered as a warning here.
    "approval": STATUS_WARNING,
    "notice": STATUS_WARNING,
}


# --------------------------------------------------------------------------- #
# Value object
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TimelineEvent:
    """One entry of the execution timeline rendered by the workflow page."""

    order: int
    key: str
    label: str
    detail: str
    status: str
    phase: str = ""

    @property
    def icon(self) -> str:
        return STATUS_ICONS.get(self.status, "·")

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "key": self.key,
            "label": self.label,
            "detail": self.detail,
            "status": self.status,
            "phase": self.phase,
            "icon": self.icon,
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _numbered(events: Iterable[Mapping[str, Any]]) -> tuple[TimelineEvent, ...]:
    return tuple(
        TimelineEvent(order=index, **item)  # type: ignore[arg-type]
        for index, item in enumerate(events, start=1)
    )


def _tool_label(tool: str) -> str:
    return TOOL_LABELS.get(tool, tool)


def _short(text: Any, limit: int = 120) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit].rstrip()}…"


def _approval_detail(record: Mapping[str, Any]) -> str:
    operation = str(record.get("operation_label") or "")
    action = str(record.get("action_label") or "")
    note = str(record.get("note") or "")
    round_no = record.get("round") or 1
    parts = [f"{operation}：{action}"]
    if round_no and int(round_no) > 1:
        parts.append(f"第 {round_no} 轮")
    if note:
        parts.append(_short(note, 80))
    return "｜".join(item for item in parts if item)


def _pending_detail(pending: Mapping[str, Any]) -> str:
    operation = str(
        pending.get("operation_label")
        or hitl.operation_label(str(pending.get("operation", "")))
    )
    reason = str(pending.get("reason") or pending.get("summary") or "")
    codes = "、".join(str(item) for item in (pending.get("guard_codes") or ()))
    parts = [operation or "待确认操作", _short(reason, 140)]
    if codes:
        parts.append(f"命中规则：{codes}")
    return "｜".join(item for item in parts if item)


def _evidence_detail(payloads: Iterable[Mapping[str, Any]]) -> str:
    rows: list[str] = []
    for payload in payloads:
        for row in payload.get("evidence") or ():
            source = str(row.get("source", "未知文件"))
            page = row.get("page", "?")
            rows.append(f"{source} 第 {page} 页")
    unique = list(dict.fromkeys(rows))
    return "；".join(unique[:6]) if unique else "未返回证据片段。"


def _risk_detail(payloads: Iterable[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for payload in payloads:
        tool = str(payload.get("tool", ""))
        score = payload.get("risk_score")
        level = payload.get("risk_level")
        if score is None:
            record = dict(payload.get("record") or {})
            score = record.get("风险值R")
            level = record.get("风险等级")
        if score is None:
            continue
        formula = str(payload.get("formula") or "")
        label = "当前风险等级" if tool == "calculate_risk" else "JSA 初始风险"
        parts.append(f"{label} {level}（R={score}{'；' + formula if formula else ''}）")
        residual = payload.get("residual_risk_score")
        if residual is not None:
            parts.append(
                f"残余风险 {payload.get('residual_risk_level')}（R={residual}）"
            )
    return "；".join(parts) if parts else "本次未计算风险值。"


def _jsa_detail(payloads: Iterable[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for payload in payloads:
        parts.append(
            f"《{payload.get('job_name', '')}》"
            f"{payload.get('risk_score', '')}·{payload.get('risk_level', '')}"
        )
    return "；".join(parts)


def _write_detail(payloads: Iterable[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for payload in payloads:
        tool = str(payload.get("tool", ""))
        if tool == "create_hazard":
            parts.append(
                f"新增隐患 {payload.get('hazard_id', '')}"
                f"（{payload.get('risk_level', '')}·{payload.get('state', '')}）"
            )
        elif tool == "update_hazard":
            changed = "、".join(payload.get("changed_fields") or []) or "无"
            parts.append(
                f"更新隐患 {payload.get('hazard_id', '')}"
                f"（变更 {changed} → {payload.get('state', '')}）"
            )
    return "；".join(parts)


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #


def build_timeline(
    state: Mapping[str, Any],
    *,
    pending_approval: Mapping[str, Any] | None = None,
    tool_results: Iterable[Mapping[str, Any]] | None = None,
) -> tuple[TimelineEvent, ...]:
    """Derive the execution timeline from a workflow state snapshot."""
    if tool_results is None:
        results = [dict(item) for item in (state.get("tool_results") or ())]
    else:
        results = [dict(item) for item in tool_results]

    approvals = [dict(item) for item in (state.get("approvals") or ())]
    violations = [dict(item) for item in (state.get("guardrails") or ())]
    plan = [dict(item) for item in (state.get("plan") or ())]
    status = str(state.get("status", "") or "")
    pending = dict(pending_approval) if isinstance(pending_approval, Mapping) else {}

    events: list[dict[str, Any]] = []

    def push(key: str, detail: str, status_value: str, phase: str = "") -> None:
        events.append(
            {
                "key": key,
                "label": TIMELINE_LABELS.get(key, key),
                "detail": detail,
                "status": status_value,
                "phase": phase,
            }
        )

    # 1. 已识别任务
    task_types = list(state.get("task_types") or ())
    route_label = str(state.get("route_label") or "")
    if task_types:
        push(KEY_TASK, route_label, STATUS_DONE, "route")
    else:
        push(KEY_TASK, route_label or "未能识别任务类型", STATUS_WARNING, "route")

    # 2. 已生成计划
    if plan:
        names = "、".join(_tool_label(str(item.get("tool", ""))) for item in plan)
        push(KEY_PLAN, f"共 {len(plan)} 个工具调用：{names}", STATUS_DONE, "plan")

    for record in approvals:
        if str(record.get("phase")) == "plan":
            push(KEY_APPROVAL_DONE, _approval_detail(record), STATUS_DONE, "guard")

    # 安全规则命中（route 节点即记录紧急事件提示，因此暂停时也可见）
    for item in violations:
        severity = str(item.get("severity", ""))
        push(
            KEY_GUARDRAIL,
            f"{item.get('title', '')}：{_short(item.get('detail', ''), 160)}",
            _SEVERITY_STATUS.get(severity, STATUS_INFO),
            "guard",
        )

    if pending and str(pending.get("kind", "plan")) == "plan":
        # The write was stopped before any tool ran, so nothing else happened.
        push(KEY_APPROVAL_PENDING, _pending_detail(pending), STATUS_PENDING, "guard")
        return _numbered(events)

    # 3. 已调用工具
    for payload in results:
        tool = str(payload.get("tool", ""))
        result_status = str(payload.get("status", ""))
        state_value = {
            "ok": STATUS_DONE,
            "blocked": STATUS_BLOCKED,
            "error": STATUS_REJECTED,
        }.get(result_status, STATUS_INFO)
        detail = str(payload.get("message") or "")
        if result_status == "ok":
            detail = str(payload.get("reason") or "") or "执行成功。"
        push(KEY_TOOL, f"{_tool_label(tool)}｜{_short(detail, 140)}", state_value, "tools")

    # 4. 已获取证据
    sds_payloads = [item for item in results if str(item.get("tool")) == "search_sds"]
    if sds_payloads:
        blocked = any(str(item.get("status")) == "blocked" for item in sds_payloads)
        push(
            KEY_EVIDENCE,
            _evidence_detail(sds_payloads),
            STATUS_BLOCKED if blocked else STATUS_DONE,
            "screen",
        )
    elif any(str(item.get("tool")) == "search_sds" for item in plan):
        push(KEY_EVIDENCE, "SDS 检索未执行或未返回结果。", STATUS_BLOCKED, "screen")

    # 5. 当前风险等级
    risk_payloads = [
        item
        for item in results
        if str(item.get("tool")) in {"calculate_risk", "draft_jsa"}
        and str(item.get("status")) == "ok"
    ]
    if risk_payloads:
        push(KEY_RISK, _risk_detail(risk_payloads), STATUS_DONE, "screen")

    # 6. 已生成 JSA
    jsa_payloads = [
        item
        for item in results
        if str(item.get("tool")) == "draft_jsa" and str(item.get("status")) == "ok"
    ]
    if jsa_payloads:
        push(KEY_JSA, _jsa_detail(jsa_payloads), STATUS_DONE, "tools")

    for record in approvals:
        if str(record.get("phase")) == "evidence":
            push(KEY_APPROVAL_DONE, _approval_detail(record), STATUS_DONE, "guard")

    # 7. 已执行写操作
    write_payloads = [
        item
        for item in results
        if str(item.get("tool")) in _WRITE_RESULT_TOOLS
        and str(item.get("status")) == "ok"
    ]
    if write_payloads:
        push(KEY_WRITE, _write_detail(write_payloads), STATUS_DONE, "tools")

    # 8. 等待审批（若在检索后暂停，这是最后一个事件）或已完成
    if pending:
        push(KEY_APPROVAL_PENDING, _pending_detail(pending), STATUS_PENDING, "guard")
    elif status == "rejected":
        push(
            KEY_COMPLETED,
            "审批被拒绝，流程已停止，未执行任何写操作。",
            STATUS_REJECTED,
            "summarize",
        )
    elif status == "blocked":
        push(KEY_COMPLETED, "存在未通过的安全规则，流程已阻断。", STATUS_BLOCKED, "summarize")
    else:
        push(KEY_COMPLETED, "工作流执行结束。", STATUS_DONE, "summarize")

    return _numbered(events)


__all__ = [
    "KEY_APPROVAL_DONE",
    "KEY_APPROVAL_PENDING",
    "KEY_COMPLETED",
    "KEY_EVIDENCE",
    "KEY_GUARDRAIL",
    "KEY_JSA",
    "KEY_PLAN",
    "KEY_RISK",
    "KEY_TASK",
    "KEY_TOOL",
    "KEY_WRITE",
    "STATUS_BLOCKED",
    "STATUS_DONE",
    "STATUS_ICONS",
    "STATUS_INFO",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "STATUS_WARNING",
    "TIMELINE_LABELS",
    "TimelineEvent",
    "build_timeline",
]

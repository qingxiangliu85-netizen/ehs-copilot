"""Deterministic, human-readable composition of a workflow run.

Kept separate from ``graph.py`` so the orchestration stays about control flow
and this module stays about wording.  No LLM is involved: the answer is built
directly from the structured tool payloads, which makes it reproducible in
tests and usable without an API key.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from config import NOT_FOUND_MESSAGE

from .hitl import ACTION_ICONS
from .state import STATUS_REJECTED, TOOL_LABELS


_STATUS_MARK = {"ok": "OK", "blocked": "跳过", "error": "失败"}

_OPERATION_DESCRIPTIONS = {
    "create_hazard": "新增一条隐患记录（写入隐患台账）",
    "update_hazard": "更新现有隐患记录",
    "close_hazard": "把隐患置为已关闭（整改闭环）",
    "major_risk_write": "重大风险相关的写操作",
    "risk_downgrade": "降低隐患的风险等级",
    "sds_insufficient_evidence": "在 SDS 证据不足的情况下继续使用结论",
    "sds_source_conflict": "在 SDS 来源冲突的情况下继续使用结论",
}


def _format_metrics(metrics: Mapping[str, Any]) -> str:
    return (
        f"- JSA 高/重大风险：{metrics.get('jsa_high_major', 0)} 项\n"
        f"- 待整改隐患：{metrics.get('hazard_pending', 0)} 项\n"
        f"- 已关闭隐患：{metrics.get('hazard_closed', 0)} 项\n"
        f"- 整改完成率：{float(metrics.get('completion_rate', 0.0)):.1f}%\n"
        f"- JSA 记录总数：{metrics.get('jsa_total', 0)} 条\n"
        f"- 隐患记录总数：{metrics.get('hazard_total', 0)} 条"
    )


def _format_distribution(distribution: Mapping[str, Any], unit: str = "项") -> str:
    parts = [f"{key} {value}{unit}" for key, value in distribution.items()]
    return "｜".join(parts)


def describe_tool_result(result: Mapping[str, Any]) -> str:
    """Return one readable block of text describing a single tool result."""
    tool = str(result.get("tool", ""))
    status = str(result.get("status", ""))

    if status == "error":
        return f"执行失败：{result.get('message', '未知错误')}"
    if status == "blocked":
        return f"已阻断：{result.get('message', '前置条件未满足')}"

    if tool == "search_sds":
        answer = str(result.get("answer", "")).strip()
        if not answer or answer == NOT_FOUND_MESSAGE:
            return "未在 SDS 知识库中检索到能够支持该问题的章节证据，已按“无依据不回答”处理。"
        lines = [f"检索回答：{answer}"]
        sources = result.get("sources") or []
        if sources:
            lines.append(
                "证据来源："
                + "；".join(f"{item[0]} 第 {item[1]} 页" for item in sources)
            )
        return "\n".join(lines)

    if tool == "calculate_risk":
        return (
            f"{result.get('formula', '')}，风险等级为 {result.get('risk_level', '')}。"
        )

    if tool == "draft_jsa":
        return (
            f"已生成 JSA 记录《{result.get('job_name', '')}》："
            f"初始风险 {result.get('risk_score', '')}·{result.get('risk_level', '')}，"
            f"残余风险 {result.get('residual_risk_score', '')}·{result.get('residual_risk_level', '')}"
            f"（当前会话 JSA 共 {result.get('jsa_record_count', 0)} 条）。"
        )

    if tool == "create_hazard":
        return (
            f"已新增隐患 {result.get('hazard_id', '')}："
            f"{result.get('record', {}).get('隐患描述', '')}｜"
            f"类型 {result.get('hazard_type', '')}｜等级 {result.get('risk_level', '')}｜"
            f"状态 {result.get('state', '')}"
            f"（当前会话隐患共 {result.get('hazard_record_count', 0)} 条）。"
        )

    if tool == "update_hazard":
        changed = "、".join(result.get("changed_fields", [])) or "无"
        return (
            f"已更新隐患 {result.get('hazard_id', '')}：变更字段 {changed}｜"
            f"当前状态 {result.get('state', '')}｜当前风险等级 {result.get('risk_level', '')}。"
        )

    if tool == "get_dashboard_summary":
        lines = [_format_metrics(result.get("metrics", {}))]
        risk = result.get("hazard_risk_distribution")
        status_dist = result.get("hazard_status_distribution")
        if risk:
            lines.append(f"隐患风险分布：{_format_distribution(risk)}")
        if status_dist:
            lines.append(f"整改状态分布：{_format_distribution(status_dist)}")
        priority_hazards = result.get("priority_hazards") or []
        priority_jsa = result.get("priority_jsa") or []
        lines.append(
            f"重点关注：高/重大未关闭隐患 {len(priority_hazards)} 项，"
            f"高/重大风险 JSA {len(priority_jsa)} 条。"
        )
        return "\n".join(lines)

    return "工具已执行完成。"


def tool_headline(result: Mapping[str, Any]) -> str:
    """Return a one-line summary used in the step table."""
    status = str(result.get("status", ""))
    if status in {"error", "blocked"}:
        return str(result.get("message", ""))[:60]
    return describe_tool_result(result).splitlines()[0][:80]


# --------------------------------------------------------------------------- #
# Approval / guardrail narration
# --------------------------------------------------------------------------- #


def _calls_brief(calls: Iterable[Mapping[str, Any]], limit: int = 4) -> str:
    parts: list[str] = []
    for item in list(calls)[:limit]:
        tool = str(item.get("tool", ""))
        arguments = dict(item.get("arguments") or {})
        rendered = json.dumps(arguments, ensure_ascii=False, default=str)
        if len(rendered) > 200:
            rendered = f"{rendered[:200]}…"
        parts.append(f"{TOOL_LABELS.get(tool, tool)} {rendered}")
    return "；".join(parts)


def describe_approval_request(pending: Mapping[str, Any]) -> str:
    """Return the operator-facing description of a pending approval."""
    operation = str(pending.get("operation", ""))
    lines = [
        "⏳ **该工作流已暂停，等待人工确认。**",
        "",
        f"- 待确认操作：**{pending.get('operation_label') or operation}**"
        f"（{_OPERATION_DESCRIPTIONS.get(operation, operation)}）",
        f"- 轮次：第 {pending.get('round', 1)} 轮",
    ]
    reason = str(pending.get("reason") or pending.get("summary") or "")
    if reason:
        lines.append(f"- 触发原因：{reason}")
    guard_codes = list(pending.get("guard_codes") or ())
    if guard_codes:
        lines.append("- 命中规则：" + "、".join(str(item) for item in guard_codes))
    calls = list(pending.get("calls") or ())
    if calls:
        lines.append(f"- 拟执行内容：{_calls_brief(calls)}")
    current = dict(pending.get("current") or {})
    if current:
        lines.append(
            "- 变更前：" + json.dumps(current, ensure_ascii=False, default=str)
        )
    proposed = dict(pending.get("proposed") or {})
    if proposed:
        lines.append(
            "- 变更后：" + json.dumps(proposed, ensure_ascii=False, default=str)
        )
    lines.extend(
        [
            "",
            "> 在获得批准之前，上述操作不会执行。请选择：**批准** / **修改参数后批准** / **拒绝**。",
        ]
    )
    return "\n".join(lines)


def describe_rejection(approvals: Iterable[Mapping[str, Any]]) -> str:
    """Return the answer shown when a human rejected the pending operation."""
    rejected = [
        item for item in approvals if str(item.get("action")) == "reject"
    ] or list(approvals)
    lines = [
        "⛔ **本次流程已被人工拒绝，已停止执行。**",
        "",
        "没有执行任何工具调用，也没有写入任何记录。",
        "",
    ]
    for item in rejected:
        operation = str(item.get("operation", ""))
        lines.append(
            f"- 被拒绝的操作：{item.get('operation_label') or operation}"
            f"（{_OPERATION_DESCRIPTIONS.get(operation, operation)}）"
        )
        if item.get("note"):
            lines.append(f"- 拒绝原因：{item['note']}")
    lines.extend(
        [
            "",
            "如需继续，请重新发起任务并批准，或调整参数后重试。",
        ]
    )
    return "\n".join(lines)


def describe_approval_log(approvals: Iterable[Mapping[str, Any]]) -> str:
    """Return the 'approved/rejected by a human' footer for the answer."""
    lines: list[str] = []
    for item in approvals:
        action = str(item.get("action", ""))
        icon = ACTION_ICONS.get(action, "✅" if item.get("auto") else "·")
        note = f"（{item['note']}）" if item.get("note") else ""
        origin = "自动放行" if item.get("auto") else "人工"
        lines.append(
            f"- {icon} {item.get('operation_label', '')}："
            f"{item.get('action_label', '')}｜{origin}处置{note}"
        )
    return "\n".join(lines)


def describe_guardrails(violations: Iterable[Mapping[str, Any]]) -> str:
    """Return the guardrail findings footer for the answer."""
    lines: list[str] = []
    for item in violations:
        severity = {
            "block": "阻断",
            "approval": "需审批",
            "notice": "提示",
        }.get(str(item.get("severity", "")), str(item.get("severity", "")))
        lines.append(
            f"- [{severity}] {item.get('title', '')}：{item.get('detail', '')}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Answer composition
# --------------------------------------------------------------------------- #


def compose_answer(
    route_label: str,
    plan: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    *,
    status: str = "",
    approvals: Iterable[Mapping[str, Any]] = (),
    pending_approval: Mapping[str, Any] | None = None,
    guardrails: Iterable[Mapping[str, Any]] = (),
) -> str:
    """Compose the final assistant answer from the executed tool results."""
    approvals = list(approvals)
    violations = list(guardrails)

    notices = [item for item in violations if str(item.get("severity")) == "notice"]
    emergency_prefix = "\n\n".join(
        str(item.get("detail", "")) for item in notices if item.get("detail")
    )

    if pending_approval:
        body = describe_approval_request(pending_approval)
        header = f"**识别任务：{route_label}**" if route_label else ""
        return "\n\n".join(item for item in (emergency_prefix, header, body) if item)

    if status == STATUS_REJECTED:
        header = f"**识别任务：{route_label}**" if route_label else ""
        body = describe_rejection(approvals)
        log = describe_approval_log(approvals)
        parts = [emergency_prefix, header, body, log]
        return "\n\n".join(item for item in parts if item).strip()

    if not plan:
        return (
            f"未能把这条请求识别为已支持的任务（{route_label}）。\n\n"
            "当前支持：SDS 查询、JSA 风险评估、隐患管理、仪表盘汇总，"
            "以及由它们组合而成的任务。请补充更具体的描述后重试。"
        )

    lines = [f"**识别任务：{route_label}**", ""]
    for index, result in enumerate(tool_results, start=1):
        tool = str(result.get("tool", ""))
        label = TOOL_LABELS.get(tool, tool)
        mark = _STATUS_MARK.get(str(result.get("status", "")), "")
        lines.append(f"**{index}. {label}（{mark}）**")
        lines.append("")
        lines.append(describe_tool_result(result))
        lines.append("")

    log = describe_approval_log(approvals)
    if log:
        lines.extend(["**人工审批记录**", "", log, ""])

    findings = describe_guardrails(violations)
    if findings:
        lines.extend(["**安全规则（Guardrail）**", "", findings, ""])

    lines.append("> AI 输出仅用于信息检索与记录辅助，实际操作前请核对原始 SDS 及所在单位 EHS 制度。")
    composed = "\n".join(lines).strip()
    if emergency_prefix:
        composed = f"{emergency_prefix}\n\n{composed}"
    if str(status) == "blocked":
        composed = (
            "🚫 **本次流程因未通过安全规则而被阻断，相关结论不可直接使用。**\n\n"
            + composed
        )
    return composed


__all__ = [
    "compose_answer",
    "describe_approval_log",
    "describe_approval_request",
    "describe_guardrails",
    "describe_rejection",
    "describe_tool_result",
    "tool_headline",
]

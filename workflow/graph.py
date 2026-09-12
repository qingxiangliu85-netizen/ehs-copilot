"""LangGraph orchestration for the EHS Copilot workflow assistant.

Graph shape::

    START → route → plan → guard_plan ─┬─(拒绝 / 无需执行)→ summarize → END
                                       └─(通过)→ tools → screen_evidence ─┐
                                                                         └→ summarize → END

* ``route``            classify the sentence into task types and extract parameters
* ``plan``             turn the decision into explicit, ordered tool calls
* ``guard_plan``       guardrails **and human approval before any tool runs**
* ``tools``            :class:`langgraph.prebuilt.ToolNode` executes the calls
* ``screen_evidence``  guardrails **and human approval after the SDS retrieval**
* ``summarize``        compose the answer, the step trace and the tool results

Human-in-the-loop is LangGraph-native: a gate calls ``interrupt()``, the graph
checkpoints and returns to the caller, and :func:`resume_workflow` continues the
*same thread* with the operator's decision.  Nothing before the gate re-runs, so
a write can never be executed twice.

The plan itself is still produced by the deterministic router, so no LLM is
required to decide which tools to call.
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from tools import LANGCHAIN_TOOLS, ToolContext, current_context, use_context

from . import guardrails, hitl, router
from .guardrails import SEVERITY_APPROVAL, SEVERITY_BLOCK, SEVERITY_NOTICE
from .state import (
    STATUS_AWAITING_APPROVAL,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_REJECTED,
    STEP_INFO,
    STEP_OK,
    STEP_WARN,
    TASK_LABELS,
    TOOL_LABELS,
    WorkflowResult,
    WorkflowState,
    make_step,
    new_state,
)
from .summary import compose_answer, tool_headline
from .timeline import build_timeline


GRAPH_NODE_ORDER: tuple[str, ...] = (
    "route",
    "plan",
    "guard_plan",
    "tools",
    "screen_evidence",
    "summarize",
)

_STEP_ROUTE = 1
_STEP_PLAN = 2
_STEP_TOOLS_START = 3

# Maps a plan argument name onto the hazard record field it changes, so the
# approval request can show "变更前 / 变更后" honestly.
_HAZARD_FIELD_MAP: dict[str, str] = {
    "hazard_id": "隐患编号",
    "status": "状态",
    "risk_level": "风险等级",
    "owner": "责任人",
    "description": "隐患描述",
    "hazard_type": "隐患类型",
    "corrective_action": "整改措施",
    "due_on": "整改期限",
    "found_on": "发现日期",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _jsonable(value: Any) -> Any:
    """Convert tuples into lists so the state stays JSON-friendly."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def plan_to_tool_calls(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the router's plan into LangChain tool-call dictionaries."""
    return [
        {
            "name": str(item["tool"]),
            "args": dict(item.get("arguments") or {}),
            "id": str(item.get("call_id") or f"call_{index}"),
            "type": "tool_call",
        }
        for index, item in enumerate(plan, start=1)
    ]


def _parse_tool_message(message: ToolMessage) -> dict[str, Any]:
    """Normalise a ToolMessage payload back into a dictionary."""
    content = message.content
    if isinstance(content, dict):
        payload: dict[str, Any] = dict(content)
    elif isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            parsed = {"raw": content}
        payload = dict(parsed) if isinstance(parsed, dict) else {"raw": parsed}
    else:
        payload = {"raw": str(content)}

    payload.setdefault("tool", message.name or "unknown")
    payload["call_id"] = str(getattr(message, "tool_call_id", "") or "")
    return payload


def collect_tool_payloads(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the payload of every tool message currently in the state."""
    payloads: list[dict[str, Any]] = []
    for message in state.get("messages") or ():
        if isinstance(message, ToolMessage):
            payloads.append(_parse_tool_message(message))
    return payloads


def _safe_records() -> tuple[list[dict[str, object]], tuple[str, ...]]:
    """Return the live session records, or empties when no context is bound."""
    try:
        context = current_context()
    except Exception:  # noqa: BLE001 - guardrails must not break the graph
        return [], ()
    return list(context.hazard_records), tuple(context.loaded_files)


# --------------------------------------------------------------------------- #
# Routing / planning nodes (unchanged behaviour from phase 1)
# --------------------------------------------------------------------------- #


def route_node(state: WorkflowState) -> dict[str, Any]:
    """Classify the user sentence and extract the parameters it carries.

    The emergency notice lives here rather than in ``guard_plan`` because
    ``route`` never interrupts: its output is always checkpointed, so the notice
    survives a later human gate.
    """
    user_input = str(state.get("user_input", ""))
    decision = router.route(user_input)

    if decision.task_types:
        hits = "；".join(
            f"{TASK_LABELS.get(task, task)}←" + "/".join(words[:5])
            for task, words in decision.matched_keywords.items()
            if words
        )
        detail = f"{decision.label}" + (f"（命中：{hits}）" if hits else "")
        status = STEP_OK
    else:
        detail = "未匹配到已知任务类型。"
        status = STEP_WARN

    updates: dict[str, Any] = {
        "route_label": decision.label,
        "task_types": list(decision.task_types),
        "matched_keywords": decision.matched_keywords,
        "parameters": _jsonable(decision.parameters),
        "plan": [item.to_dict() for item in decision.plan],
        "steps": [
            make_step(_STEP_ROUTE, "route", "识别任务类型", detail, status)
        ],
        "errors": [] if decision.task_types else ["未能识别任务类型。"],
    }

    notice = guardrails.emergency_notice(user_input)
    if notice:
        updates["guardrails"] = [notice.to_dict()]
    return updates


def plan_node(state: WorkflowState) -> dict[str, Any]:
    """Emit the ordered tool calls the router decided on."""
    plan = list(state.get("plan") or [])
    tool_calls = plan_to_tool_calls(plan)

    lines = [
        f"{index}. {TOOL_LABELS.get(str(item.get('tool')), item.get('tool'))}"
        f" — {item.get('reason', '')}"
        for index, item in enumerate(plan, start=1)
    ]
    detail = "\n".join(lines) if lines else "无需调用工具。"

    return {
        "messages": [AIMessage(content="", tool_calls=tool_calls)],
        "steps": [
            make_step(
                _STEP_PLAN,
                "plan",
                "生成工具调用计划",
                detail,
                STEP_OK if plan else STEP_WARN,
            )
        ],
    }


# --------------------------------------------------------------------------- #
# Approval helpers
# --------------------------------------------------------------------------- #


def _change_sets(
    calls: list[dict[str, Any]],
    hazard_records: list[dict[str, object]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the 变更前 / 变更后 snapshots shown in the approval request."""
    current: dict[str, Any] = {}
    proposed: dict[str, Any] = {}

    for entry in calls:
        tool = str(entry.get("tool", ""))
        arguments = dict(entry.get("arguments") or {})
        if tool == "update_hazard":
            identifier = str(arguments.get("hazard_id", ""))
            existing: dict[str, Any] = {}
            for record in hazard_records:
                if str(record.get("隐患编号", "")).strip().upper() == identifier.strip().upper():
                    existing = dict(record)
                    break
            for key, value in arguments.items():
                if key in {"hazard_id"} or value in (None, "", [], {}):
                    continue
                field = _HAZARD_FIELD_MAP.get(key, key)
                current.setdefault(identifier, {})[field] = existing.get(field, "（未找到该隐患）")
                proposed.setdefault(identifier, {})[field] = value
        else:
            label = str(arguments.get("hazard_id") or "（自动生成编号）")
            for key, value in arguments.items():
                field = _HAZARD_FIELD_MAP.get(key, key)
                proposed.setdefault(label, {})[field] = value
    return current, proposed


def _plan_request(
    plan: list[dict[str, Any]],
    violations: list[guardrails.Violation],
    hazard_records: list[dict[str, object]],
) -> hitl.ApprovalRequest:
    """Aggregate every write-approval into one request the operator answers."""
    calls: list[dict[str, Any]] = []
    operations: list[str] = []
    for item in plan:
        tool = str(item.get("tool", ""))
        arguments = dict(item.get("arguments") or {})
        own = guardrails.write_violations(tool, arguments, hazard_records)
        if not own:
            continue
        calls.append(
            {
                "call_id": str(item.get("call_id", "")),
                "tool": tool,
                "arguments": arguments,
                "operations": [item.operation for item in own],
                "reasons": [item.detail for item in own],
            }
        )
        operations.extend(item.operation for item in own)

    current, proposed = _change_sets(calls, hazard_records)
    call_ids = [str(item["call_id"]) for item in calls]
    return hitl.ApprovalRequest(
        gate_id="plan:" + ",".join(call_ids),
        operation=hitl.primary_operation(operations),
        reason="；".join(dict.fromkeys(item.detail for item in violations)),
        summary=f"本次计划包含 {len(calls)} 个写操作，需人工确认后才能执行。",
        calls=tuple(calls),
        current=current,
        proposed=proposed,
        guard_codes=tuple(dict.fromkeys(item.code for item in violations)),
        round=1,
        kind="plan",
    )


def _evidence_request(
    payload: Mapping[str, Any],
    violations: list[guardrails.Violation],
) -> hitl.ApprovalRequest:
    """Build the approval request for an under-evidenced SDS conclusion."""
    operation = hitl.primary_operation([item.operation for item in violations])
    return hitl.ApprovalRequest(
        gate_id="evidence:search_sds",
        operation=operation,
        reason="；".join(dict.fromkeys(item.detail for item in violations)),
        summary="SDS 检索结果未完全满足证据要求，继续使用该结论需人工确认。",
        calls=(
            {
                "call_id": "evidence:search_sds",
                "tool": "search_sds",
                "arguments": {"question": str(payload.get("question", ""))},
                "operations": [item.operation for item in violations],
                "reasons": [item.detail for item in violations],
            },
        ),
        current={},
        proposed={},
        guard_codes=tuple(dict.fromkeys(item.code for item in violations)),
        round=1,
        kind="evidence",
    )


def _normalise_decision(
    raw: Any, request: hitl.ApprovalRequest
) -> tuple[hitl.ApprovalDecision | None, str]:
    """Validate a resume payload against the request it is answering."""
    decision = hitl.ApprovalDecision.from_payload(raw)
    if decision is None:
        return None, "审批返回内容无法识别，已按拒绝处理。"
    if decision.gate_id and decision.gate_id != request.gate_id:
        return None, (
            f"审批编号不匹配（收到 {decision.gate_id}，当前需要 {request.gate_id}），"
            "已按拒绝处理。"
        )
    if decision.round > hitl.MAX_APPROVAL_ROUNDS:
        return None, "审批轮次超出上限，已按拒绝处理。"
    return decision, ""


def _blocked_override(message: str, codes: list[str]) -> dict[str, Any]:
    return {"status": "blocked", "message": message, "guard_codes": list(codes)}


def _apply_plan_modification(
    plan: list[dict[str, Any]],
    request: hitl.ApprovalRequest,
    decision: hitl.ApprovalDecision,
    hazard_records: list[dict[str, object]],
) -> tuple[list[dict[str, Any]], str, list[guardrails.Violation]]:
    """Return ``(new_plan, refusal_reason, escalated_violations)``."""
    original = {str(item["call_id"]): item for item in request.calls}
    allowed_operations = {
        operation
        for item in request.calls
        for operation in (item.get("operations") or ())
    }

    replacements: dict[str, dict[str, Any]] = {}
    for entry in decision.calls:
        call_id = str(entry.get("call_id", ""))
        if call_id not in original:
            return plan, f"修改内容引用了本计划中不存在的调用 {call_id!r}，已按拒绝处理。", []
        tool = str(entry.get("tool") or original[call_id].get("tool"))
        if tool != str(original[call_id].get("tool")):
            return plan, f"不允许在审批修改中更换工具（{call_id}），已按拒绝处理。", []
        replacements[call_id] = {
            "tool": tool,
            "arguments": dict(entry.get("arguments") or {}),
        }

    if not replacements:
        return plan, "审批动作是「修改」但未提供任何修改内容，已按拒绝处理。", []

    new_plan: list[dict[str, Any]] = []
    for item in plan:
        call_id = str(item.get("call_id", ""))
        if call_id not in replacements:
            new_plan.append(dict(item))
            continue
        updated = dict(item)
        updated["arguments"] = replacements[call_id]["arguments"]
        new_plan.append(updated)

    escalated: list[guardrails.Violation] = []
    for item in new_plan:
        for violation in guardrails.write_violations(
            str(item.get("tool", "")), item.get("arguments") or {}, hazard_records
        ):
            if violation.operation not in allowed_operations:
                escalated.append(violation)

    if escalated:
        detail = "；".join(item.detail for item in escalated)
        return (
            plan,
            "修改后的参数引入了新的审批条件，单次人工审批不能覆盖，已按拒绝处理。"
            f"（{detail}）请重新发起任务以获得新的审批。",
            escalated,
        )
    return new_plan, "", []


# --------------------------------------------------------------------------- #
# Guardrail nodes
# --------------------------------------------------------------------------- #


def guard_plan_node(state: WorkflowState) -> dict[str, Any]:
    """Run the pre-execution guardrails and, if needed, ask a human.

    Order of business:

    1. risk-argument integrity — a plan may never carry a score (``block``),
    2. write-approval gate (``approval``) — the only human gate in this node.

    The emergency notice is emitted by ``route`` instead: a node that interrupts
    never returns its own state updates, so anything decided here would be lost
    whenever the gate fires.
    """
    plan = [dict(item) for item in (state.get("plan") or ())]
    hazard_records, _ = _safe_records()

    findings: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    updates: dict[str, Any] = {}

    # --- 1. risk integrity: blocked calls are silently dropped --------------
    blocked_call_ids: list[str] = []
    for item in plan:
        tool = str(item.get("tool", ""))
        if tool not in guardrails.RISK_TOOLS:
            continue
        own = guardrails.risk_argument_violations([item])
        if own:
            findings.extend(violation.to_dict() for violation in own)
            blocked_call_ids.append(str(item.get("call_id", "")))

    if blocked_call_ids:
        remaining = [
            item
            for item in plan
            if str(item.get("call_id", "")) not in blocked_call_ids
        ]
        updates["messages"] = [AIMessage(content="", tool_calls=plan_to_tool_calls(remaining))]

    # --- 3. write approval gate --------------------------------------------
    write_violations = guardrails.plan_write_violations(plan, hazard_records)
    if write_violations:
        request = _plan_request(plan, write_violations, hazard_records)
        auto = bool(state.get("auto_approve", True))
        if auto:
            findings.extend(violation.to_dict() for violation in write_violations)
            approvals.append(
                hitl.approval_record(
                    request,
                    hitl.auto_decision(
                        request.gate_id,
                        note="调用方未启用人工审批（auto_approve=True），已自动放行。",
                    ),
                    phase="plan",
                    auto=True,
                )
            )
        else:
            raw = interrupt(request.to_payload())
            decision, problem = _normalise_decision(raw, request)

            if decision is None:
                findings.append(
                    guardrails.Violation(
                        code="approval_payload_invalid",
                        severity=SEVERITY_APPROVAL,
                        detail=problem,
                        tool=request.tools[0] if request.tools else "",
                    ).to_dict()
                )
                approvals.append(
                    hitl.approval_record(
                        request,
                        hitl.ApprovalDecision(
                            gate_id=request.gate_id,
                            action=hitl.ACTION_REJECT,
                            note=problem,
                        ),
                        phase="plan",
                    )
                )
                updates["status"] = STATUS_REJECTED
                updates["guardrails"] = findings
                updates["approvals"] = approvals
                return updates

            if decision.is_reject:
                approvals.append(
                    hitl.approval_record(request, decision, phase="plan")
                )
                updates["status"] = STATUS_REJECTED
            elif decision.is_modify:
                new_plan, refusal, escalated = _apply_plan_modification(
                    plan, request, decision, hazard_records
                )
                findings.extend(violation.to_dict() for violation in escalated)
                record = hitl.approval_record(request, decision, phase="plan")
                if refusal:
                    record["action"] = hitl.ACTION_REJECT
                    record["action_label"] = hitl.ACTION_LABELS[hitl.ACTION_REJECT]
                    record["note"] = refusal
                    approvals.append(record)
                    updates["status"] = STATUS_REJECTED
                else:
                    record["note"] = decision.note or "已按修改后的参数执行。"
                    approvals.append(record)
                    updates["plan"] = new_plan
                    updates["messages"] = [
                        AIMessage(content="", tool_calls=plan_to_tool_calls(new_plan))
                    ]
            else:
                approvals.append(hitl.approval_record(request, decision, phase="plan"))

    if findings:
        updates["guardrails"] = findings
    if approvals:
        updates["approvals"] = approvals
    return updates


def screen_evidence_node(state: WorkflowState) -> dict[str, Any]:
    """Run the post-retrieval guardrails over every tool payload.

    Covers the risk-score integrity check and the SDS evidence rules (file name,
    page, original snippet, source conflict, chemical coverage).
    """
    payloads = collect_tool_payloads(state)
    if not payloads:
        return {}

    hazard_records, loaded_files = _safe_records()
    findings: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    overrides: dict[str, dict[str, Any]] = {}

    # --- risk scores must be reproducible with jsa.calculate_risk -----------
    for payload in payloads:
        violation = guardrails.risk_payload_violation(payload)
        if violation:
            findings.append(violation.to_dict())
            overrides[str(payload.get("tool", ""))] = _blocked_override(
                violation.detail, [violation.code]
            )

    # --- SDS conclusion guardrails -----------------------------------------
    sds_payloads = [
        item for item in payloads if str(item.get("tool")) == "search_sds"
    ]
    for payload in sds_payloads:
        if str(payload.get("status")) != "ok":
            continue
        question = str(payload.get("question") or state.get("user_input") or "")
        violations = guardrails.sds_violations(
            payload,
            question=question,
            loaded_files=loaded_files,
        )
        if not violations:
            continue

        findings.extend(violation.to_dict() for violation in violations)
        blocks = [item for item in violations if item.severity == SEVERITY_BLOCK]
        needs_approval = [
            item for item in violations if item.severity == SEVERITY_APPROVAL
        ]

        if blocks:
            overrides["search_sds"] = _blocked_override(
                "；".join(item.detail for item in blocks),
                [item.code for item in blocks],
            )
            continue

        if not needs_approval:
            continue

        request = _evidence_request(payload, needs_approval)
        if bool(state.get("auto_approve", True)):
            approvals.append(
                hitl.approval_record(
                    request,
                    hitl.auto_decision(
                        request.gate_id,
                        note="调用方未启用人工审批，证据不足的结论不予放行。",
                    ),
                    phase="evidence",
                    auto=True,
                )
            )
            overrides["search_sds"] = _blocked_override(
                "无人确认时，SDS 证据不足或来源冲突的结论一律阻断："
                + "；".join(item.detail for item in needs_approval)
                + " 请补充证据，或在交互式页面中由 EHS 人员确认后继续。",
                [item.code for item in needs_approval],
            )
            continue

        raw = interrupt(request.to_payload())
        decision, problem = _normalise_decision(raw, request)

        if decision is None or decision.is_reject:
            note = problem or decision.note or "已拒绝使用该 SDS 结论。"
            record = (
                hitl.approval_record(request, decision, phase="evidence")
                if decision is not None
                else hitl.approval_record(
                    request,
                    hitl.ApprovalDecision(
                        gate_id=request.gate_id,
                        action=hitl.ACTION_REJECT,
                        note=note,
                    ),
                    phase="evidence",
                )
            )
            if decision is not None and decision.note:
                record["note"] = decision.note
            approvals.append(record)
            overrides["search_sds"] = _blocked_override(
                "该 SDS 结论未获人工确认，已阻断："
                + "；".join(item.detail for item in needs_approval)
                + f"（{note}）",
                [item.code for item in needs_approval],
            )
        else:
            record = hitl.approval_record(request, decision, phase="evidence")
            if decision.is_modify and decision.note:
                record["note"] = decision.note or ""
            approvals.append(record)
            overrides["search_sds"] = {
                "status": "ok",
                "human_confirmed": True,
                "message": "证据不足或来源冲突，已由人工确认后继续使用。",
                "guard_codes": [item.code for item in needs_approval],
            }

        # Only one human gate per node execution: LangGraph re-plays a node from
        # the top on resume, so a second ``interrupt()`` in the same run would
        # create a second, harder-to-reason-about pause.
        break

    updates: dict[str, Any] = {}
    if findings:
        updates["guardrails"] = findings
    if approvals:
        updates["approvals"] = approvals
    if overrides:
        updates["tool_overrides"] = overrides
    _ = hazard_records
    return updates


# --------------------------------------------------------------------------- #
# Summarize
# --------------------------------------------------------------------------- #


def _governance_steps(state: Mapping[str, Any], start: int) -> list[dict[str, Any]]:
    """Build the approval / guardrail steps that sit between plan and tools."""
    steps: list[dict[str, Any]] = []
    order = start

    for item in state.get("approvals") or ():
        automatic = bool(item.get("auto"))
        steps.append(
            make_step(
                order,
                "guard",
                "自动放行（非交互式调用）" if automatic else "人工审批",
                f"{item.get('operation_label', '')}：{item.get('action_label', '')}",
                STEP_INFO if automatic else STEP_OK,
            )
        )
        order += 1

    for item in state.get("guardrails") or ():
        severity = str(item.get("severity", ""))
        steps.append(
            make_step(
                order,
                "guard",
                f"安全规则：{item.get('title', '')}",
                str(item.get("detail", "")),
                STEP_WARN if severity in {SEVERITY_BLOCK, SEVERITY_APPROVAL} else STEP_INFO,
            )
        )
        order += 1
    return steps


def summarize_node(state: WorkflowState) -> dict[str, Any]:
    """Collect the tool results, build the answer and close the step trace."""
    plan = list(state.get("plan") or [])
    plan_by_call = {str(item.get("call_id")): item for item in plan}
    overrides = dict(state.get("tool_overrides") or {})

    results: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = _governance_steps(state, _STEP_TOOLS_START)
    orders = iter(range(_STEP_TOOLS_START + len(steps), 1000))
    errors: list[str] = []

    for message in state.get("messages", []):
        if not isinstance(message, ToolMessage):
            continue
        payload = _parse_tool_message(message)
        planned = plan_by_call.get(payload["call_id"])
        if planned:
            payload["arguments"] = planned.get("arguments") or {}
            payload["reason"] = planned.get("reason") or ""

        override = overrides.get(str(payload.get("tool", "")))
        if override and str(payload.get("status")) == "ok":
            payload.update(override)

        results.append(payload)

        tool = str(payload.get("tool", ""))
        label = TOOL_LABELS.get(tool, tool)
        status = str(payload.get("status", ""))
        steps.append(
            make_step(
                next(orders),
                "tools",
                f"调用工具：{label}",
                tool_headline(payload),
                STEP_OK if status == "ok" else STEP_WARN,
            )
        )
        if status == "error":
            errors.append(f"{label}：{payload.get('message', '')}")

    executed = {str(item.get("call_id")) for item in results}
    skipped = [
        item for item in plan if str(item.get("call_id")) not in executed
    ]
    if skipped:
        names = "、".join(
            TOOL_LABELS.get(str(item.get("tool")), str(item.get("tool")))
            for item in skipped
        )
        steps.append(
            make_step(
                next(orders),
                "summarize",
                "跳过未执行的调用",
                f"{len(skipped)} 个调用未执行：{names}",
                STEP_WARN,
            )
        )

    findings = [dict(item) for item in (state.get("guardrails") or ())]
    approvals = [dict(item) for item in (state.get("approvals") or ())]

    status = str(state.get("status") or "")
    if not status:
        guard_blocked = any(
            str(item.get("severity")) == SEVERITY_BLOCK for item in findings
        )
        tool_blocked = any(str(item.get("status")) == "blocked" for item in results)
        status = (
            STATUS_BLOCKED if (guard_blocked or tool_blocked) else STATUS_COMPLETED
        )

    route_label = str(state.get("route_label", ""))
    answer = compose_answer(
        route_label,
        plan,
        results,
        status=status,
        approvals=approvals,
        pending_approval=None,
        guardrails=findings,
    )
    steps.append(
        make_step(
            next(orders),
            "summarize",
            "汇总执行结果",
            f"共 {len(results)} 个工具返回结果。",
            STEP_OK if results else STEP_WARN,
        )
    )

    return {
        "tool_results": results,
        "final_answer": answer,
        "steps": steps,
        "errors": errors,
        "status": status,
    }


def _next_after_plan(state: WorkflowState) -> str:
    if str(state.get("status") or "") == STATUS_REJECTED:
        return "summarize"
    return "tools" if state.get("plan") else "summarize"


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #


def build_graph(*, checkpointer: Any = None):
    """Build and compile the workflow graph.

    A checkpointer is required for the human-in-the-loop gates; the module level
    :data:`COMPILED_GRAPH` always gets one.
    """
    builder = StateGraph(WorkflowState)
    builder.add_node("route", route_node)
    builder.add_node("plan", plan_node)
    builder.add_node("guard_plan", guard_plan_node)
    builder.add_node("tools", ToolNode(list(LANGCHAIN_TOOLS)))
    builder.add_node("screen_evidence", screen_evidence_node)
    builder.add_node("summarize", summarize_node)

    builder.add_edge(START, "route")
    builder.add_edge("route", "plan")
    builder.add_edge("plan", "guard_plan")
    builder.add_conditional_edges(
        "guard_plan",
        _next_after_plan,
        {"tools": "tools", "summarize": "summarize"},
    )
    builder.add_edge("tools", "screen_evidence")
    builder.add_edge("screen_evidence", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile(checkpointer=checkpointer)


CHECKPOINTER = MemorySaver()
COMPILED_GRAPH = build_graph(checkpointer=CHECKPOINTER)


def _extract_pending(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the pending approval payload carried by an interrupted run."""
    interrupts = raw.get("__interrupt__") or ()
    for item in interrupts:
        value = getattr(item, "value", None)
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _citations_ok(results: list[dict[str, Any]]) -> bool:
    for payload in results:
        if str(payload.get("tool")) != "search_sds":
            continue
        if str(payload.get("status")) == "blocked":
            return False
        if str(payload.get("status")) != "ok":
            continue
        evidence = list(payload.get("evidence") or ())
        if not evidence:
            return False
        for row in evidence:
            if not row.get("source") or not row.get("page") or not row.get("snippet"):
                return False
    return True


def _to_result(
    raw: Mapping[str, Any],
    *,
    user_input: str,
    thread_id: str,
    elapsed_ms: int,
) -> WorkflowResult:
    """Turn a raw graph state (possibly interrupted) into a result snapshot."""
    pending = _extract_pending(raw)

    results = [dict(item) for item in (raw.get("tool_results") or ())]
    if not results:
        results = collect_tool_payloads(raw)
        for payload in results:
            payload.pop("reason", None)

    status = str(raw.get("status") or "")
    if not status:
        status = STATUS_AWAITING_APPROVAL if pending else STATUS_COMPLETED

    timeline = build_timeline(raw, pending_approval=pending, tool_results=results)

    return WorkflowResult(
        user_input=str(user_input or raw.get("user_input") or ""),
        route_label=str(raw.get("route_label", "")),
        task_types=tuple(raw.get("task_types") or ()),
        matched_keywords=dict(raw.get("matched_keywords") or {}),
        parameters=dict(raw.get("parameters") or {}),
        plan=tuple(raw.get("plan") or ()),
        steps=tuple(raw.get("steps") or ()),
        tool_results=tuple(results),
        final_answer=str(raw.get("final_answer", ""))
        or (
            compose_answer(
                str(raw.get("route_label", "")),
                list(raw.get("plan") or ()),
                results,
                status=status,
                approvals=list(raw.get("approvals") or ()),
                pending_approval=pending,
                guardrails=list(raw.get("guardrails") or ()),
            )
            if pending
            else ""
        ),
        errors=tuple(raw.get("errors") or ()),
        elapsed_ms=elapsed_ms,
        thread_id=thread_id,
        status=status,
        pending_approval=pending,
        approvals=tuple(raw.get("approvals") or ()),
        guardrails=tuple(raw.get("guardrails") or ()),
        timeline=tuple(item.to_dict() for item in timeline),
        citations_ok=_citations_ok(results),
    )


def _config(thread_id: str, recursion_limit: int) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }


def new_thread_id() -> str:
    """Return a fresh workflow thread identifier."""
    return f"wf-{uuid4().hex}"


def run_workflow(
    user_input: str,
    context: ToolContext,
    *,
    thread_id: str | None = None,
    auto_approve: bool = True,
    recursion_limit: int = 25,
) -> WorkflowResult:
    """Run one task through the graph and return an immutable result snapshot.

    ``auto_approve`` defaults to ``True`` so that programmatic callers keep the
    phase-1 behaviour.  The interactive Streamlit page always passes
    ``auto_approve=False``: every gated operation then waits for a real human.

    When a gate fires the returned result has ``pending_approval`` set and
    ``status == "awaiting_approval"``; continue it with :func:`resume_workflow`
    using the same ``thread_id``.
    """
    thread = str(thread_id or new_thread_id())
    started = time.perf_counter()
    with use_context(context):
        raw = COMPILED_GRAPH.invoke(
            new_state(user_input, auto_approve=auto_approve, thread_id=thread),
            _config(thread, recursion_limit),
        )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return _to_result(
        raw, user_input=str(user_input or ""), thread_id=thread, elapsed_ms=elapsed_ms
    )


def resume_workflow(
    thread_id: str,
    context: ToolContext,
    decision: hitl.ApprovalDecision | Mapping[str, Any],
    *,
    recursion_limit: int = 25,
) -> WorkflowResult:
    """Continue a paused workflow with the operator's decision."""
    payload = (
        decision.to_payload()
        if isinstance(decision, hitl.ApprovalDecision)
        else dict(decision)
    )
    thread = str(thread_id)
    started = time.perf_counter()
    with use_context(context):
        raw = COMPILED_GRAPH.invoke(
            Command(resume=payload),
            _config(thread, recursion_limit),
        )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return _to_result(
        raw,
        user_input=str(raw.get("user_input") or ""),
        thread_id=thread,
        elapsed_ms=elapsed_ms,
    )


def pending_state(thread_id: str) -> dict[str, Any] | None:
    """Return the pending approval of a paused thread, if any."""
    snapshot = COMPILED_GRAPH.get_state({"configurable": {"thread_id": str(thread_id)}})
    values = dict(getattr(snapshot, "values", {}) or {})
    interrupts = list(getattr(snapshot, "interrupts", ()) or ())
    for item in interrupts:
        value = getattr(item, "value", None)
        if isinstance(value, Mapping):
            return dict(value)
    for item in values.get("__interrupt__") or ():
        value = getattr(item, "value", None)
        if isinstance(value, Mapping):
            return dict(value)
    return None


__all__ = [
    "CHECKPOINTER",
    "COMPILED_GRAPH",
    "GRAPH_NODE_ORDER",
    "build_graph",
    "collect_tool_payloads",
    "new_thread_id",
    "pending_state",
    "plan_to_tool_calls",
    "resume_workflow",
    "run_workflow",
]

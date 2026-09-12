"""Streamlit page for the V3 "AI 工作流助手".

The page is a pure view.  It hands the live session objects to
:func:`workflow.graph.run_workflow`, renders the returned
:class:`~workflow.state.WorkflowResult`, and — when the graph has paused on a
human-in-the-loop gate — turns the operator's click into an
:class:`~workflow.hitl.ApprovalDecision` for :func:`workflow.graph.resume_workflow`.

No business logic lives here.
"""

from __future__ import annotations

import html
import json
from typing import Any, Mapping

import streamlit as st

from config import SAFETY_DISCLAIMER
from tools import ToolContext, describe_tools

from .graph import resume_workflow, run_workflow
from .hitl import ACTION_APPROVE, ACTION_MODIFY, ACTION_REJECT, operation_label
from .state import STATUS_AWAITING_APPROVAL, TASK_LABELS, TOOL_LABELS
from .summary import describe_tool_result, tool_headline
from .timeline import STATUS_ICONS

RESULT_KEY = "workflow_result"
ERROR_KEY = "workflow_error"
THREAD_KEY = "workflow_thread_id"
MODIFY_KEY = "workflow_modify_open"

# (button label, full task text)
EXAMPLE_TASKS: tuple[tuple[str, str], ...] = (
    ("SDS 查询", "查询该 SDS 中关于 PPE 的要求，并给出证据来源"),
    ("JSA 风险评估", "为 HF 酸洗作业做 JSA 风险评估，可能性4，严重度5"),
    (
        "新增隐患（需审批）",
        "新增一条隐患：配电箱前堆放杂物，风险等级高，责任人张三，整改期限2026-10-01",
    ),
    ("更新隐患（需审批）", "更新隐患 DEMO-HZ-005 的状态为整改中"),
    ("关闭隐患（需审批）", "更新隐患 DEMO-HZ-005 的状态为已关闭"),
    ("降低风险等级（需审批）", "更新隐患 DEMO-HZ-006 的风险等级为中"),
    ("重大风险写操作", "新增一条隐患：反应釜法兰泄漏，风险等级重大，责任人李四"),
    ("化学品不匹配", "查询浓硫酸泄漏的应急处置要求"),
    ("紧急事件", "现场发生大量氢氟酸泄漏，有人员灼伤，怎么处理"),
    ("隐患统计", "现在有多少条隐患未整改？整改完成率是多少？"),
    (
        "组合任务",
        "查询该 SDS 中关于 PPE 的要求，并为该作业做 JSA 风险评估，同时新增一条隐患：现场堆放杂物",
    ),
)

NODE_LABELS = {
    "route": "路由 · 识别任务",
    "plan": "规划 · 生成工具调用",
    "guard": "安全 · 规则与审批",
    "tools": "执行 · 调用工具",
    "screen": "核验 · 证据与风险",
    "summarize": "汇总 · 生成回答",
}

_STATUS_ICON = {"ok": "✅", "warn": "⚠️", "info": "ℹ️"}

_RUN_BADGES: dict[str, tuple[str, str, str]] = {
    "completed": ("✅ 已完成", "#e7f6ec", "#0f8a4a"),
    "awaiting_approval": ("⏳ 等待人工确认", "#fef4e6", "#a86400"),
    "rejected": ("⛔ 已被拒绝，流程停止", "#fdeceb", "#c5221f"),
    "blocked": ("🚫 已被安全规则阻断", "#fdeceb", "#c5221f"),
}

_TIMELINE_COLORS = {
    "done": "#0f8a4a",
    "pending": "#d99400",
    "rejected": "#c5221f",
    "blocked": "#c5221f",
    "warning": "#d99400",
    "info": "#4b5563",
}


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _build_context(
    vector_store: Any,
    jsa_records: list[dict[str, object]],
    hazard_records: list[dict[str, object]],
    loaded_files: tuple[str, ...],
) -> ToolContext:
    return ToolContext(
        vector_store=vector_store,
        jsa_records=jsa_records,
        hazard_records=hazard_records,
        loaded_files=tuple(loaded_files),
    )


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #


def _render_run_badge(result: Mapping[str, Any], pending: Mapping[str, Any] | None) -> None:
    status = str(result.get("status") or "")
    key = "awaiting_approval" if pending else status
    label, background, colour = _RUN_BADGES.get(
        key, ("⏳ 运行中", "#eef2f7", "#374151")
    )
    guard_count = len(result.get("guardrails") or ())
    approval_count = len(result.get("approvals") or ())
    citations = "✅ 证据要素完整" if result.get("citations_ok", True) else "⚠️ 证据不完整"
    st.markdown(
        f'<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">'
        f'<span style="background:{background};color:{colour};padding:4px 10px;'
        f'border-radius:999px;font-size:13px;font-weight:600;">{label}</span>'
        f'<span style="background:#f3f4f6;color:#374151;padding:4px 10px;border-radius:999px;'
        f'font-size:12px;">安全规则命中 {guard_count}</span>'
        f'<span style="background:#f3f4f6;color:#374151;padding:4px 10px;border-radius:999px;'
        f'font-size:12px;">审批记录 {approval_count}</span>'
        f'<span style="background:#f3f4f6;color:#374151;padding:4px 10px;border-radius:999px;'
        f'font-size:12px;">{citations}</span>'
        f"</div>",
        unsafe_allow_html=True,
    )


def _render_timeline(result: Mapping[str, Any]) -> None:
    st.markdown("#### 执行时间线")
    events = list(result.get("timeline") or ())
    if not events:
        st.caption("尚未产生时间线事件。")
        return

    blocks: list[str] = []
    total = len(events)
    for index, item in enumerate(events):
        status = str(item.get("status", ""))
        colour = _TIMELINE_COLORS.get(status, "#4b5563")
        icon = str(item.get("icon") or STATUS_ICONS.get(status, "·"))
        label = html.escape(str(item.get("label", "")))
        detail = html.escape(str(item.get("detail", "")))
        connector = (
            ""
            if index == total - 1
            else '<div style="width:2px;flex:1;min-height:22px;background:#e5e7eb;"></div>'
        )
        blocks.append(
            '<div style="display:flex;gap:10px;align-items:stretch;">'
            '<div style="display:flex;flex-direction:column;align-items:center;width:16px;">'
            f'<div style="width:12px;height:12px;border-radius:50%;background:{colour};'
            f'margin-top:5px;box-shadow:0 0 0 3px {colour}22;flex:0 0 auto;"></div>'
            f"{connector}"
            "</div>"
            '<div style="flex:1;padding-bottom:14px;">'
            f'<div style="font-size:13px;font-weight:600;color:#1f2328;">'
            f"{icon} {html.escape(str(item.get('order', '')))}. {label}</div>"
            f'<div style="font-size:12px;color:#5b6472;line-height:1.55;">{detail}</div>'
            "</div>"
            "</div>"
        )

    st.markdown(
        '<div style="background:#f8f9fb;border:1px solid #e5e7eb;border-radius:10px;'
        'padding:14px 16px 2px 16px;">' + "".join(blocks) + "</div>",
        unsafe_allow_html=True,
    )

    with st.expander("时间线（表格视图 / 原始事件）"):
        st.dataframe(
            [
                {
                    "顺序": item.get("order"),
                    "事件": item.get("label"),
                    "状态": f"{item.get('icon', '')} {item.get('status', '')}",
                    "详情": item.get("detail"),
                    "阶段": NODE_LABELS.get(str(item.get("phase")), item.get("phase")),
                }
                for item in events
            ],
            hide_index=True,
            use_container_width=True,
        )


def _render_guardrail_panel(result: Mapping[str, Any]) -> None:
    findings = list(result.get("guardrails") or ())
    approvals = list(result.get("approvals") or ())
    if not findings and not approvals:
        return
    with st.expander("安全规则（Guardrail）与审批记录", expanded=bool(findings)):
        if findings:
            st.dataframe(
                [
                    {
                        "等级": {
                            "block": "🚫 阻断",
                            "approval": "⏳ 需审批",
                            "notice": "⚠️ 提示",
                        }.get(str(item.get("severity")), item.get("severity")),
                        "规则": item.get("title"),
                        "说明": item.get("detail"),
                    }
                    for item in findings
                ],
                hide_index=True,
                use_container_width=True,
            )
        if approvals:
            st.markdown("**审批记录**")
            st.dataframe(
                [
                    {
                        "操作": item.get("operation_label"),
                        "处置": item.get("action_label"),
                        "方式": "自动放行" if item.get("auto") else "人工",
                        "阶段": "执行前" if item.get("phase") == "plan" else "检索后",
                        "备注": item.get("note"),
                    }
                    for item in approvals
                ],
                hide_index=True,
                use_container_width=True,
            )


def _render_approval_panel(
    result: Mapping[str, Any],
    *,
    context: ToolContext,
) -> None:
    """Render the human-in-the-loop gate and turn clicks into decisions."""
    pending = result.get("pending_approval")
    if not pending:
        return

    thread_id = str(result.get("thread_id") or "")
    gate_id = str(pending.get("gate_id") or "")
    operation = str(pending.get("operation") or "")
    kind = str(pending.get("kind") or "plan")

    st.divider()
    st.error("⏳ **该工作流已暂停，等待人工确认。在获得批准前，相关写操作不会执行。**")
    st.markdown(
        f"**待确认操作：{pending.get('operation_label') or operation_label(operation)}**"
        f"　｜　轮次：第 {pending.get('round', 1)} 轮"
    )
    if pending.get("reason"):
        st.markdown(f"**触发原因**：{pending['reason']}")

    calls = list(pending.get("calls") or ())
    if calls:
        st.markdown("**拟执行内容**")
        st.dataframe(
            [
                {
                    "#": index,
                    "工具": TOOL_LABELS.get(str(item.get("tool")), item.get("tool")),
                    "操作": "、".join(
                        operation_label(str(code)) for code in item.get("operations") or ()
                    ),
                    "参数": json.dumps(
                        item.get("arguments") or {}, ensure_ascii=False, default=str
                    ),
                }
                for index, item in enumerate(calls, start=1)
            ],
            hide_index=True,
            use_container_width=True,
        )

    current = dict(pending.get("current") or {})
    proposed = dict(pending.get("proposed") or {})
    if current or proposed:
        left, right = st.columns(2)
        with left:
            st.markdown("**变更前**")
            st.code(_json_text(current) if current else "（新增记录，无变更前状态）", language="json")
        with right:
            st.markdown("**变更后**")
            st.code(_json_text(proposed) if proposed else "（无）", language="json")

    codes = list(pending.get("guard_codes") or ())
    if codes:
        st.caption("命中规则：" + "、".join(str(code) for code in codes))

    def _submit(decision_payload: dict[str, Any]) -> None:
        try:
            resumed = resume_workflow(thread_id, context, decision_payload)
            st.session_state[RESULT_KEY] = resumed.to_dict()
            st.session_state[ERROR_KEY] = None
        except Exception as exc:  # noqa: BLE001 - surface the failure in the UI
            st.session_state[ERROR_KEY] = f"{type(exc).__name__}: {exc}"
        st.session_state[MODIFY_KEY] = False
        st.rerun()

    columns = st.columns(3)
    if columns[0].button(
        "✅ 批准", type="primary", use_container_width=True, key="wf_approve"
    ):
        _submit(
            {
                "gate_id": gate_id,
                "action": ACTION_APPROVE,
                "round": pending.get("round", 1),
            }
        )
    if columns[1].button("✏️ 修改参数后批准", use_container_width=True, key="wf_modify"):
        st.session_state[MODIFY_KEY] = True
        st.rerun()
    if columns[2].button("⛔ 拒绝", use_container_width=True, key="wf_reject"):
        _submit(
            {
                "gate_id": gate_id,
                "action": ACTION_REJECT,
                "round": pending.get("round", 1),
                "note": "操作员在会话中拒绝了该操作。",
            }
        )

    if st.session_state.get(MODIFY_KEY):
        st.markdown("**修改拟执行参数**")
        if kind == "plan":
            st.caption(
                "只允许调整参数，不允许更换工具。任何会引入新的审批项"
                "（重大风险 / 降低风险等级 / 关闭隐患）的修改都会被按拒绝处理。"
            )
            draft = st.text_area(
                "calls（JSON）",
                value=_json_text(calls),
                height=200,
                key="wf_modify_calls",
            )
            if st.button("用修改后的参数继续", type="primary", key="wf_modify_submit"):
                try:
                    edited = json.loads(draft)
                    if not isinstance(edited, list):
                        raise ValueError("修改内容必须是一个数组。")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"JSON 解析失败：{exc}")
                else:
                    _submit(
                        {
                            "gate_id": gate_id,
                            "action": ACTION_MODIFY,
                            "calls": edited,
                            "round": pending.get("round", 1),
                        }
                    )
        else:
            st.caption("对「证据不足/来源冲突后继续」的确认：可填写备注，说明由谁基于什么依据批准。")
            note = st.text_input("确认备注", key="wf_modify_note")
            if st.button("确认并继续", type="primary", key="wf_modify_submit"):
                _submit(
                    {
                        "gate_id": gate_id,
                        "action": ACTION_MODIFY,
                        "calls": [],
                        "note": note or "EHS 人员确认后在证据不足的情况下继续。",
                        "round": pending.get("round", 1),
                    }
                )
    st.divider()


# --------------------------------------------------------------------------- #
# Result panels
# --------------------------------------------------------------------------- #


def _render_route_panel(result: Mapping[str, Any]) -> None:
    st.markdown("#### 1. 识别出的任务类型")
    if not result.get("task_types"):
        st.warning("未能把这条请求识别为已支持的任务。当前支持：SDS 查询、JSA 风险评估、隐患管理、仪表盘汇总及其组合。")
        return

    st.success(f"**{result.get('route_label', '')}**")
    chips = [TASK_LABELS.get(item, item) for item in result.get("task_types", [])]
    st.markdown("任务类型：" + "　·　".join(f"`{item}`" for item in chips))

    keywords = result.get("matched_keywords") or {}
    if keywords:
        st.caption(
            "命中关键词："
            + "；".join(
                f"{TASK_LABELS.get(task, task)} → {'/'.join(words[:6])}"
                for task, words in keywords.items()
                if words
            )
        )


def _render_plan_panel(result: Mapping[str, Any]) -> None:
    st.markdown("#### 2. 计划调用的工具")
    plan = result.get("plan") or []
    if not plan:
        st.caption("无需调用工具。")
        return
    rows = [
        {
            "#": index,
            "工具": TOOL_LABELS.get(str(item.get("tool")), item.get("tool")),
            "调用参数": json.dumps(
                item.get("arguments") or {}, ensure_ascii=False, default=str
            ),
            "调用原因": item.get("reason", ""),
        }
        for index, item in enumerate(plan, start=1)
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)


def _render_steps_panel(result: Mapping[str, Any]) -> None:
    st.markdown("#### 3. 当前执行步骤")
    steps = result.get("steps") or []
    if not steps:
        st.caption("没有执行步骤。")
        return
    rows = [
        {
            "步骤": item.get("order", index),
            "阶段": NODE_LABELS.get(str(item.get("node")), item.get("node")),
            "状态": _STATUS_ICON.get(str(item.get("status")), "·"),
            "说明": item.get("title", ""),
            "详情": str(item.get("detail", "")).replace("\n", " ／ "),
        }
        for index, item in enumerate(steps, start=1)
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)
    st.caption(
        "引擎：LangGraph（route → plan → guard_plan → tools → screen_evidence → summarize）"
        f"｜耗时 {result.get('elapsed_ms', 0)} ms"
    )


def _render_tool_panel(result: Mapping[str, Any]) -> None:
    st.markdown("#### 4. 工具执行结果")
    tool_results = result.get("tool_results") or []
    if not tool_results:
        st.caption("没有工具被执行。")
        return

    for index, item in enumerate(tool_results, start=1):
        tool = str(item.get("tool", ""))
        label = TOOL_LABELS.get(tool, tool)
        status = str(item.get("status", ""))
        icon = _STATUS_ICON.get("ok" if status == "ok" else "warn", "·")
        with st.expander(f"{index}. {label}｜{icon} {status}", expanded=True):
            st.markdown(describe_tool_result(item))
            if item.get("human_confirmed"):
                st.info("该结论在证据不足/来源冲突的情况下，已由人工确认后继续使用。")
            if item.get("arguments"):
                st.caption("调用参数：" + json.dumps(item["arguments"], ensure_ascii=False, default=str))
            evidence = item.get("evidence") or []
            if evidence:
                st.markdown("**证据来源（文件名 / 页码 / 原文片段）**")
                st.dataframe(
                    [
                        {
                            "排序": row.get("rank"),
                            "文件": row.get("source"),
                            "页码": row.get("page"),
                            "章节": row.get("sections"),
                            "片段": row.get("snippet"),
                        }
                        for row in evidence
                    ],
                    hide_index=True,
                    use_container_width=True,
                )
            with st.popover("查看原始返回（JSON）"):
                st.code(_json_text(item), language="json")
            st.caption(tool_headline(item))


def _render_result(result: dict[str, Any]) -> None:
    st.divider()
    st.markdown("### 工作流执行结果")
    st.caption(f"输入任务：{result.get('user_input', '')}｜线程：{result.get('thread_id', '')}")
    pending = result.get("pending_approval")
    _render_run_badge(result, pending if isinstance(pending, Mapping) else None)
    st.write("")
    _render_route_panel(result)
    _render_plan_panel(result)
    _render_steps_panel(result)
    _render_tool_panel(result)

    st.markdown("#### 5. 最终回答")
    st.markdown(result.get("final_answer", ""))
    if result.get("errors"):
        st.warning("执行中的提示：" + "；".join(result["errors"]))

    _render_guardrail_panel(result)
    _render_timeline(result)

    if st.button("清空结果", use_container_width=False, key="workflow_clear"):
        st.session_state[RESULT_KEY] = None
        st.session_state[ERROR_KEY] = None
        st.session_state[THREAD_KEY] = None
        st.session_state[MODIFY_KEY] = False
        st.rerun()


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


def render_workflow_page(
    *,
    vector_store: Any = None,
    jsa_records: list[dict[str, object]] | None = None,
    hazard_records: list[dict[str, object]] | None = None,
    loaded_files: tuple[str, ...] = (),
) -> None:
    """Render the AI workflow assistant page."""
    st.title("AI 工作流助手")
    st.caption(
        "Unified Task Entry · LangGraph Routing · Tool Calling · "
        "Human-in-the-loop · Guardrails"
    )
    st.info(
        "用一句自然语言描述任务，系统会识别任务类型、规划工具调用并依次执行。"
        "写操作、重大风险、降低风险等级、以及 SDS 证据不足时的继续，都会暂停等待人工确认；"
        "全过程以执行时间线展示。"
    )
    st.warning(
        "本页仍为原型演示：自动生成的 JSA 与隐患记录为草稿，须经人工确认后才可作为正式记录。"
    )

    jsa_records = jsa_records if jsa_records is not None else []
    hazard_records = hazard_records if hazard_records is not None else []

    status_columns = st.columns(3)
    status_columns[0].metric(
        "SDS 知识库", "已就绪" if vector_store is not None else "未构建"
    )
    status_columns[1].metric("会话 JSA 记录", len(jsa_records))
    status_columns[2].metric("会话隐患记录", len(hazard_records))

    st.divider()
    st.markdown("### 统一任务入口")
    text = st.text_area(
        "请用自然语言描述任务",
        height=110,
        key="workflow_input",
        placeholder=(
            "例如：查询该 SDS 中关于 PPE 的要求；或：为 HF 酸洗作业做 JSA 风险评估，可能性4，严重度5"
        ),
    )
    run_clicked = st.button(
        "运行工作流", type="primary", use_container_width=True, key="workflow_run"
    )

    context = _build_context(vector_store, jsa_records, hazard_records, loaded_files)

    submitted: str | None = None
    if run_clicked:
        submitted = (text or "").strip()
        if not submitted:
            st.warning("请先输入任务描述。")
            submitted = None

    st.markdown("##### 示例任务")
    example_columns = st.columns(2)
    for index, (label, task) in enumerate(EXAMPLE_TASKS):
        if example_columns[index % 2].button(
            label,
            key=f"workflow_example_{index}",
            use_container_width=True,
            help=task,
        ):
            submitted = task

    if submitted:
        st.session_state[MODIFY_KEY] = False
        with st.spinner("正在识别任务并执行工作流……"):
            try:
                result = run_workflow(submitted, context, auto_approve=False)
                st.session_state[RESULT_KEY] = result.to_dict()
                st.session_state[THREAD_KEY] = result.thread_id
                st.session_state[ERROR_KEY] = None
            except Exception as exc:  # noqa: BLE001 - surface the error in the UI
                st.session_state[RESULT_KEY] = None
                st.session_state[ERROR_KEY] = f"{type(exc).__name__}: {exc}"

    error = st.session_state.get(ERROR_KEY)
    if error:
        st.error(f"工作流执行失败：{error}")

    result = st.session_state.get(RESULT_KEY)
    if result:
        _render_approval_panel(result, context=context)
        _render_result(result)
    else:
        st.caption("尚未执行任务。")

    with st.expander("可用工具与复用的现有能力"):
        st.dataframe(
            [
                {
                    "工具": f"{item['name']}（{item['label']}）",
                    "作用": item["description"],
                    "复用的现有函数": item["reuses"],
                }
                for item in describe_tools()
            ],
            hide_index=True,
            use_container_width=True,
        )

    st.divider()
    st.caption(SAFETY_DISCLAIMER)


__all__ = ["EXAMPLE_TASKS", "render_workflow_page"]

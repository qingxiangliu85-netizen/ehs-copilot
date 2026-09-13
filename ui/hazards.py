"""隐患与整改 — hazard list and hazard detail.

The detail page makes the separation of duty explicit: finishing the
rectification is not the same as the EHS verification that closes the hazard.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Mapping, Sequence

import streamlit as st

from services import hazard_service, permit_service, persona_service
from workflow import audit, hazard_state, roles, sla
from workflow import actions as workflow_actions

from . import commands, common, product, widgets, worklist

PENDING_KEY = "_hazard_pending"

NEXT_ACTION_LABELS: dict[str, str] = {
    hazard_state.HAZARD_OPEN: "待指派整改负责人与整改期限",
    hazard_state.HAZARD_ASSIGNED: "待整改：按整改措施执行并留存证据",
    hazard_state.HAZARD_IN_PROGRESS: "待提交整改证据",
    hazard_state.HAZARD_VERIFICATION_PENDING: "待EHS验证",
    hazard_state.HAZARD_REOPENED: "已退回：待重新指派并整改",
    hazard_state.HAZARD_CLOSED: "已关闭",
}

TASK_TEXT: dict[str, str] = {
    "assign": "指派整改负责人与整改期限",
    "start": "开始整改",
    "submit_rectification": "提交整改证据并送 EHS 验证",
    "verify_pass": "验证整改有效性并关闭隐患",
    "verify_fail": "退回整改并说明原因",
    "reopen": "重新打开隐患并说明原因",
}


def hazard_next_action(hazard: Mapping[str, Any]) -> str:
    """Return the business description of a hazard's next step."""
    return NEXT_ACTION_LABELS.get(str(hazard.get("status", "")), "—")


def hazard_is_overdue(
    hazard: Mapping[str, Any], moment: datetime | None = None
) -> bool:
    """Return whether a hazard has blown its rectification or verification clock."""
    moment = moment or common.now()
    if str(hazard.get("status", "")) == hazard_state.HAZARD_VERIFICATION_PENDING:
        return sla.is_overdue(hazard.get("verification_due_at"), moment)
    if str(hazard.get("status", "")) == hazard_state.HAZARD_CLOSED:
        return False
    return sla.is_overdue(hazard.get("due_at"), moment)


def _row(
    connection: sqlite3.Connection, hazard: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "隐患编号": str(hazard.get("id", "")),
        "来源Permit": str(hazard.get("permit_id", "")) or "—",
        "描述": str(hazard.get("title", "")),
        "风险": common.risk_label(hazard.get("risk_level")),
        "整改负责人": common.user_name(connection, str(hazard.get("owner_id", ""))),
        "截止日期": common.short_date(hazard.get("due_at")),
        "状态": common.hazard_status_label(hazard.get("status")),
        "是否逾期": "是" if hazard_is_overdue(hazard) else "否",
        "下一动作": hazard_next_action(hazard),
    }


def _events(connection: sqlite3.Connection, hazard_id: str) -> list[dict[str, Any]]:
    return audit.list_events(
        connection, entity_type=audit.ENTITY_HAZARD, entity_id=hazard_id, limit=500
    )


# --------------------------------------------------------------------------- #
# List page
# --------------------------------------------------------------------------- #


def render_list(connection: sqlite3.Connection, user: Mapping[str, Any]) -> None:
    """Render the 隐患与整改 list page."""
    st.title("隐患与整改")
    st.caption("隐患来自作业执行过程；整改完成不等于隐患关闭，必须经 EHS 验证")

    hazards = hazard_service.list_hazards(connection, limit=500)
    if not hazards:
        widgets.empty_state("暂无隐患记录。")
        return

    status_options = ["全部"] + [
        common.hazard_status_label(status) for status in hazard_state.HAZARD_STATUSES
    ]
    owner_ids = sorted(
        {str(item.get("owner_id", "")) for item in hazards if item.get("owner_id")}
    )
    owner_labels = {value: common.user_name(connection, value) for value in owner_ids}
    permit_ids = sorted(
        {str(item.get("permit_id", "")) for item in hazards if item.get("permit_id")}
    )

    filters = st.columns([2.2, 1.4, 1.2, 1.6])
    keyword = filters[0].text_input("搜索", placeholder="搜索隐患编号、描述或来源作业")
    status_choice = filters[1].selectbox("状态筛选", status_options)
    risk_choice = filters[2].selectbox("风险筛选", ["全部"] + list(common.RISK_LEVELS))
    owner_choice = filters[3].selectbox(
        "整改负责人筛选",
        ["全部"] + owner_ids,
        format_func=lambda value: (
            "全部" if value == "全部" else owner_labels.get(value, value)
        ),
    )

    text = str(keyword or "").strip().lower()
    visible = []
    for hazard in hazards:
        if status_choice != "全部" and common.hazard_status_label(
            hazard.get("status")
        ) != status_choice:
            continue
        if risk_choice != "全部" and str(hazard.get("risk_level", "")) != risk_choice:
            continue
        if owner_choice != "全部" and str(hazard.get("owner_id", "")) != owner_choice:
            continue
        if text:
            blob = " ".join(
                [
                    str(hazard.get("id", "")),
                    str(hazard.get("title", "")),
                    str(hazard.get("permit_id", "")),
                ]
            ).lower()
            if text not in blob:
                continue
        visible.append(hazard)

    st.caption(f"共 {len(visible)} 条隐患（全部 {len(hazards)} 条）")
    if not visible:
        widgets.empty_state("没有符合条件的隐患，请调整筛选条件。")
        return

    rows = [_row(connection, hazard) for hazard in visible]
    event = st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="hazard_table",
    )
    selected = list(getattr(getattr(event, "selection", None), "rows", []) or [])
    if selected:
        index = int(selected[0])
        if 0 <= index < len(visible):
            hazard_id = str(visible[index].get("id", ""))
            st.button(
                f"打开 {hazard_id} 详情",
                type="primary",
                key=f"open_hazard_{hazard_id}",
                on_click=common.open_detail,
                args=(common.PAGE_HAZARDS, "hazard", hazard_id),
            )
    else:
        st.caption("在上表中选中一行，即可打开该隐患详情。")

    if permit_ids:
        st.caption("来源作业许可：" + "、".join(permit_ids))


# --------------------------------------------------------------------------- #
# Detail page
# --------------------------------------------------------------------------- #


def _render_header(connection: sqlite3.Connection, hazard: Mapping[str, Any]) -> None:
    st.title(f"{hazard.get('id', '')} · {hazard.get('title', '')}")
    chips: list[tuple[str, tuple[str, str] | None]] = [
        widgets.risk_chip(hazard.get("risk_level")),
        widgets.hazard_status_chip(hazard.get("status")),
    ]
    overdue = widgets.overdue_chip(hazard_is_overdue(hazard))
    if overdue is not None:
        chips.append(overdue)
    widgets.render_chips(chips)

    permit_id = str(hazard.get("permit_id", ""))
    permit = permit_service.get_permit(connection, permit_id) if permit_id else None
    widgets.key_values(
        [
            ("隐患编号", hazard.get("id", "")),
            ("风险等级", common.risk_label(hazard.get("risk_level"))),
            ("状态", common.hazard_status_label(hazard.get("status"))),
            ("来源作业许可", permit_id or "—"),
            ("来源作业名称", (permit or {}).get("title", "—")),
            ("整改截止日期", common.short_date(hazard.get("due_at"))),
            ("验证截止日期", common.short_date(hazard.get("verification_due_at"))),
            ("隐患类型", hazard.get("hazard_type", "") or "—"),
            ("数据性质", hazard.get("data_label", "") or "—"),
        ]
    )

    st.markdown("**责任角色**")
    roles = (
        ("发现人", hazard.get("reported_by_id", "")),
        ("整改负责人", hazard.get("owner_id", "")),
        ("EHS验证人", hazard.get("verifier_id", "")),
    )
    columns = st.columns(len(roles))
    for index, (label, value) in enumerate(roles):
        with columns[index]:
            st.markdown(label)
            st.caption(common.user_name(connection, str(value or "")))
    if permit is not None:
        st.button(
            f"打开来源作业许可 {permit_id}",
            key=f"open_permit_from_hazard_{hazard.get('id', '')}",
            on_click=common.open_detail,
            args=(common.PAGE_PERMITS, "permit", permit_id),
        )


def _render_pending_form(
    connection: sqlite3.Connection,
    hazard: Mapping[str, Any],
    user: Mapping[str, Any],
    code: str,
) -> None:
    hazard_id = str(hazard.get("id", ""))
    st.markdown("---")
    heading = TASK_TEXT.get(code, code)

    if code == "assign":
        st.markdown(f"**{heading}**")
        owners = [
            item
            for item in persona_service.list_users(connection)
            if str(item.get("role", ""))
            in (roles.ROLE_ACTION_OWNER, roles.ROLE_DEMO_ADMIN)
        ]
        owner_id = st.selectbox(
            "整改负责人",
            [str(item["id"]) for item in owners],
            format_func=lambda value: common.user_name(connection, value),
            key=f"assign_owner_{hazard_id}",
        )
        due = st.date_input("整改期限", key=f"assign_due_{hazard_id}")
        actions = st.columns([1, 1])
        if actions[0].button("确认指派", type="primary", key=f"assign_ok_{hazard_id}"):
            ok, message = commands.execute(
                connection,
                "hazard",
                hazard_id,
                code,
                user=user,
                owner_id=owner_id,
                due_at=due.isoformat(),
            )
            st.session_state.pop(PENDING_KEY, None)
            common.set_flash(
                message if ok else f"操作未完成：{message}",
                "success" if ok else "error",
            )
            st.rerun()
        if actions[1].button("取消", key=f"assign_cancel_{hazard_id}"):
            st.session_state.pop(PENDING_KEY, None)
            st.rerun()
        return

    if code == "submit_rectification":
        with st.form(f"rectify_{hazard_id}"):
            st.markdown(f"**{heading}**")
            notes = st.text_area(
                "整改说明",
                key=f"rectify_notes_{hazard_id}",
                placeholder="说明已完成的整改工作；整改完成不等于隐患关闭。",
                height=90,
            )
            st.markdown("**整改证据（至少 1 条）**")
            file_name = st.text_input(
                "证据文件名",
                value="（占位）整改证据（模拟）.pdf",
                key=f"rectify_file_{hazard_id}",
            )
            second = st.columns(2)
            evidence_type = second[0].selectbox(
                "证据类型", ["照片", "记录", "报告", "其他"], key=f"rectify_type_{hazard_id}"
            )
            evidence_note = second[1].text_input(
                "证据说明", key=f"rectify_note_{hazard_id}"
            )
            confirm = st.form_submit_button("提交整改证据", type="primary")
            cancel = st.form_submit_button("取消")
        if cancel:
            st.session_state.pop(PENDING_KEY, None)
            st.rerun()
        if confirm:
            if not str(notes or "").strip():
                st.error("请填写整改说明。")
                return
            ok, message = commands.execute(
                connection,
                "hazard",
                hazard_id,
                code,
                user=user,
                reason=notes,
                evidence=[
                    {
                        "file_name": file_name,
                        "evidence_type": evidence_type,
                        "note": evidence_note,
                    }
                ],
            )
            st.session_state.pop(PENDING_KEY, None)
            common.set_flash(
                message if ok else f"操作未完成：{message}",
                "success" if ok else "error",
            )
            st.rerun()
        return

    label = "验证说明" if code == "verify_pass" else "原因 / 验证说明"
    with st.form(f"pending_{hazard_id}_{code}"):
        st.markdown(f"**{heading}**")
        reason = widgets.reason_input(label=label, key=f"hreason_{hazard_id}_{code}")
        confirm = st.form_submit_button("确认执行", type="primary")
        cancel = st.form_submit_button("取消")
    if cancel:
        st.session_state.pop(PENDING_KEY, None)
        st.rerun()
    if confirm:
        if not str(reason or "").strip():
            st.error("该操作必须填写说明或原因。")
            return
        ok, message = commands.execute(
            connection, "hazard", hazard_id, code, user=user, reason=reason
        )
        st.session_state.pop(PENDING_KEY, None)
        common.set_flash(
            message if ok else f"操作未完成：{message}", "success" if ok else "error"
        )
        st.rerun()


def _render_action_area(
    connection: sqlite3.Connection,
    hazard: Mapping[str, Any],
    user: Mapping[str, Any],
) -> None:
    hazard_id = str(hazard.get("id", ""))
    actions = worklist.actions_for("hazard", hazard, user)
    primary = workflow_actions.primary_action(actions)

    with st.container(border=True):
        st.markdown("#### 当前需要你做什么")
        st.caption(
            f"当前身份：{common.persona_label(user)}　｜　"
            f"当前状态：{common.hazard_status_label(hazard.get('status'))}"
        )
        if primary is None:
            st.info("当前身份在这一状态下没有可执行的动作。")
            return
        st.markdown(f"**当前任务：**　{TASK_TEXT.get(primary.code, primary.label)}")
        if str(hazard.get("status", "")) == hazard_state.HAZARD_VERIFICATION_PENDING:
            st.caption(
                "整改负责人已提交整改证据；是否关闭必须由 EHS 审核人验证后决定。"
            )
        if str(hazard.get("status", "")) == hazard_state.HAZARD_IN_PROGRESS:
            st.caption("提交整改后进入「待验证」，不会自动关闭。")

        pending = st.session_state.get(PENDING_KEY) or {}
        if pending.get("id") == hazard_id:
            _render_pending_form(connection, hazard, user, str(pending.get("code", "")))
            return
        clicked = widgets.render_actions(actions, key_prefix=f"hazard_{hazard_id}")
        if clicked is None:
            return
        action = next((item for item in actions if item.code == clicked), None)
        if action is None:
            return
        # ``verify_pass`` also collects a 验证说明: the state machine refuses a
        # pass without one, so it must go through the reason form (never a dead
        # direct click).  ``assign`` and ``submit_rectification`` have their own
        # dedicated forms in ``_render_pending_form``.
        if action.requires_reason or action.code in (
            "assign",
            "submit_rectification",
            "verify_pass",
        ):
            st.session_state[PENDING_KEY] = {"id": hazard_id, "code": action.code}
            st.rerun()
        ok, message = commands.execute(
            connection, "hazard", hazard_id, action.code, user=user
        )
        common.set_flash(
            message if ok else f"操作未完成：{message}", "success" if ok else "error"
        )
        st.rerun()


def _render_body(connection: sqlite3.Connection, hazard: Mapping[str, Any]) -> None:
    actions = list(hazard.get("corrective_actions") or ())
    evidence = list(hazard.get("evidence") or ())

    with st.expander("整改措施", expanded=True):
        if actions:
            st.dataframe(
                [
                    {
                        "整改措施": item.get("action_text", ""),
                        "负责人": common.user_name(
                            connection, str(item.get("owner_id", ""))
                        ),
                        "期限": common.short_date(item.get("due_at")),
                        "状态": {
                            "planned": "计划中",
                            "in_progress": "执行中",
                            "completed": "已完成",
                        }.get(str(item.get("status", "")), str(item.get("status", ""))),
                    }
                    for item in actions
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("尚未登记整改措施。")

    with st.expander("整改证据", expanded=False):
        if evidence:
            st.dataframe(
                [
                    {
                        "文件": item.get("file_name", ""),
                        "类型": item.get("evidence_type", ""),
                        "说明": item.get("note", "") or "—",
                        "上传人": common.user_name(
                            connection, str(item.get("uploaded_by", ""))
                        ),
                        "上传时间": common.short_datetime(item.get("uploaded_at")),
                    }
                    for item in evidence
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("尚未提交整改证据。")

    with st.expander("EHS 验证记录", expanded=False):
        if str(hazard.get("verification_result", "")).strip():
            st.markdown(
                "**验证结论：**　"
                + ("验证通过" if hazard.get("verification_result") == "pass" else "退回整改")
            )
            st.markdown(f"**验证人：**　{common.user_name(connection, str(hazard.get('verifier_id', '')))}")
            st.markdown(f"**验证时间：**　{common.short_datetime(hazard.get('verified_at'))}")
            st.markdown(f"**验证说明：**　{hazard.get('verification_notes', '') or '—'}")
        else:
            st.caption("尚未完成 EHS 验证。整改完成不等于隐患关闭。")
        if hazard.get("reopened_reason"):
            st.warning(f"退回原因：{hazard.get('reopened_reason')}")

    with st.expander("操作记录（Activity Timeline）", expanded=False):
        widgets.render_activity_timeline(_events(connection, str(hazard.get("id", ""))))


def _render_flow_position(
    connection: sqlite3.Connection, hazard: Mapping[str, Any]
) -> None:
    """Show where this hazard sits in the permit execution loop."""
    st.markdown("#### 这条隐患在作业流程中的位置")
    st.markdown(product.subflow_html(), unsafe_allow_html=True)
    st.caption(product.HAZARD_SUBFLOW_CAPTION)

    permit_id = str(hazard.get("permit_id", ""))
    permit = permit_service.get_permit(connection, permit_id) if permit_id else None
    if permit is None:
        st.caption("该隐患尚未关联作业许可（可来自日常巡检）。")
        return
    st.markdown(
        f"来源作业许可：**{permit_id}**　{permit.get('title', '')}　｜　"
        f"作业类型：{product.work_type_label(permit)}　｜　"
        f"作业当前状态：{common.permit_status_label(permit.get('status'))}"
    )


def render_detail(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    hazard_id: str,
) -> None:
    """Render one hazard detail page."""
    hazard = hazard_service.get_hazard(connection, hazard_id)
    st.button(
        "← 返回隐患列表",
        on_click=common.close_detail,
        args=(common.PAGE_HAZARDS,),
    )
    if hazard is None:
        st.error(f"未找到隐患 {hazard_id}。")
        return

    _render_header(connection, hazard)
    st.divider()
    _render_action_area(connection, hazard, user)
    st.divider()
    _render_flow_position(connection, hazard)
    st.divider()
    _render_body(connection, hazard)


__all__ = [
    "NEXT_ACTION_LABELS",
    "TASK_TEXT",
    "hazard_is_overdue",
    "hazard_next_action",
    "render_detail",
    "render_list",
]

"""今日工作台 — first explains the product, then answers "what must I handle today?".

The screen is deliberately shallow, in four parts:

1. what this product is + the 「体验3分钟完整流程」 golden Demo entry;
2. 「我的待办」 — four headline numbers and one filtered list;
3. a short 风险 / 逾期 reminder list;
4. the most recent activity (optional).

The full management dashboard stays on 风险看板.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

import streamlit as st

from services import hazard_service, permit_service
from workflow import audit, hazard_state

from . import common, product, widgets, worklist

MAX_ALERTS = 5
MAX_ACTIVITY = 6

_RISK_RANK = {"重大": 0, "高": 1, "中": 2, "低": 3}


def _render_summary(rows: list[dict[str, Any]]) -> None:
    summary = worklist.worklist_summary(rows)
    columns = st.columns(4)
    columns[0].metric("我的待办", summary["total"])
    columns[1].metric("已逾期", summary["overdue"])
    columns[2].metric("今日到期", summary["due_today"])
    columns[3].metric("高/重大风险", summary["high_risk"])


def _render_card(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    index: int,
) -> None:
    primary = row.get("primary")
    with st.container(border=True):
        chips: list[tuple[str, tuple[str, str] | None]] = [
            widgets.risk_chip(row.get("risk_level")),
            widgets.neutral_chip(row.get("action_label", "")),
        ]
        overdue = widgets.overdue_chip(bool(row.get("is_overdue")))
        if overdue is not None:
            chips.append(overdue)
        widgets.render_chips(chips)

        st.markdown(f"**{row.get('entity_id', '')} · {row.get('title', '')}**")
        st.caption(str(row.get("reason", "")))

        owner = common.user_name(connection, str(row.get("assigned_to", "")))
        due = common.short_datetime(row.get("due_at"))
        status = worklist.status_label_for_row(connection, row)
        st.markdown(
            f"负责人：{owner}　｜　截止时间：{due}　｜　当前状态：{status}"
        )

        target = str(row.get("target_page", common.PAGE_TODAY))
        entity_type = str(row.get("entity_type", ""))
        entity_id = str(row.get("entity_id", ""))
        label = getattr(primary, "label", "") or "打开处理"
        columns = st.columns([1.4, 3.6])
        columns[0].button(
            label,
            key=f"today_open_{index}_{entity_id}_{row.get('action_type', '')}",
            type="primary",
            width="stretch",
            on_click=common.open_detail,
            args=(target, entity_type, entity_id),
            help="进入详情页完成该操作；提交时系统会再次校验权限与状态。",
        )
        columns[1].caption("点击进入详情页，在「当前需要你做什么」中执行。")


def _priority_items(
    connection: sqlite3.Connection, limit: int = MAX_ALERTS
) -> list[dict[str, Any]]:
    """Return the few items the whole site should worry about first."""
    moment = common.now()
    items: list[dict[str, Any]] = []

    hazards = hazard_service.list_hazards(connection, limit=500)
    open_hazards = [
        hazard
        for hazard in hazards
        if str(hazard.get("status", "")) != hazard_state.HAZARD_CLOSED
    ]
    open_hazards.sort(
        key=lambda hazard: (
            0 if common.is_overdue(hazard.get("due_at"), moment) else 1,
            _RISK_RANK.get(str(hazard.get("risk_level", "")).strip(), 9),
            str(hazard.get("due_at", "") or "9999"),
            str(hazard.get("id", "")),
        )
    )
    for hazard in open_hazards:
        if not common.is_high_risk(hazard.get("risk_level")):
            continue
        items.append(
            {
                "类型": "隐患",
                "编号": str(hazard.get("id", "")),
                "事项": str(hazard.get("title", "")),
                "风险": common.risk_label(hazard.get("risk_level")),
                "截止": common.short_date(hazard.get("due_at")),
                "状态": common.hazard_status_label(hazard.get("status")),
                "是否逾期": "是" if common.is_overdue(hazard.get("due_at"), moment) else "否",
            }
        )
        if len(items) >= limit:
            return items

    for permit in permit_service.list_permits(connection, limit=500):
        status = str(permit.get("status", ""))
        if status not in common.ACTIVE_PERMIT_STATUSES:
            continue
        if not common.is_overdue(permit.get("valid_to"), moment):
            continue
        items.append(
            {
                "类型": "作业许可",
                "编号": str(permit.get("id", "")),
                "事项": str(permit.get("title", "")),
                "风险": common.risk_label(
                    permit.get("residual_risk_level", "") or permit.get("risk_level", "")
                ),
                "截止": common.short_date(permit.get("valid_to")),
                "状态": common.permit_status_label(status),
                "是否逾期": "是",
            }
        )
        if len(items) >= limit:
            return items
    return items


def _render_alerts(connection: sqlite3.Connection) -> None:
    st.markdown("#### 需要优先关注")
    items = _priority_items(connection)
    if not items:
        st.caption("当前没有逾期或高风险的未关闭事项。")
        return
    st.caption(f"高/重大未关闭隐患与逾期作业许可（最多 {MAX_ALERTS} 条）")
    st.dataframe(
        items,
        hide_index=True,
        width="stretch",
    )


def _render_recent_activity(connection: sqlite3.Connection) -> None:
    st.markdown("#### 最近活动")
    events = list(
        reversed(audit.list_events(connection, limit=MAX_ACTIVITY))
    )
    if not events:
        st.caption("暂无操作记录。")
        return
    st.dataframe(
        [
            {
                "时间": common.short_datetime(event.get("created_at")),
                "操作者": event.get("actor", "") or "system",
                "实体": (
                    f"{widgets.ENTITY_LABELS.get(str(event.get('entity_type', '')), '')} "
                    f"{event.get('entity_id', '')}"
                ),
                "动作": widgets.AUDIT_ACTION_LABELS.get(
                    str(event.get("action", "")), str(event.get("action", ""))
                ),
            }
            for event in events
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption("完整审计记录见「资料与审计」。")


def render(connection: sqlite3.Connection, user: Mapping[str, Any]) -> None:
    """Render the 今日工作台 page."""
    st.title("今日工作台")

    product.render_hero()
    st.divider()

    rows = worklist.build_worklist(connection, user)
    _render_summary(rows)

    st.divider()
    choice = st.radio(
        "待办筛选",
        worklist.WORKLIST_FILTERS,
        horizontal=True,
        key="today_filter",
        label_visibility="collapsed",
    )

    filtered = worklist.filter_worklist(rows, choice)
    st.markdown(f"#### 我的待办（{len(filtered)}）")

    if not filtered:
        if not rows:
            st.info(
                f"{common.persona_label(user)} 当前没有待处理事项。"
                "可切换左侧的 Demo 身份查看其他角色的待办。"
            )
        else:
            st.info("该筛选条件下没有待办事项。")
    else:
        for index, row in enumerate(filtered):
            _render_card(connection, row, index=index)

    st.divider()
    _render_alerts(connection)

    st.divider()
    _render_recent_activity(connection)


__all__ = ["render"]

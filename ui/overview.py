"""风险看板 — the six questions a manager actually asks, and nothing decorative.

1. 当前正在执行多少作业许可？
2. 有多少待审批？
3. 哪些作业许可即将到期？
4. 哪些高/重大风险作业仍未关闭？
5. 哪些隐患已经逾期？
6. 哪些隐患等待 EHS 验证？

Every number is derived from the store on each render.  Charts that did not
answer one of these questions have been removed rather than kept for looks.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

import streamlit as st

from services import hazard_service, permit_service
from workflow import hazard_state, permit_state

from . import common, product


def _permit_risk(permit: Mapping[str, Any]) -> str:
    return str(
        permit.get("residual_risk_level", "") or permit.get("risk_level", "") or ""
    )


def kpi_values(
    connection: sqlite3.Connection, moment: Any = None
) -> dict[str, int]:
    """Return the headline numbers, all derived from the store."""
    stamp = moment or common.now()
    permits = permit_service.list_permits(connection, limit=1000)
    hazards = hazard_service.list_hazards(connection, limit=1000)

    active = sum(
        1 for permit in permits if str(permit.get("status", "")) == permit_state.PERMIT_ACTIVE
    )
    suspended = sum(
        1
        for permit in permits
        if str(permit.get("status", "")) == permit_state.PERMIT_SUSPENDED
    )
    pending_approval = sum(
        1
        for permit in permits
        if str(permit.get("status", "")) == permit_state.PERMIT_APPROVAL_PENDING
    )
    expiring = sum(
        1
        for permit in permits
        if str(permit.get("status", "")) not in permit_state.PERMIT_TERMINAL_STATUSES
        and common.permit_validity(permit, stamp)["is_expiring"]
    )
    high_open_permits = sum(
        1
        for permit in permits
        if str(permit.get("status", "")) not in permit_state.PERMIT_TERMINAL_STATUSES
        and common.is_high_risk(_permit_risk(permit))
    )
    overdue_hazards = sum(
        1
        for hazard in hazards
        if str(hazard.get("status", "")) != hazard_state.HAZARD_CLOSED
        and common.is_overdue(hazard.get("due_at"), stamp)
    )
    pending_verification = sum(
        1
        for hazard in hazards
        if str(hazard.get("status", "")) == hazard_state.HAZARD_VERIFICATION_PENDING
    )
    high_open_hazards = sum(
        1
        for hazard in hazards
        if str(hazard.get("status", "")) != hazard_state.HAZARD_CLOSED
        and common.is_high_risk(hazard.get("risk_level"))
    )
    return {
        "active_permits": active,
        "suspended_permits": suspended,
        "pending_approval": pending_approval,
        "expiring_permits": expiring,
        "high_open_permits": high_open_permits,
        "overdue_rectification": overdue_hazards,
        "pending_verification": pending_verification,
        "high_open_hazards": high_open_hazards,
    }


def _table(rows: list[dict[str, Any]], empty: str) -> None:
    """Render one answer table, or an explicit empty state."""
    if not rows:
        st.caption(empty)
        return
    st.dataframe(rows, hide_index=True, width="stretch")


def _expiring_permits(
    connection: sqlite3.Connection, moment: Any
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for permit in permit_service.list_permits(connection, limit=1000):
        if str(permit.get("status", "")) in permit_state.PERMIT_TERMINAL_STATUSES:
            continue
        validity = common.permit_validity(permit, moment)
        if not (validity["is_expiring"] or validity["is_expired"]):
            continue
        rows.append(
            {
                "编号": str(permit.get("id", "")),
                "作业名称": str(permit.get("title", "")),
                "作业类型": product.work_type_label(permit),
                "有效期至": validity["to_text"],
                "有效期状态": validity["label"],
                "作业负责人": common.user_name(
                    connection, str(permit.get("owner_id", ""))
                ),
            }
        )
    return rows


def _high_open_permits(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for permit in permit_service.list_permits(connection, limit=1000):
        if str(permit.get("status", "")) in permit_state.PERMIT_TERMINAL_STATUSES:
            continue
        risk = _permit_risk(permit)
        if not common.is_high_risk(risk):
            continue
        rows.append(
            {
                "编号": str(permit.get("id", "")),
                "作业名称": str(permit.get("title", "")),
                "作业类型": product.work_type_label(permit),
                "风险": common.risk_label(risk),
                "状态": common.permit_status_label(permit.get("status")),
                "当前待办": _next_action_text(permit),
            }
        )
    return rows


def _next_action_text(permit: Mapping[str, Any]) -> str:
    from . import permits as permits_page

    return permits_page.permit_next_action(permit)


def _hazard_rows(connection: sqlite3.Connection, moment: Any) -> tuple[list, list]:
    overdue: list[dict[str, Any]] = []
    verifying: list[dict[str, Any]] = []
    for hazard in hazard_service.list_hazards(connection, limit=1000):
        status = str(hazard.get("status", ""))
        if status != hazard_state.HAZARD_CLOSED and common.is_overdue(
            hazard.get("due_at"), moment
        ):
            overdue.append(
                {
                    "隐患编号": str(hazard.get("id", "")),
                    "描述": str(hazard.get("title", "")),
                    "风险": common.risk_label(hazard.get("risk_level")),
                    "整改负责人": common.user_name(
                        connection, str(hazard.get("owner_id", ""))
                    ),
                    "整改截止": common.short_date(hazard.get("due_at")),
                    "状态": common.hazard_status_label(status),
                }
            )
        if status == hazard_state.HAZARD_VERIFICATION_PENDING:
            verifying.append(
                {
                    "隐患编号": str(hazard.get("id", "")),
                    "描述": str(hazard.get("title", "")),
                    "风险": common.risk_label(hazard.get("risk_level")),
                    "验证截止": common.short_date(hazard.get("verification_due_at")),
                    "来源作业许可": str(hazard.get("permit_id", "")) or "—",
                }
            )
    return overdue, verifying


def render(connection: sqlite3.Connection, user: Mapping[str, Any]) -> None:
    """Render the 风险看板 page."""
    st.title("风险看板")
    st.caption("只回答管理者需要立刻知道的数量与清单")

    moment = common.now()
    values = kpi_values(connection, moment)
    columns = st.columns(6)
    columns[0].metric("执行中许可", values["active_permits"])
    columns[1].metric("待审批", values["pending_approval"])
    columns[2].metric("即将到期许可", values["expiring_permits"])
    columns[3].metric("高/重大未关闭许可", values["high_open_permits"])
    columns[4].metric("逾期隐患", values["overdue_rectification"])
    columns[5].metric("待EHS验证隐患", values["pending_verification"])

    if values["suspended_permits"]:
        st.caption(f"另有 {values['suspended_permits']} 个作业许可处于「已暂停」。")
    if values["high_open_hazards"]:
        st.caption(
            f"高/重大风险且未关闭的隐患共 {values['high_open_hazards']} 条（见左侧「逾期隐患」清单）。"
        )

    st.divider()

    overdue_hazards, verifying_hazards = _hazard_rows(connection, moment)

    left, right = st.columns(2)
    with left:
        st.markdown("#### 即将到期 / 已过期的作业许可")
        st.caption("时间维度：许可有效期，与风险等级无关。")
        _table(
            _expiring_permits(connection, moment),
            "当前没有即将到期或已过期的作业许可。",
        )

        st.markdown("#### 高/重大风险未关闭的作业许可")
        _table(_high_open_permits(connection), "当前没有高/重大风险未关闭的作业许可。")

    with right:
        st.markdown("#### 已逾期的隐患")
        st.caption("时间维度：整改截止日期已过，且隐患尚未关闭。")
        _table(overdue_hazards, "当前没有逾期的隐患。")

        st.markdown("#### 等待 EHS 验证的隐患")
        st.caption("整改已提交，是否关闭必须由 EHS 验证后决定。")
        _table(verifying_hazards, "当前没有等待 EHS 验证的隐患。")


__all__ = ["kpi_values", "render"]

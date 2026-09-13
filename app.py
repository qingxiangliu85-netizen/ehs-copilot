"""Streamlit entry point for the EHS Copilot V5 product UI (P0C / P0C.2).

Product positioning (one place, see ``ui.product``):

    EHS Copilot — 高风险非例行作业许可与隐患闭环工作台
    从作业申请到整改关闭，一张许可单管理高风险作业全过程。

Five first-level pages, no technical vocabulary:

    今日工作台 · 作业许可 · 隐患与整改 · 风险看板 · 资料与审计

The engine underneath (workflow graph, router, tool calling, guardrails) is
unchanged and still runs, but it is not part of the business navigation.  The
previous multi-page console — including the standalone SDS library, the JSA
tool and the AI workflow console — is preserved and runnable as
``streamlit run legacy_console.py``.

All state changes go through ``services.*``; the pages only render and forward
clicks, so the permission layer and the state machines remain the only gates.
"""

from __future__ import annotations

from typing import Any, Mapping

import streamlit as st

from ui import common, hazards, overview, permits, product, records, today

st.set_page_config(
    page_title=f"{product.PRODUCT_NAME}｜{product.PRODUCT_CATEGORY}",
    page_icon="🦺",
    layout="wide",
)


def _sidebar(connection: Any) -> str:
    """Render the identity switcher and the five-item navigation."""
    personas = common.list_personas(connection)
    options = [str(user["id"]) for user in personas]
    labels = {str(user["id"]): common.persona_label(user) for user in personas}
    default_index = (
        options.index(common.DEFAULT_USER_ID)
        if common.DEFAULT_USER_ID in options
        else 0
    )

    with st.sidebar:
        st.subheader(product.PRODUCT_NAME)
        st.caption(product.PRODUCT_CATEGORY)
        st.caption(product.PRODUCT_ONE_LINER)

        st.markdown("**当前身份（Demo）**")
        st.selectbox(
            "当前身份（Demo）",
            options,
            index=default_index,
            format_func=lambda value: labels.get(value, value),
            key=common.USER_KEY,
            label_visibility="collapsed",
        )
        st.caption(common.DEMO_IDENTITY_NOTICE)

        st.divider()
        page = st.radio(
            "功能导航",
            common.NAV_PAGES,
            key=common.NAV_KEY,
            label_visibility="collapsed",
        )

        st.divider()
        st.caption(common.DEMO_NOTICE)
    return str(page)


def _render_page(
    page: str, connection: Any, user: Mapping[str, Any]
) -> None:
    """Dispatch to the five business pages, honouring an open detail."""
    detail = common.current_detail(page)

    if page == common.PAGE_PERMITS:
        if detail.get("type") == "permit":
            permits.render_detail(connection, user, detail["id"])
        else:
            permits.render_list(connection, user)
        return

    if page == common.PAGE_HAZARDS:
        if detail.get("type") == "hazard":
            hazards.render_detail(connection, user, detail["id"])
        else:
            hazards.render_list(connection, user)
        return

    if page == common.PAGE_OVERVIEW:
        overview.render(connection, user)
        return

    if page == common.PAGE_RECORDS:
        records.render(connection, user)
        return

    today.render(connection, user)


def main() -> None:
    """Open the prototype store, render the sidebar and the selected page."""
    with common.database() as connection:
        page = _sidebar(connection)
        user = common.current_user(connection)
        common.render_flash()
        _render_page(page, connection, user)


main()

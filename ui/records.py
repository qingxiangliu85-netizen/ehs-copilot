"""资料与审计 — the SDS knowledge base and the append-only operation records.

The SDS tab has four explicit states (未加载 / 加载中 / 已加载 / 加载失败) instead
of an open-ended "自动加载中" spinner, and it never presents public-source
material as if it were an SDS.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

import streamlit as st

from rag import build_knowledge_base, retrieve_documents
from workflow import audit

from . import common, widgets

DEMO_SDS_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "demo_sds"
    / "EHS_Copilot_Demo_Synthetic_SDS.pdf"
)

STATE_NOT_LOADED = "not_loaded"
STATE_LOADING = "loading"
STATE_LOADED = "loaded"
STATE_ERROR = "error"

STATE_LABELS = {
    STATE_NOT_LOADED: "未加载",
    STATE_LOADING: "加载中",
    STATE_LOADED: "已加载",
    STATE_ERROR: "加载失败",
}

KB_KEY = "_sds_kb"
QUERY_KEY = "_sds_query"

EXAMPLE_QUESTIONS = (
    "主要危险性是什么？",
    "操作需要哪些 PPE？",
    "皮肤接触后如何处理？",
    "应该如何储存？",
    "泄漏时采取什么措施？",
    "火灾时使用什么灭火介质？",
)


class _DemoUpload:
    """Adapt the repository demo PDF to the existing upload interface."""

    def __init__(self, path: Path) -> None:
        self.name = path.name
        self._payload = path.read_bytes()

    def getvalue(self) -> bytes:
        return self._payload


@st.cache_resource(show_spinner=False)
def _build_from_paths(paths: tuple[str, ...]) -> Any:
    """Build (and cache) a knowledge base from local PDF paths."""
    uploads = [_DemoUpload(Path(path)) for path in paths]
    return build_knowledge_base(uploads)


def sds_state() -> str:
    """Return the current SDS tab state."""
    return str(st.session_state.get(common.SDS_STATE_KEY, STATE_NOT_LOADED))


def _set_state(state: str, error: str = "") -> None:
    st.session_state[common.SDS_STATE_KEY] = state
    st.session_state[common.SDS_ERROR_KEY] = error


def _queue_question(question: str) -> None:
    """Widget callback: fill the query box (allowed before the widget exists)."""
    st.session_state[QUERY_KEY] = question


def _ensure_loaded(connection: sqlite3.Connection) -> Any:
    """Return the cached knowledge base, building it on the transition run."""
    if sds_state() == STATE_LOADING:
        try:
            with st.spinner("正在解析 Synthetic SDS 并建立索引……"):
                knowledge_base = _build_from_paths((str(DEMO_SDS_PATH),))
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            _set_state(STATE_ERROR, f"{type(exc).__name__}: {exc}")
            return None
        _set_state(STATE_LOADED)
        st.session_state[KB_KEY] = knowledge_base
        st.rerun()
    if sds_state() == STATE_LOADED:
        return st.session_state.get(KB_KEY)
    return None


def _render_state_banner() -> None:
    state = sds_state()
    label = STATE_LABELS.get(state, state)
    tone = {
        STATE_NOT_LOADED: common.NEUTRAL_CHIP,
        STATE_LOADING: common.RISK_COLORS["中"],
        STATE_LOADED: common.RISK_COLORS["低"],
        STATE_ERROR: common.RISK_COLORS["重大"],
    }.get(state, common.NEUTRAL_CHIP)
    widgets.render_chips([(f"SDS 知识库：{label}", tone)])
    if state == STATE_ERROR:
        st.error(
            "SDS 知识库加载失败："
            + str(st.session_state.get(common.SDS_ERROR_KEY, "") or "未知错误")
        )


def _render_evidence(documents: tuple[Any, ...]) -> None:
    if not documents:
        st.caption("未检索到与该问题相关的 SDS 原文。")
        return
    st.dataframe(
        [
            {
                "排序": index,
                "来源文件": str(document.metadata.get("source", "未知文件")),
                "页码": int(document.metadata.get("page", 0)),
                "原文片段": " ".join(str(document.page_content).split())[:220],
            }
            for index, document in enumerate(documents, start=1)
        ],
        hide_index=True,
        width="stretch",
    )


def _render_sds_tab(connection: sqlite3.Connection) -> None:
    # Materialise the state key so callers/tests can read it directly; the tab
    # always starts in 未加载 and never auto-loads on its own.
    st.session_state.setdefault(common.SDS_STATE_KEY, STATE_NOT_LOADED)
    st.session_state.setdefault(common.SDS_ERROR_KEY, "")
    st.caption(
        "Synthetic SDS 仅用于演示：仓库自带的模拟 SDS，不是任何真实化学品的官方安全数据表。"
    )
    _render_state_banner()

    knowledge_base = _ensure_loaded(connection)

    if knowledge_base is None and sds_state() != STATE_LOADING:
        columns = st.columns([1.4, 3.6])
        clicked = columns[0].button(
            "加载 Synthetic SDS 知识库",
            type="primary",
            width="stretch",
            key="sds_load_button",
        )
        columns[1].caption(
            "加载后可基于语义检索定位危险性、PPE、储存、急救、泄漏与消防信息。"
        )
        if clicked:
            _set_state(STATE_LOADING)
            st.rerun()
        return

    if sds_state() == STATE_LOADING:
        st.info("正在加载 SDS 知识库，请稍候……")
        return

    status = st.columns(3)
    status[0].metric("已加载 SDS", f"{len(knowledge_base.file_names)} 份")
    status[1].metric("文本页", knowledge_base.page_count)
    status[2].metric("文本块", knowledge_base.chunk_count)

    st.markdown("#### 知识库检索")
    question = st.text_input(
        "输入问题",
        key=QUERY_KEY,
        placeholder="例如：HF 皮肤接触后应该如何处理？",
    )
    example_columns = st.columns(3)
    for index, example in enumerate(EXAMPLE_QUESTIONS):
        example_columns[index % 3].button(
            example,
            key=f"sds_example_{index}",
            width="stretch",
            on_click=_queue_question,
            args=(example,),
        )

    if not str(question or "").strip():
        st.caption("输入问题或点击示例，即可基于 SDS 原文检索。")
        return

    try:
        documents = tuple(retrieve_documents(knowledge_base.vector_store, question))
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        st.error(f"检索失败：{exc}")
        return

    st.markdown("**最相关信息**")
    if documents:
        st.markdown(" ".join(str(documents[0].page_content).split())[:360])
    else:
        st.caption("当前 SDS 知识库中未找到相关信息。")
    st.markdown("**证据来源（文件 / 页码 / 原文）**")
    _render_evidence(documents)
    st.caption("AI 输出仅用于信息检索辅助，实际操作前请核对原始 SDS 与本单位 EHS 制度。")


def _render_audit_tab(connection: sqlite3.Connection) -> None:
    entity_types = ["全部", audit.ENTITY_PERMIT, audit.ENTITY_HAZARD, "demo_user"]
    filters = st.columns([1.2, 1.6, 1.2])
    entity_choice = filters[0].selectbox(
        "实体类型",
        entity_types,
        format_func=lambda value: {
            "全部": "全部",
            audit.ENTITY_PERMIT: "作业许可",
            audit.ENTITY_HAZARD: "隐患",
            "demo_user": "Demo 身份",
        }.get(value, value),
    )
    keyword = filters[1].text_input("实体编号", placeholder="例如 PERMIT-DEMO-001")
    limit = filters[2].number_input("显示条数", 20, 500, 100, step=20)

    events = audit.list_events(
        connection,
        entity_type=None if entity_choice == "全部" else entity_choice,
        entity_id=str(keyword or "").strip() or None,
        limit=int(limit),
    )
    if not events:
        widgets.empty_state("没有符合条件的审计记录。")
        return

    events = list(reversed(events))
    st.caption(f"共 {len(events)} 条记录（最新在上）")
    st.dataframe(
        [
            {
                "时间": common.short_datetime(event.get("created_at")),
                "操作者": event.get("actor", "") or "system",
                "实体": f"{widgets.ENTITY_LABELS.get(str(event.get('entity_type', '')), event.get('entity_type', ''))} "
                        f"{event.get('entity_id', '')}",
                "动作": widgets.AUDIT_ACTION_LABELS.get(
                    str(event.get("action", "")), event.get("action", "")
                ),
                "状态变化": (
                    f"{common.business_state_label(event.get('entity_type'), event.get('from_state'))}"
                    f" → "
                    f"{common.business_state_label(event.get('entity_type'), event.get('to_state'))}"
                ),
                "原因": event.get("reason", "") or "—",
            }
            for event in events
        ],
        hide_index=True,
        width="stretch",
    )


def render(connection: sqlite3.Connection, user: Mapping[str, Any]) -> None:
    """Render the 资料与审计 page."""
    st.title("资料与审计")
    st.caption("SDS 知识库与不可修改的操作记录")

    sds_tab, audit_tab = st.tabs(["SDS知识库", "审计记录"])
    with sds_tab:
        _render_sds_tab(connection)
    with audit_tab:
        _render_audit_tab(connection)


__all__ = [
    "STATE_ERROR",
    "STATE_LABELS",
    "STATE_LOADED",
    "STATE_LOADING",
    "STATE_NOT_LOADED",
    "render",
    "sds_state",
]

"""Streamlit entry point for EHS Copilot."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

from config import NOT_FOUND_MESSAGE, SAFETY_DISCLAIMER
from dashboard import render_dashboard_page
from hazards import (
    HAZARD_STATUSES,
    HAZARD_TYPES,
    RISK_LEVELS as HAZARD_RISK_LEVELS,
    calculate_hazard_summary,
    create_demo_hazard_records,
    create_hazard_record,
    delete_hazard_record,
    hazards_to_csv,
    next_hazard_id,
    update_hazard_record,
)
from llm import LLMConfigurationError, LLMResponseError, is_llm_configured
from jsa import calculate_risk, create_jsa_record, records_to_csv
from rag import (
    SDSProcessingError,
    answer_question,
    build_knowledge_base,
    collect_sources,
    retrieve_documents,
)
from workbench import render_job_detail_page, render_workbench_page
from workflow.ui import render_workflow_page


EXAMPLE_QUESTIONS = (
    "主要危险性是什么？",
    "操作需要哪些 PPE？",
    "皮肤接触后如何处理？",
    "应该如何储存？",
    "泄漏时采取什么措施？",
    "火灾时使用什么灭火介质？",
)
DEMO_SDS_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "demo_sds"
    / "EHS_Copilot_Demo_Synthetic_SDS.pdf"
)
class LocalDemoPDF:
    """Adapt a repository demo PDF to the existing upload interface."""

    def __init__(self, path: Path):
        self.name = path.name
        self._payload = path.read_bytes()

    def getvalue(self) -> bytes:
        return self._payload


st.set_page_config(
    page_title="EHS Copilot｜AI辅助EHS风险与危化品管理平台",
    page_icon="🧪",
    layout="wide",
)


def initialize_state() -> None:
    defaults = {
        "vector_store": None,
        "loaded_files": [],
        "page_count": 0,
        "chunk_count": 0,
        "messages": [],
        "suggested_question": None,
        "knowledge_base_mode": None,
        "auto_demo_attempted": False,
        "auto_demo_error": None,
        "jsa_records": [],
        "hazard_notice": None,
        "job_records": [],
        "active_job_id": None,
        "new_job_open": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    if "hazard_records" not in st.session_state:
        st.session_state.hazard_records = create_demo_hazard_records()


def build_evidence(documents: tuple[object, ...]) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for document in documents:
        source = str(document.metadata.get("source", "未知文件"))
        page = int(document.metadata.get("page", 0))
        source_page = (source, page)
        if source_page in seen:
            continue
        seen.add(source_page)
        snippet = " ".join(str(document.page_content).split())
        if len(snippet) > 320:
            snippet = f"{snippet[:320].rstrip()}…"
        evidence.append(
            {
                "rank": len(evidence) + 1,
                "source": source,
                "page": page,
                "snippet": snippet,
            }
        )
    return evidence


def render_evidence(
    evidence: list[dict[str, object]],
    sources: tuple[tuple[str, int], ...] = (),
) -> None:
    st.markdown("#### 证据来源")
    if evidence:
        for item in evidence:
            with st.container(border=True):
                st.markdown(
                    f"**相关性排序 #{item.get('rank', 1)} · "
                    f"{item['source']} · 第 {item['page']} 页**"
                )
                st.caption(str(item["snippet"]))
        return
    if sources:
        for source, page in sources:
            with st.container(border=True):
                st.markdown(f"**{source} · 第 {page} 页**")
                st.caption("该历史消息未保存检索片段，请以原始 SDS 为准。")
        return
    st.caption("未检索到能够支持该回答的 SDS 证据。")


def render_assistant_response(
    content: str,
    evidence: list[dict[str, object]],
    sources: tuple[tuple[str, int], ...] = (),
    response_mode: str = "ai",
) -> None:
    st.markdown("### 查询结果")
    st.markdown("#### 最相关信息 / 摘要")
    st.markdown(content)
    render_evidence(evidence, sources)
    st.caption(SAFETY_DISCLAIMER)


def render_empty_state() -> None:
    st.info(
        "上传 SDS 后，可直接用自然语言查询危险性、PPE、储存、急救、消防及泄漏处置信息，"
        "无需逐页翻阅文档。"
    )
    st.markdown("### 三步开始使用")
    step_columns = st.columns(3)
    steps = (
        ("① 上传 SDS", "在左侧上传一个或多个包含可复制文字的 SDS PDF。"),
        ("② 构建知识库", "点击“构建 / 更新知识库”，系统将解析文件并建立索引。"),
        ("③ 基于证据提问", "输入问题，系统将进行语义检索并显示摘要、来源文件、页码和原文。"),
    )
    for column, (title, description) in zip(step_columns, steps):
        with column:
            with st.container(border=True):
                st.markdown(f"**{title}**")
                st.caption(description)


def render_example_questions(knowledge_base_ready: bool) -> None:
    st.markdown("### 可以问什么？")
    st.caption(
        "知识库就绪后，可点击示例直接提问。"
        if knowledge_base_ready
        else "完成 SDS 知识库构建后，即可点击以下示例问题。"
    )
    question_columns = st.columns(2)
    for index, example in enumerate(EXAMPLE_QUESTIONS):
        if question_columns[index % 2].button(
            example,
            key=f"example_question_{index}",
            use_container_width=True,
            disabled=not knowledge_base_ready,
        ):
            st.session_state.suggested_question = example


def activate_knowledge_base(result: object, mode: str) -> None:
    st.session_state.vector_store = result.vector_store
    st.session_state.loaded_files = list(result.file_names)
    st.session_state.page_count = result.page_count
    st.session_state.chunk_count = result.chunk_count
    st.session_state.messages = []
    st.session_state.knowledge_base_mode = mode


def build_extractive_summary(documents: tuple[object, ...]) -> str:
    """Extract a concise passage from the top retrieved SDS result."""
    if not documents:
        return NOT_FOUND_MESSAGE

    lines = [line.strip() for line in str(documents[0].page_content).splitlines()]
    chinese_lines: list[str] = []
    other_lines: list[str] = []
    for line in lines:
        if not line:
            continue
        lowered = line.casefold()
        if lowered.startswith("synthetic sds for demonstration only") or line.startswith(
            "模拟 SDS，仅用于软件演示"
        ):
            continue
        if line in {"中文 / CHINESE", "英文 / ENGLISH"}:
            continue
        if lowered.startswith("ehs copilot demo cleaner") or lowered.startswith(
            "synthetic sds | page"
        ):
            continue
        if re.match(r"^(section|第)\s*\d+", line, flags=re.IGNORECASE):
            continue
        if (
            not re.search(r"[\u4e00-\u9fff]", line)
            and line.upper() == line
            and len(line.split()) <= 8
        ):
            continue
        line = re.sub(r"^[A-Z0-9][A-Z0-9_-]{2,}:\s*", "", line)
        if re.search(r"[\u4e00-\u9fff]", line):
            chinese_lines.append(line)
        else:
            other_lines.append(line)

    summary = " ".join(chinese_lines or other_lines).strip()
    if not summary:
        return "匹配到以下 SDS 证据，请核对原文。"
    if len(summary) > 360:
        summary = f"{summary[:360].rstrip()}…"
    return summary


def build_retrieval_response(vector_store: object, question: str) -> dict[str, object]:
    documents = tuple(retrieve_documents(vector_store, question))
    evidence = build_evidence(documents)
    sources = collect_sources(documents)
    content = build_extractive_summary(documents)
    return {
        "content": content,
        "sources": sources,
        "evidence": evidence,
        "response_mode": "retrieval",
    }


def render_risk_badge(score: int, level: str, label: str) -> None:
    st.metric(label, f"{score} · {level}")


def render_jsa_page() -> None:
    """Render the minimal, session-only JSA assessment workflow."""
    st.title("JSA风险评估")
    st.caption("Job Safety Analysis · 作业安全分析")
    st.info(
        "将作业步骤、危害与控制措施逐条加入当前会话，系统自动计算初始风险和残余风险。"
    )
    st.warning(
        "风险评估结果仅作求职作品演示，实际风险等级应依据企业制度和现场评估确定。"
    )
    st.caption("下方 HF酸洗内容为模拟演示数据，不作为真实作业SOP或现场操作依据。")
    st.caption(
        "风险分级：1–4 低风险｜5–9 中风险｜10–16 高风险｜17–25 重大风险"
    )

    job_name = st.text_input("作业名称", value="HF酸洗（模拟演示）")
    job_step = st.text_area(
        "作业步骤",
        value="将待处理样件放入模拟酸洗槽并完成清洗",
        height=80,
    )
    hazard = st.text_area(
        "危害因素",
        value="HF飞溅、酸雾吸入、容器泄漏",
        height=80,
    )
    consequence = st.text_area(
        "可能后果",
        value="皮肤或眼睛化学灼伤、吸入伤害",
        height=80,
    )

    initial_columns = st.columns(2)
    likelihood = initial_columns[0].select_slider(
        "可能性 L（1–5）", options=range(1, 6), value=4, key="jsa_initial_l"
    )
    severity = initial_columns[1].select_slider(
        "严重度 S（1–5）", options=range(1, 6), value=5, key="jsa_initial_s"
    )
    initial_score, initial_level = calculate_risk(likelihood, severity)
    render_risk_badge(initial_score, initial_level, "初始风险 R=L×S")

    existing_controls = st.text_area(
        "现有控制措施",
        value="模拟措施：局部排风、耐酸碱手套、护目镜与面屏、应急冲淋设施",
        height=90,
    )
    suggested_controls = st.text_area(
        "建议控制措施",
        value="模拟建议：密闭加料、液位监测、双人复核，并按企业制度完善现场应急措施",
        height=90,
    )

    residual_columns = st.columns(2)
    residual_likelihood = residual_columns[0].select_slider(
        "控制后可能性 L（1–5）",
        options=range(1, 6),
        value=2,
        key="jsa_residual_l",
    )
    residual_severity = residual_columns[1].select_slider(
        "控制后严重度 S（1–5）",
        options=range(1, 6),
        value=5,
        key="jsa_residual_s",
    )
    residual_score, residual_level = calculate_risk(
        residual_likelihood, residual_severity
    )
    render_risk_badge(residual_score, residual_level, "残余风险 R=L×S")

    submitted = st.button("添加到JSA", type="primary", use_container_width=True)

    if submitted:
        required_fields = (job_name, job_step, hazard, consequence)
        if not all(value.strip() for value in required_fields):
            st.error("请填写作业名称、作业步骤、危害因素和可能后果。")
        else:
            st.session_state.jsa_records.append(
                create_jsa_record(
                    job_name=job_name,
                    job_step=job_step,
                    hazard=hazard,
                    consequence=consequence,
                    likelihood=likelihood,
                    severity=severity,
                    existing_controls=existing_controls,
                    suggested_controls=suggested_controls,
                    residual_likelihood=residual_likelihood,
                    residual_severity=residual_severity,
                )
            )
            st.success("已添加到当前会话的JSA表格。")

    st.divider()
    st.subheader("当前会话JSA表格")
    records = st.session_state.jsa_records
    if not records:
        st.caption("尚未添加记录。可连续添加多个作业步骤。")
    else:
        st.dataframe(records, hide_index=True, use_container_width=True)
        action_columns = st.columns((2, 1, 1))
        delete_index = action_columns[0].selectbox(
            "选择要删除的记录",
            options=range(len(records)),
            format_func=lambda index: (
                f"#{index + 1} {records[index]['作业名称']}｜"
                f"{records[index]['作业步骤']}"
            ),
        )
        if action_columns[1].button("删除所选", use_container_width=True):
            records.pop(delete_index)
            st.rerun()
        if action_columns[2].button("清空记录", use_container_width=True):
            records.clear()
            st.rerun()

        st.download_button(
            "下载JSA CSV",
            data=records_to_csv(records),
            file_name="EHS_Copilot_JSA.csv",
            mime="text/csv",
            use_container_width=True,
        )

    st.divider()
    st.caption(SAFETY_DISCLAIMER)


def render_hazard_page() -> None:
    """Render session-only hazard tracking and corrective-action management."""
    st.title("隐患整改管理")
    st.caption("Hazard Tracking · Corrective Action Management")
    st.info(
        "记录隐患、责任人、整改期限与整改状态，并在当前会话中完成维护和导出。"
    )
    st.warning(
        "本工具为EHS数字化原型/求职展示项目，示例数据仅用于功能演示，"
        "不替代企业制度、现场风险评估及专业人员判断。"
    )
    st.caption("默认案例均标注为“模拟数据 / Demo，不代表真实企业记录”。")

    notice = st.session_state.pop("hazard_notice", None)
    if notice:
        st.success(notice)

    records = st.session_state.hazard_records
    summary = calculate_hazard_summary(records)
    metric_columns = st.columns(5)
    metric_columns[0].metric("隐患总数", summary["total"])
    metric_columns[1].metric("待整改", summary["pending"])
    metric_columns[2].metric("整改中", summary["in_progress"])
    metric_columns[3].metric("已关闭", summary["closed"])
    metric_columns[4].metric("整改完成率", f"{summary['completion_rate']:.1f}%")
    st.caption(
        "风险等级分布："
        + "｜".join(
            f"{level} {summary['risk_distribution'][level]}"
            for level in HAZARD_RISK_LEVELS
        )
    )

    if records:
        st.markdown("#### 整改闭环状态")
        today = date.today()
        status_rows = []
        for record in records:
            due_text = str(record.get("整改期限", "")).strip()
            overdue = False
            if due_text and str(record.get("状态", "")) != "已关闭":
                try:
                    overdue = date.fromisoformat(due_text) < today
                except ValueError:
                    overdue = False
            evidence_count = len(list(record.get("rectification_evidence") or ()))
            status_rows.append(
                {
                    "隐患编号": record.get("隐患编号", ""),
                    "问题": record.get("隐患描述", ""),
                    "风险等级": record.get("风险等级", ""),
                    "责任人": record.get("责任人", ""),
                    "截止时间": due_text,
                    "当前状态": record.get("状态", ""),
                    "是否逾期": "⚠️ 是" if overdue else "否",
                    "整改证据": f"✅ {evidence_count} 条" if evidence_count else "—",
                    "复查人": record.get("reviewer", ""),
                }
            )
        st.dataframe(status_rows, hide_index=True, use_container_width=True)

    add_tab, manage_tab = st.tabs(("新增隐患", "查看与维护"))

    with add_tab:
        with st.form("hazard_create_form", clear_on_submit=False):
            first_row = st.columns((1, 1, 1))
            hazard_id = first_row[0].text_input(
                "隐患编号", value=next_hazard_id(records)
            )
            hazard_type = first_row[1].selectbox("隐患类型", HAZARD_TYPES)
            risk_level = first_row[2].selectbox(
                "风险等级", HAZARD_RISK_LEVELS, index=1
            )
            description = st.text_area(
                "隐患描述", placeholder="请客观描述发现的问题与所在场景。", height=90
            )
            second_row = st.columns((1, 1, 1))
            owner = second_row[0].text_input("责任人", placeholder="姓名或责任岗位")
            found_on = second_row[1].date_input("发现日期", value=date.today())
            due_on = second_row[2].date_input(
                "整改期限", value=date.today() + timedelta(days=14)
            )
            corrective_action = st.text_area(
                "整改措施", placeholder="填写拟采取或已采取的整改措施。", height=100
            )
            status = st.selectbox("状态", HAZARD_STATUSES)
            submitted = st.form_submit_button(
                "新增隐患记录", type="primary", use_container_width=True
            )

        if submitted:
            try:
                record = create_hazard_record(
                    hazard_id=hazard_id,
                    description=description,
                    hazard_type=hazard_type,
                    risk_level=risk_level,
                    owner=owner,
                    found_on=found_on,
                    due_on=due_on,
                    corrective_action=corrective_action,
                    status=status,
                    existing_ids=(
                        str(item.get("隐患编号", "")) for item in records
                    ),
                )
                records.append(record)
                st.session_state.hazard_notice = f"已新增隐患记录：{record['隐患编号']}"
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    with manage_tab:
        st.subheader("全部隐患")
        if not records:
            st.caption("当前会话暂无隐患记录，可在“新增隐患”中创建。")
        else:
            st.dataframe(records, hide_index=True, use_container_width=True)
            selected_id = st.selectbox(
                "选择要维护的隐患",
                options=[str(record["隐患编号"]) for record in records],
                format_func=lambda value: next(
                    f"{value}｜{record['隐患描述']}"
                    for record in records
                    if record["隐患编号"] == value
                ),
                key="hazard_selected_id",
            )
            selected_record = next(
                record for record in records if record["隐患编号"] == selected_id
            )

            st.markdown("#### 快速更新整改状态")
            status_columns = st.columns((2, 1))
            quick_status = status_columns[0].selectbox(
                "整改状态",
                HAZARD_STATUSES,
                index=HAZARD_STATUSES.index(str(selected_record["状态"])),
                key=f"hazard_quick_status_{selected_id}",
            )
            if status_columns[1].button(
                "更新状态", type="primary", use_container_width=True
            ):
                update_hazard_record(records, selected_id, {"状态": quick_status})
                st.session_state.hazard_notice = f"已更新 {selected_id} 的整改状态。"
                st.rerun()

            st.markdown("#### 修改隐患记录")
            with st.form(f"hazard_edit_form_{selected_id}"):
                edit_row = st.columns((1, 1))
                edit_type = edit_row[0].selectbox(
                    "隐患类型",
                    HAZARD_TYPES,
                    index=HAZARD_TYPES.index(str(selected_record["隐患类型"])),
                    key=f"hazard_edit_type_{selected_id}",
                )
                edit_risk = edit_row[1].selectbox(
                    "风险等级",
                    HAZARD_RISK_LEVELS,
                    index=HAZARD_RISK_LEVELS.index(
                        str(selected_record["风险等级"])
                    ),
                    key=f"hazard_edit_risk_{selected_id}",
                )
                edit_description = st.text_area(
                    "隐患描述",
                    value=str(selected_record["隐患描述"]),
                    height=90,
                    key=f"hazard_edit_description_{selected_id}",
                )
                edit_dates = st.columns((1, 1, 1))
                edit_owner = edit_dates[0].text_input(
                    "责任人",
                    value=str(selected_record["责任人"]),
                    key=f"hazard_edit_owner_{selected_id}",
                )
                edit_found_on = edit_dates[1].date_input(
                    "发现日期",
                    value=date.fromisoformat(str(selected_record["发现日期"])),
                    key=f"hazard_edit_found_{selected_id}",
                )
                edit_due_on = edit_dates[2].date_input(
                    "整改期限",
                    value=date.fromisoformat(str(selected_record["整改期限"])),
                    key=f"hazard_edit_due_{selected_id}",
                )
                edit_action = st.text_area(
                    "整改措施",
                    value=str(selected_record["整改措施"]),
                    height=100,
                    key=f"hazard_edit_action_{selected_id}",
                )
                edit_status = st.selectbox(
                    "状态",
                    HAZARD_STATUSES,
                    index=HAZARD_STATUSES.index(str(selected_record["状态"])),
                    key=f"hazard_edit_status_{selected_id}",
                )
                edit_submitted = st.form_submit_button(
                    "保存修改", use_container_width=True
                )

            if edit_submitted:
                try:
                    update_hazard_record(
                        records,
                        selected_id,
                        {
                            "隐患描述": edit_description,
                            "隐患类型": edit_type,
                            "风险等级": edit_risk,
                            "责任人": edit_owner,
                            "发现日期": edit_found_on,
                            "整改期限": edit_due_on,
                            "整改措施": edit_action,
                            "状态": edit_status,
                        },
                    )
                    st.session_state.hazard_notice = f"已保存隐患记录：{selected_id}"
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

            st.markdown("#### 删除与导出")
            delete_column, clear_column = st.columns(2)
            if delete_column.button(
                "删除当前记录", use_container_width=True, key="hazard_delete_selected"
            ):
                delete_hazard_record(records, selected_id)
                st.session_state.hazard_notice = f"已删除隐患记录：{selected_id}"
                st.rerun()

            confirm_clear = clear_column.checkbox(
                "确认清空全部记录", key="hazard_confirm_clear"
            )
            if clear_column.button(
                "清空全部记录",
                use_container_width=True,
                disabled=not confirm_clear,
                key="hazard_clear_all",
            ):
                records.clear()
                st.session_state.hazard_notice = "已清空当前会话的全部隐患记录。"
                st.rerun()

            st.download_button(
                "下载隐患记录 CSV",
                data=hazards_to_csv(records),
                file_name="EHS_Copilot_Hazard_Records.csv",
                mime="text/csv",
                use_container_width=True,
            )

    st.divider()
    st.caption(
        "本工具为EHS数字化原型/求职展示项目，示例数据仅用于功能演示，"
        "不替代企业制度、现场风险评估及专业人员判断。"
    )


initialize_state()
with st.sidebar:
    st.subheader("功能导航")
    selected_page = st.radio(
        "功能导航",
        ("作业闭环", "隐患整改", "EHS驾驶舱", "工具箱"),
        index=0,
        label_visibility="collapsed",
    )
    toolbox_page = ""
    if selected_page == "工具箱":
        toolbox_page = st.selectbox(
            "工具箱",
            ("SDS资料库", "JSA工具", "AI工作流控制台"),
        )

if selected_page == "作业闭环":
    with st.sidebar:
        st.divider()
        st.caption(
            "以作业单为主线的危化品作业安全审查与整改闭环。"
            "公开安全证据与 SDS 证据严格分开。"
        )
    if st.session_state.get("active_job_id"):
        render_job_detail_page(
            st.session_state.job_records,
            st.session_state.hazard_records,
            vector_store=st.session_state.vector_store,
        )
    else:
        render_workbench_page(
            st.session_state.job_records,
            st.session_state.hazard_records,
        )
    st.stop()

if selected_page == "隐患整改":
    with st.sidebar:
        st.divider()
        st.warning(
            "示例数据仅用于功能演示；实际隐患整改应依据企业制度和现场要求执行。"
        )
    render_hazard_page()
    st.stop()

if selected_page == "EHS驾驶舱":
    render_dashboard_page(
        st.session_state.jsa_records,
        st.session_state.hazard_records,
        st.session_state.job_records,
    )
    st.stop()

if toolbox_page == "AI工作流控制台":
    with st.sidebar:
        st.divider()
        st.warning(
            "工作流自动生成的 JSA 与隐患记录均为草稿，须经人工确认后方可作为正式记录。"
        )
    # The workflow assistant may need an SDS knowledge base; reuse the same
    # Demo bootstrap as the SDS page so the two pages behave consistently.
    if (
        st.session_state.vector_store is None
        and not st.session_state.auto_demo_attempted
    ):
        st.session_state.auto_demo_attempted = True
        try:
            with st.spinner("正在自动加载 Synthetic SDS 并建立 Demo 知识库……"):
                demo_result = build_knowledge_base([LocalDemoPDF(DEMO_SDS_PATH)])
            activate_knowledge_base(demo_result, mode="demo")
        except Exception as exc:
            st.session_state.auto_demo_error = str(exc)
    render_workflow_page(
        vector_store=st.session_state.vector_store,
        jsa_records=st.session_state.jsa_records,
        hazard_records=st.session_state.hazard_records,
        loaded_files=tuple(st.session_state.loaded_files),
    )
    st.stop()

if toolbox_page == "JSA工具":
    with st.sidebar:
        st.divider()
        st.warning(
            "JSA结果仅作演示；实际风险等级须依据企业制度和现场评估确定。"
        )
    render_jsa_page()
    st.stop()

# 工具箱 → SDS资料库（以下为 SDS 检索页）

llm_available = is_llm_configured()

if (
    st.session_state.vector_store is None
    and not st.session_state.auto_demo_attempted
):
    st.session_state.auto_demo_attempted = True
    try:
        with st.spinner("正在自动加载 Synthetic SDS 并建立 Demo 知识库……"):
            demo_result = build_knowledge_base([LocalDemoPDF(DEMO_SDS_PATH)])
        activate_knowledge_base(demo_result, mode="demo")
    except Exception as exc:
        st.session_state.auto_demo_error = str(exc)

with st.sidebar:
    st.header("EHS Copilot")
    st.caption("上传并构建自己的 SDS 知识库")
    uploaded_files = st.file_uploader(
        "上传一个或多个 SDS PDF",
        type=["pdf"],
        accept_multiple_files=True,
        help="当前版本仅解析包含可复制文字的 PDF，暂不支持扫描件 OCR。",
    )

    if uploaded_files:
        st.markdown("**待构建文件：**")
        for uploaded_file in uploaded_files:
            st.write(f"- {uploaded_file.name}")

    if st.button("构建 / 更新知识库", type="primary", use_container_width=True):
        if not uploaded_files:
            st.warning("请先上传至少一份 SDS PDF。")
        else:
            try:
                with st.spinner("正在解析 PDF、生成 Embedding 并构建 FAISS 索引……"):
                    result = build_knowledge_base(uploaded_files)
                activate_knowledge_base(result, mode="uploaded")
                st.success(
                    f"知识库已更新：{len(result.file_names)} 个文件，"
                    f"{result.page_count} 个文本页，{result.chunk_count} 个文本块。"
                )
            except SDSProcessingError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"知识库构建失败：{exc}")

    st.divider()
    st.markdown("**当前已加载文件：**")
    if st.session_state.loaded_files:
        if st.session_state.knowledge_base_mode == "demo":
            st.success("Demo 知识库已就绪")
        for file_name in st.session_state.loaded_files:
            st.write(f"- {file_name}")
        st.caption(
            f"文本页：{st.session_state.page_count}｜"
            f"文本块：{st.session_state.chunk_count}"
        )
    else:
        st.caption("尚未构建知识库")

    st.divider()
    st.warning(SAFETY_DISCLAIMER)

st.title("SDS资料库")
st.caption("工具箱 / SDS资料库｜上传并构建自己的 SDS 知识库")
st.markdown("### SDS智能检索")
st.markdown(
    "**基于语义检索快速定位危险性、PPE、储存、急救、泄漏及消防信息，"
    "并提供原始 SDS 页码与证据追溯。**"
)

capability_columns = st.columns(3)
for column, capability in zip(
    capability_columns,
    ("AI Semantic Search", "SDS Evidence Retrieval", "Multi-PDF Knowledge Base"),
):
    with column:
        with st.container(border=True):
            st.markdown(f"**{capability}**")

if st.session_state.auto_demo_error:
    st.error(f"Demo 知识库自动加载失败：{st.session_state.auto_demo_error}")

if st.session_state.vector_store is None:
    render_empty_state()
else:
    if st.session_state.knowledge_base_mode == "demo":
        st.success("Demo 知识库已就绪，可以开始提问。")
    else:
        st.success("知识库已就绪，可以开始提问。")
    status_columns = st.columns(3)
    status_columns[0].metric("已加载 SDS", f"{len(st.session_state.loaded_files)} 份")
    status_columns[1].metric("文本页", st.session_state.page_count)
    status_columns[2].metric("文本 Chunks", st.session_state.chunk_count)

render_example_questions(st.session_state.vector_store is not None)

st.divider()

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        if message["role"] == "assistant":
            render_assistant_response(
                message["content"],
                message.get("evidence", []),
                message.get("sources", ()),
                message.get("response_mode", "ai"),
            )
        else:
            st.markdown(message["content"])

typed_question = st.chat_input(
    "例如：HF 皮肤接触后应该如何处理？",
    disabled=st.session_state.vector_store is None,
)
question = typed_question or st.session_state.pop("suggested_question", None)

if question:
    st.session_state.suggested_question = None
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        try:
            with st.spinner("正在检索 SDS……"):
                if llm_available:
                    result = answer_question(st.session_state.vector_store, question)
                    response = {
                        "content": result.answer,
                        "sources": result.sources,
                        "evidence": build_evidence(result.retrieved_documents),
                        "response_mode": "ai",
                    }
                else:
                    response = build_retrieval_response(
                        st.session_state.vector_store,
                        question,
                    )
            render_assistant_response(
                response["content"],
                response["evidence"],
                response["sources"],
                response["response_mode"],
            )
            st.session_state.messages.append({"role": "assistant", **response})
        except LLMConfigurationError as exc:
            st.warning(str(exc))
        except LLMResponseError as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(f"问答失败：{exc}")

st.divider()
st.markdown("**Tech Stack：Python · Streamlit · HuggingFace Embeddings · FAISS**")
st.markdown("**Safety Note**")
st.caption(
    "For information retrieval assistance only. Always verify the original SDS "
    "and site-specific EHS procedures."
)

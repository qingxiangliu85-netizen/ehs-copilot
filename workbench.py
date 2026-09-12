"""Business-first Streamlit pages for the V4 job-closure workbench.

The workbench is the product front door: it shows the job pipeline, the
next best action for each job and the ten business stages of one job.  All
business rules stay in ``jobs`` / ``job_review`` / ``closure`` — this module
only renders and wires those functions to widgets.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator, Mapping, MutableSequence

import streamlit as st

from closure import (
    RECTIFICATION_EVIDENCE_TYPES,
    close_hazard,
    close_job,
    complete_job_execution,
    create_job_hazard,
    draft_hazards_from_jsa,
    review_hazard,
    submit_rectification_evidence,
    update_hazard_rectification,
)
from dashboard import count_overdue_hazards, get_job_metrics
from hazards import RISK_LEVELS
from job_review import (
    build_job_approval_request,
    confirm_jsa,
    decide_job_approval,
    draft_jsa,
    start_job_execution,
)
from jobs import (
    JOB_STATUS_APPROVED,
    JOB_STATUS_AWAITING_APPROVAL,
    JOB_STATUS_AWAITING_EHS,
    JOB_STATUS_AWAITING_REVIEW,
    JOB_STATUS_CLOSED,
    JOB_STATUS_DRAFT,
    JOB_STATUS_EXECUTING,
    JOB_STATUS_REJECTED,
    JOB_STATUSES,
    create_job,
    get_job,
)
from workflow.hitl import ACTION_APPROVE, ACTION_MODIFY, ACTION_REJECT, ApprovalDecision


ACTIVE_JOB_KEY = "active_job_id"
NEW_JOB_KEY = "new_job_open"

JOB_FLOW: tuple[str, ...] = (
    JOB_STATUS_DRAFT,
    JOB_STATUS_AWAITING_EHS,
    JOB_STATUS_AWAITING_APPROVAL,
    JOB_STATUS_APPROVED,
    JOB_STATUS_EXECUTING,
    JOB_STATUS_AWAITING_REVIEW,
    JOB_STATUS_CLOSED,
)

_EVIDENCE_TOPICS: tuple[tuple[str, str], ...] = (
    ("危险性", "主要危险性是什么？"),
    ("PPE", "操作需要哪些 PPE？"),
    ("急救", "皮肤接触后如何处理？"),
    ("泄漏", "泄漏时采取什么措施？"),
    ("消防", "火灾时使用什么灭火介质？"),
    ("储存", "应该如何储存？"),
)

_BADGES = {
    "done": "✅ 已完成",
    "active": "🔵 当前阶段",
    "todo": "○ 待进行",
    "rejected": "⛔ 已驳回",
}

_NEXT_ACTIONS: dict[str, str] = {
    JOB_STATUS_DRAFT: "生成 JSA 草稿",
    JOB_STATUS_AWAITING_EHS: "EHS 人工确认",
    JOB_STATUS_AWAITING_APPROVAL: "审批（批准 / 修改 / 驳回）",
    JOB_STATUS_APPROVED: "开始执行",
    JOB_STATUS_EXECUTING: "完成执行 / 创建关联隐患",
    JOB_STATUS_AWAITING_REVIEW: "整改复查 / 关闭作业",
    JOB_STATUS_CLOSED: "已完成",
    JOB_STATUS_REJECTED: "已驳回（流程终止）",
}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def find_hazard(
    hazard_records: MutableSequence[dict[str, object]], hazard_id: str
) -> dict[str, object] | None:
    target = str(hazard_id or "").strip()
    for record in hazard_records:
        if str(record.get("隐患编号", "")).strip() == target:
            return record
    return None


def job_risk_label(job: Mapping[str, object]) -> str:
    confirmation = dict(job.get("jsa_confirmation") or {})
    final = dict(confirmation.get("final") or {})
    return str(
        final.get("残余风险等级") or final.get("风险等级") or "—"
    )


def job_owner_label(job: Mapping[str, object]) -> str:
    info = dict(job.get("execution_info") or {})
    return str(info.get("executor") or info.get("completed_by") or "—")


def next_action_label(job: Mapping[str, object]) -> str:
    return _NEXT_ACTIONS.get(str(job.get("status", "")), "—")


def close_blockers(hazard: Mapping[str, object]) -> list[str]:
    """Return the reasons a hazard cannot be closed yet (for UI feedback)."""
    blockers: list[str] = []
    if not list(hazard.get("rectification_evidence") or ()):
        blockers.append("缺少整改证据")
    if not str(hazard.get("reviewer", "")).strip():
        blockers.append("缺少复查人")
    if not str(hazard.get("review_note", "")).strip():
        blockers.append("缺少复查意见")
    if str(hazard.get("状态", "")) == "已关闭":
        blockers.append("隐患已关闭")
    return blockers


def job_close_blockers(
    job: Mapping[str, object],
    hazard_records: MutableSequence[dict[str, object]],
) -> list[str]:
    """Return the reasons a job cannot be closed yet (for UI feedback)."""
    status = str(job.get("status", ""))
    if status == JOB_STATUS_CLOSED:
        return []
    if status != JOB_STATUS_AWAITING_REVIEW:
        return [f"作业当前状态为「{status}」，需先完成执行并进入「待复查」"]
    open_ids: list[str] = []
    for hazard_id in job.get("linked_hazard_ids") or ():
        hazard = find_hazard(hazard_records, str(hazard_id))
        if hazard is None:
            open_ids.append(f"{hazard_id}（记录缺失）")
        elif str(hazard.get("状态", "")) != "已关闭":
            open_ids.append(str(hazard_id))
    if open_ids:
        return ["存在未关闭的关联隐患：" + "、".join(open_ids)]
    return []


@contextmanager
def _stage(number: int, title: str, state: str) -> Iterator[None]:
    with st.container(border=True):
        st.markdown(f"#### {number}. {title}　{_BADGES.get(state, '')}")
        yield


def _stage_state(
    job: Mapping[str, object], *, done: bool, active: bool
) -> str:
    if str(job.get("status", "")) == JOB_STATUS_REJECTED and active:
        return "rejected"
    if done:
        return "done"
    if active:
        return "active"
    return "todo"


def load_hf_case(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
) -> str:
    """Load the HF pickling demo case once and return its job id."""
    from demo_cases import create_demo_hf_case

    case = create_demo_hf_case()
    job = case["job"]
    identifier = str(job["job_id"])
    if get_job(job_records, identifier) is None:
        job_records.append(job)
    for hazard in case["linked_hazards"]:
        if find_hazard(hazard_records, str(hazard["隐患编号"])) is None:
            hazard_records.append(hazard)
    return identifier


def _public_evidence_rows(job: Mapping[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in job.get("public_evidence") or ():
        rows.append(
            {
                "机构": item.get("organization", ""),
                "标题": item.get("source_title", ""),
                "主题": item.get("topic", ""),
                "章节": item.get("section", ""),
                "支持片段": item.get("passage", ""),
                "原始链接": item.get("source_url", ""),
            }
        )
    return rows


def _sds_evidence_rows(job: Mapping[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in job.get("sds_evidence") or ():
        rows.append(
            {
                "文件": item.get("source", ""),
                "页码": item.get("page", ""),
                "章节": item.get("sections", ""),
                "主题": item.get("topic", ""),
                "原文片段": item.get("snippet", "") or item.get("passage", ""),
            }
        )
    return rows


def _extract_sds_snapshots(
    vector_store: Any, job: Mapping[str, object]
) -> list[dict[str, object]]:
    from rag import retrieve_documents

    snapshots: list[dict[str, object]] = []
    for topic, question in _EVIDENCE_TOPICS:
        documents = retrieve_documents(vector_store, question)
        for document in documents[:1]:
            snapshots.append(
                {
                    "source": str(document.metadata.get("source", "")),
                    "page": int(document.metadata.get("page", 0) or 0),
                    "sections": str(document.metadata.get("sections", "")),
                    "snippet": " ".join(str(document.page_content).split())[:320],
                    "topic": topic,
                }
            )
    return snapshots


def _hazard_rows(
    job: Mapping[str, object],
    hazard_records: MutableSequence[dict[str, object]],
) -> list[dict[str, object]]:
    today = date.today()
    rows: list[dict[str, object]] = []
    for hazard_id in job.get("linked_hazard_ids") or ():
        hazard = find_hazard(hazard_records, str(hazard_id))
        if hazard is None:
            rows.append(
                {
                    "隐患编号": hazard_id,
                    "问题": "（记录缺失）",
                    "风险等级": "",
                    "责任人": "",
                    "截止时间": "",
                    "当前状态": "",
                    "是否逾期": "",
                    "整改证据": "",
                }
            )
            continue
        overdue = False
        raw_due = str(hazard.get("整改期限", "")).strip()
        if raw_due and str(hazard.get("状态", "")) != "已关闭":
            try:
                overdue = date.fromisoformat(raw_due) < today
            except ValueError:
                overdue = False
        evidence_count = len(list(hazard.get("rectification_evidence") or ()))
        rows.append(
            {
                "隐患编号": hazard.get("隐患编号", ""),
                "问题": hazard.get("隐患描述", ""),
                "风险等级": hazard.get("风险等级", ""),
                "责任人": hazard.get("责任人", ""),
                "截止时间": raw_due,
                "当前状态": hazard.get("状态", ""),
                "是否逾期": "⚠️ 是" if overdue else "否",
                "整改证据": f"✅ {evidence_count} 条" if evidence_count else "—",
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Workbench (home)
# --------------------------------------------------------------------------- #


def render_workbench_page(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
) -> None:
    """Render the job pipeline home page."""
    st.title("作业闭环")
    st.caption(
        "危化品非例行作业：新建 → 证据 → JSA → 确认 → 审批 → 执行 → 整改 → 关闭"
    )

    metrics = get_job_metrics(job_records)
    overdue = count_overdue_hazards(hazard_records)
    cards = st.columns(6)
    cards[0].metric("在办作业", metrics["job_active"])
    cards[1].metric("待审批", metrics["job_awaiting_approval"])
    cards[2].metric("执行中", metrics["job_executing"])
    cards[3].metric("待复查", metrics["job_awaiting_review"])
    cards[4].metric("高风险作业", metrics["job_high_risk"])
    cards[5].metric("逾期隐患", overdue)

    actions = st.columns((1, 1, 2))
    with actions[0]:
        if st.button(
            "一键载入HF酸洗模拟案例",
            type="primary",
            use_container_width=True,
            key="wb_load_hf_case",
        ):
            st.session_state[ACTIVE_JOB_KEY] = load_hf_case(
                job_records, hazard_records
            )
            st.rerun()
    with actions[1]:
        if st.button(
            "新建危化品作业",
            use_container_width=True,
            key="wb_new_job",
        ):
            st.session_state[NEW_JOB_KEY] = not st.session_state.get(
                NEW_JOB_KEY, False
            )

    if st.session_state.get(NEW_JOB_KEY):
        _render_new_job_form(job_records)

    st.divider()
    st.subheader("作业列表")
    filter_column, count_column = st.columns((2, 1))
    status_filter = filter_column.selectbox(
        "状态筛选",
        ("全部",) + JOB_STATUSES,
        key="wb_status_filter",
    )
    rows = [
        job
        for job in job_records
        if status_filter == "全部" or str(job.get("status", "")) == status_filter
    ]
    count_column.caption(f"共 {len(rows)} 条作业")

    if not rows:
        st.info("暂无作业。点击「一键载入HF酸洗模拟案例」体验完整闭环，或新建一条作业。")
        return

    for job in rows:
        _render_job_row(job)


def _render_job_row(job: Mapping[str, object]) -> None:
    chemicals = "、".join(
        str(item.get("name", ""))
        for item in job.get("chemicals") or ()
        if str(item.get("name", "")).strip()
    ) or "—"
    with st.container(border=True):
        columns = st.columns((1.1, 2.0, 1.3, 1.0, 1.0, 1.0, 1.2, 1.5, 0.8))
        columns[0].markdown(f"**{job.get('job_id', '')}**")
        columns[1].markdown(str(job.get("job_name", "")))
        columns[2].caption(chemicals)
        columns[3].markdown(job_risk_label(job))
        columns[4].markdown(str(job.get("status", "")))
        columns[5].caption(job_owner_label(job))
        columns[6].caption(str(job.get("updated_at", "")))
        columns[7].caption(next_action_label(job))
        if columns[8].button(
            "打开",
            key=f"wb_open_{job.get('job_id', '')}",
            use_container_width=True,
        ):
            st.session_state[ACTIVE_JOB_KEY] = str(job.get("job_id", ""))
            st.rerun()
        data_label = str(job.get("data_label", "")).strip()
        if data_label:
            st.caption(f"数据性质：{data_label}｜下一步：{next_action_label(job)}")


def _render_new_job_form(job_records: MutableSequence[dict[str, object]]) -> None:
    from jobs import next_job_id

    with st.form("wb_new_job_form"):
        st.markdown("**新建危化品非例行作业**")
        first = st.columns(2)
        job_name = first[0].text_input("作业名称", key="wb_new_name")
        area = first[1].text_input("作业区域", key="wb_new_area")
        chemicals_text = st.text_input(
            "化学品（用中文顿号/逗号分隔）", key="wb_new_chemicals"
        )
        steps_text = st.text_area(
            "作业步骤（每行一步）", height=100, key="wb_new_steps"
        )
        created_by = st.text_input("创建人", key="wb_new_creator")
        submitted = st.form_submit_button(
            "创建作业", type="primary", use_container_width=True
        )
    if not submitted:
        return
    try:
        if not str(job_name).strip():
            raise ValueError("作业名称不能为空。")
        chemicals = [
            {"name": part.strip(), "aliases": []}
            for part in chemicals_text.replace("，", "、").split("、")
            if part.strip()
        ]
        steps = [
            {"order": index, "name": line.strip()}
            for index, line in enumerate(steps_text.splitlines(), start=1)
            if line.strip()
        ]
        job = create_job(
            job_id=next_job_id(job_records),
            job_name=job_name,
            chemicals=chemicals,
            steps=steps,
            created_by=created_by,
        )
        if str(area).strip():
            job["area"] = str(area).strip()
        job_records.append(job)
        st.session_state[ACTIVE_JOB_KEY] = str(job["job_id"])
        st.session_state[NEW_JOB_KEY] = False
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))


# --------------------------------------------------------------------------- #
# Job detail
# --------------------------------------------------------------------------- #


def render_job_detail_page(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
    *,
    vector_store: Any = None,
) -> None:
    """Render the ten-stage job detail page."""
    job = get_job(job_records, str(st.session_state.get(ACTIVE_JOB_KEY, "")))
    if job is None:
        st.session_state[ACTIVE_JOB_KEY] = None
        st.warning("未找到当前作业，请返回作业列表。")
        if st.button("返回作业列表", key="job_back_missing"):
            st.rerun()
        return

    if st.button("← 返回作业列表", key="job_back"):
        st.session_state[ACTIVE_JOB_KEY] = None
        st.rerun()

    _render_job_header(job)
    _render_progress(job)
    st.divider()

    _render_stage_basics(job)
    _render_stage_evidence(job, vector_store)
    _render_stage_jsa_draft(job_records, job)
    _render_stage_confirmation(job_records, job)
    _render_stage_approval(job_records, job)
    _render_stage_execution(job_records, job)
    _render_stage_hazards(job_records, hazard_records, job)
    _render_stage_rectification(job_records, hazard_records, job)
    _render_stage_review(job_records, hazard_records, job)
    _render_stage_closure(job_records, hazard_records, job)
    _render_tech_details(job)


def _render_job_header(job: Mapping[str, object]) -> None:
    st.title(str(job.get("job_name", "作业详情")))
    chemicals = "、".join(
        str(item.get("name", ""))
        for item in job.get("chemicals") or ()
        if str(item.get("name", "")).strip()
    ) or "—"
    columns = st.columns(5)
    columns[0].metric("作业编号", str(job.get("job_id", "")))
    columns[1].metric("化学品", chemicals)
    columns[2].metric("当前风险（确认后）", job_risk_label(job))
    columns[3].metric("当前状态", str(job.get("status", "")))
    columns[4].metric("下一步", next_action_label(job))
    data_label = str(job.get("data_label", "")).strip()
    if data_label:
        st.caption(f"数据性质：{data_label}｜更新时间：{job.get('updated_at', '')}")


def _render_progress(job: Mapping[str, object]) -> None:
    st.markdown("#### 业务进度")
    status = str(job.get("status", ""))
    current = JOB_FLOW.index(status) if status in JOB_FLOW else -1
    columns = st.columns(len(JOB_FLOW))
    for index, (column, label) in enumerate(zip(columns, JOB_FLOW)):
        if status == JOB_STATUS_REJECTED:
            mark = "⛔" if label == JOB_STATUS_AWAITING_APPROVAL else (
                "✅" if index < JOB_FLOW.index(JOB_STATUS_AWAITING_APPROVAL) else "○"
            )
        elif index < current:
            mark = "✅"
        elif index == current:
            mark = "🔵"
        else:
            mark = "○"
        column.markdown(f"**{mark}**\n\n{label}")
    if status == JOB_STATUS_REJECTED:
        st.error("作业单已被驳回，流程终止；如需继续请重新建单。")


def _render_stage_basics(job: Mapping[str, object]) -> None:
    with _stage(1, "基础信息", "done"):
        if str(job.get("area", "")).strip():
            st.markdown(f"**作业区域**：{job['area']}")
        steps = list(job.get("steps") or ())
        if steps:
            st.markdown("**作业步骤**")
            st.dataframe(
                [
                    {
                        "步骤": step.get("order", index),
                        "名称": step.get("name", ""),
                        "说明": step.get("note", ""),
                    }
                    for index, step in enumerate(steps, start=1)
                ],
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.caption("尚未填写作业步骤。")
        if job.get("chemicals"):
            st.markdown("**化学品**")
            st.dataframe(list(job["chemicals"]), hide_index=True, use_container_width=True)


def _render_stage_evidence(job: Mapping[str, object], vector_store: Any) -> None:
    from job_review import attach_sds_evidence

    has_public = bool(job.get("public_evidence"))
    has_sds = bool(job.get("sds_evidence"))
    state = _stage_state(
        job,
        done=has_public or has_sds,
        active=str(job.get("status", "")) == JOB_STATUS_DRAFT,
    )
    with _stage(2, "安全证据", state):
        st.markdown("##### 【公开安全证据】非 SDS")
        st.caption(
            "来自 NIOSH / OSHA 等公开资料的逐字引用，仅用于演示与人工交叉核对，不替代 SDS。"
        )
        public_rows = _public_evidence_rows(job)
        if public_rows:
            st.dataframe(
                public_rows,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "原始链接": st.column_config.LinkColumn(
                        "原始链接", display_text="打开来源"
                    )
                },
            )
        else:
            st.info("尚未挂接公开安全证据。")

        st.markdown("##### 【SDS 证据】仅来自用户上传的合法 SDS")
        sds_rows = _sds_evidence_rows(job)
        if sds_rows:
            st.dataframe(sds_rows, hide_index=True, use_container_width=True)
        elif vector_store is None:
            st.warning(
                "尚未上传 SDS：公开安全证据不能替代 SDS，因此本作业无法生成 SDS 结论。"
                "请到「工具箱 → SDS资料库」上传合法 SDS 后再提取。"
            )
        else:
            st.caption("已检测到 SDS 知识库，可从已上传 SDS 中提取证据。")
            if st.button(
                "从已上传 SDS 中提取证据",
                key=f"sds_extract_{job.get('job_id', '')}",
            ):
                snapshots = _extract_sds_snapshots(vector_store, job)
                if not snapshots:
                    st.warning("未检索到可用的 SDS 片段，请检查上传文件内容。")
                else:
                    attach_sds_evidence(job, snapshots)
                    st.rerun()


def _render_stage_jsa_draft(
    job_records: MutableSequence[dict[str, object]],
    job: dict[str, object],
) -> None:
    draft = job.get("jsa_draft")
    status = str(job.get("status", ""))
    state = _stage_state(
        job, done=bool(draft), active=status == JOB_STATUS_DRAFT
    )
    with _stage(3, "JSA草稿", state):
        if status == JOB_STATUS_DRAFT:
            st.caption("AI 将依据作业步骤与已挂接证据生成草稿，不会猜测 L/S 或风险值。")
            if st.button(
                "生成 JSA 草稿",
                type="primary",
                key=f"jsa_draft_{job.get('job_id', '')}",
            ):
                try:
                    draft_jsa(job_records, str(job.get("job_id", "")))
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        if not draft:
            st.caption("尚未生成 JSA 草稿。")
            return

        st.markdown("**AI生成草稿**")
        st.caption("以下内容为证据候选草稿，未经 EHS 确认不得作为正式 JSA。")
        open_items = list(draft.get("open_items") or ())
        if open_items:
            st.warning("待EHS确认项：\n\n" + "\n".join(f"- {item}" for item in open_items))
        groups = (
            ("危害候选", "hazard_candidates"),
            ("后果候选", "consequence_candidates"),
            ("控制措施候选", "control_candidates"),
            ("参考证据", "reference_candidates"),
        )
        for title, key in groups:
            candidates = list(draft.get(key) or ())
            st.markdown(f"**{title}（{len(candidates)}）**")
            if not candidates:
                st.caption("无")
                continue
            st.dataframe(
                [
                    {
                        "来源轨道": item.get("track_label", ""),
                        "主题": item.get("topic", "") or item.get("section", ""),
                        "证据": item.get("source_title", "")
                        or item.get("source", ""),
                        "内容": item.get("text", ""),
                    }
                    for item in candidates
                ],
                hide_index=True,
                use_container_width=True,
            )


def _render_stage_confirmation(
    job_records: MutableSequence[dict[str, object]],
    job: dict[str, object],
) -> None:
    status = str(job.get("status", ""))
    confirmation = dict(job.get("jsa_confirmation") or {})
    state = _stage_state(
        job,
        done=bool(confirmation),
        active=status == JOB_STATUS_AWAITING_EHS,
    )
    with _stage(4, "EHS人工确认", state):
        if status == JOB_STATUS_AWAITING_EHS:
            draft = dict(job.get("jsa_draft") or {})
            suggested = dict(draft.get("suggested") or {})
            st.caption(
                "请逐项确认或修改；L/S 由 EHS 按现场条件填写，风险值由系统按 R=L×S 计算。"
            )
            with st.form(f"confirm_form_{job.get('job_id', '')}"):
                job_step = st.text_area(
                    "作业步骤", value=str(suggested.get("job_step", "")), height=80
                )
                hazard = st.text_area(
                    "危害描述", value=str(suggested.get("hazard", "")), height=80
                )
                consequence = st.text_area(
                    "可能后果", value=str(suggested.get("consequence", "")), height=80
                )
                controls = st.text_area(
                    "控制措施",
                    value=str(suggested.get("suggested_controls", "")),
                    height=80,
                )
                existing_controls = st.text_area(
                    "现有控制措施",
                    value=str(suggested.get("existing_controls", "")),
                    height=60,
                )
                risk_columns = st.columns(4)
                likelihood = risk_columns[0].select_slider(
                    "可能性 L", options=range(1, 6), value=3
                )
                severity = risk_columns[1].select_slider(
                    "严重度 S", options=range(1, 6), value=3
                )
                residual_l = risk_columns[2].select_slider(
                    "控制后 L", options=range(1, 6), value=2
                )
                residual_s = risk_columns[3].select_slider(
                    "控制后 S", options=range(1, 6), value=3
                )
                change_note = st.text_area(
                    "修改说明", value="", height=60
                )
                confirmed_by = st.text_input("确认人", value="")
                submitted = st.form_submit_button(
                    "提交 EHS 确认", type="primary", use_container_width=True
                )
            if submitted:
                try:
                    confirm_jsa(
                        job_records,
                        str(job.get("job_id", "")),
                        confirmed_by=confirmed_by,
                        job_step=job_step,
                        hazard=hazard,
                        consequence=consequence,
                        suggested_controls=controls,
                        existing_controls=existing_controls,
                        likelihood=likelihood,
                        severity=severity,
                        residual_likelihood=residual_l,
                        residual_severity=residual_s,
                        change_note=change_note,
                    )
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            return

        if not confirmation:
            st.caption("需先由 AI 生成 JSA 草稿，再进入 EHS 人工确认。")
            return

        st.markdown("**EHS人工确认结果**（与 AI 草稿分开保存）")
        final = dict(confirmation.get("final") or {})
        ai_draft = dict(confirmation.get("ai_draft") or {})
        suggested = dict(ai_draft.get("suggested") or {})
        columns = st.columns(2)
        with columns[0]:
            st.markdown("**AI原始内容（草稿）**")
            st.json(suggested)
        with columns[1]:
            st.markdown("**人工最终内容**")
            st.json(final)
        st.caption(
            f"确认人：{confirmation.get('confirmed_by', '')}｜"
            f"确认时间：{confirmation.get('confirmed_at', '')}｜"
            f"修改说明：{confirmation.get('change_note', '') or '无'}"
        )
        changed = dict(confirmation.get("changed_fields") or {})
        if changed:
            st.markdown("**人工修改明细**")
            st.dataframe(
                [
                    {
                        "字段": field,
                        "AI草稿": str(value.get("ai_draft", "")),
                        "人工最终": str(value.get("final", "")),
                    }
                    for field, value in changed.items()
                ],
                hide_index=True,
                use_container_width=True,
            )


def _render_stage_approval(
    job_records: MutableSequence[dict[str, object]],
    job: dict[str, object],
) -> None:
    status = str(job.get("status", ""))
    approved = status in {
        JOB_STATUS_APPROVED,
        JOB_STATUS_EXECUTING,
        JOB_STATUS_AWAITING_REVIEW,
        JOB_STATUS_CLOSED,
    }
    state = _stage_state(
        job,
        done=approved or status == JOB_STATUS_REJECTED,
        active=status == JOB_STATUS_AWAITING_APPROVAL,
    )
    with _stage(5, "审批", state):
        if status == JOB_STATUS_AWAITING_APPROVAL:
            final = dict((job.get("jsa_confirmation") or {}).get("final") or {})
            st.markdown(
                f"**待审批作业**：{job.get('job_name', '')}｜"
                f"风险 {final.get('风险值R', '')}·{final.get('风险等级', '')}｜"
                f"残余风险 {final.get('残余风险R', '')}·{final.get('残余风险等级', '')}"
            )
            st.caption("批准后方可执行；未批准或驳回的作业不能进入执行。")
            actor = st.text_input(
                "审批人", key=f"approval_actor_{job.get('job_id', '')}"
            )
            cards = st.columns(3, border=True)
            with cards[0]:
                st.markdown("**批准**")
                st.caption("按 EHS 确认后的版本直接批准。")
                if st.button(
                    "✅ 批准",
                    type="primary",
                    key=f"approve_{job.get('job_id', '')}",
                    use_container_width=True,
                ):
                    _decide(job_records, job, actor, ACTION_APPROVE, note="同意执行")
            with cards[1]:
                st.markdown("**修改后批准**")
                st.caption("只能附加审批条件 / 备注，不能修改 JSA 与风险值。")
                with st.form(f"modify_form_{job.get('job_id', '')}"):
                    modify_note = st.text_area("审批条件 / 备注", height=70)
                    if st.form_submit_button("✏️ 修改后批准", use_container_width=True):
                        _decide(
                            job_records,
                            job,
                            actor,
                            ACTION_MODIFY,
                            note=modify_note,
                        )
            with cards[2]:
                st.markdown("**驳回**")
                st.caption("驳回后流程终止，不能执行。")
                with st.form(f"reject_form_{job.get('job_id', '')}"):
                    reject_note = st.text_area("驳回原因", height=70)
                    if st.form_submit_button("⛔ 驳回", use_container_width=True):
                        _decide(
                            job_records,
                            job,
                            actor,
                            ACTION_REJECT,
                            note=reject_note,
                        )
            return

        approvals = list(job.get("approvals") or ())
        if not approvals:
            st.caption("尚未产生审批记录。")
            return
        st.dataframe(
            [
                {
                    "操作": item.get("operation_label", ""),
                    "处置": item.get("action_label", ""),
                    "操作人": item.get("actor", ""),
                    "时间": item.get("decided_at", ""),
                    "备注": item.get("note", ""),
                    "Guardrail": "、".join(item.get("guard_codes") or ()),
                }
                for item in approvals
            ],
            hide_index=True,
            use_container_width=True,
        )


def _decide(
    job_records: MutableSequence[dict[str, object]],
    job: dict[str, object],
    actor: str,
    action: str,
    *,
    note: str = "",
) -> None:
    request = build_job_approval_request(job)
    calls: tuple[dict[str, Any], ...] = ()
    if action == ACTION_MODIFY:
        calls = (
            {
                "call_id": request.gate_id,
                "tool": "approve_job",
                "arguments": {"note": note},
            },
        )
    decision = ApprovalDecision(
        gate_id=request.gate_id,
        action=action,
        calls=calls,
        note=note,
    )
    try:
        decide_job_approval(
            job_records,
            str(job.get("job_id", "")),
            decision,
            actor=actor or "未填写审批人",
        )
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))


def _render_stage_execution(
    job_records: MutableSequence[dict[str, object]],
    job: dict[str, object],
) -> None:
    status = str(job.get("status", ""))
    started = bool((job.get("execution_info") or {}).get("started_at"))
    finished = bool((job.get("execution_info") or {}).get("completed_at"))
    state = _stage_state(
        job,
        done=finished,
        active=status in {JOB_STATUS_APPROVED, JOB_STATUS_EXECUTING},
    )
    with _stage(6, "执行", state):
        if status == JOB_STATUS_APPROVED:
            st.caption("作业已批准，可开始执行并记录执行人。")
            executor = st.text_input(
                "执行人", key=f"executor_{job.get('job_id', '')}"
            )
            if st.button(
                "开始执行",
                type="primary",
                key=f"start_exec_{job.get('job_id', '')}",
            ):
                try:
                    start_job_execution(
                        job_records,
                        str(job.get("job_id", "")),
                        actor=executor or "未填写执行人",
                    )
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        elif status == JOB_STATUS_EXECUTING:
            info = dict(job.get("execution_info") or {})
            st.caption(
                f"执行人：{info.get('executor', '')}｜开始时间：{info.get('started_at', '')}"
            )
            completed_on = st.date_input(
                "实际完成日期",
                value=date.today(),
                key=f"completed_on_{job.get('job_id', '')}",
            )
            note = st.text_area(
                "执行小结", height=70, key=f"exec_note_{job.get('job_id', '')}"
            )
            if st.button(
                "执行完成",
                type="primary",
                key=f"complete_exec_{job.get('job_id', '')}",
            ):
                try:
                    complete_job_execution(
                        job_records,
                        str(job.get("job_id", "")),
                        actor=info.get("executor", "") or "执行人",
                        completed_at=datetime.fromisoformat(completed_on.isoformat()),
                        note=note,
                    )
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        elif started:
            info = dict(job.get("execution_info") or {})
            st.dataframe(
                [
                    {
                        "执行人": info.get("executor", ""),
                        "开始时间": info.get("started_at", ""),
                        "完成时间": info.get("completed_at", ""),
                        "执行小结": info.get("completion_note", ""),
                    }
                ],
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.caption("审批通过后，此处可开始执行。")


def _render_stage_hazards(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
    job: dict[str, object],
) -> None:
    workable = str(job.get("status", "")) in {
        JOB_STATUS_EXECUTING,
        JOB_STATUS_AWAITING_REVIEW,
    }
    state = _stage_state(job, done=False, active=workable)
    with _stage(7, "关联隐患", state):
        rows = _hazard_rows(job, hazard_records)
        if rows:
            st.dataframe(rows, hide_index=True, use_container_width=True)
        else:
            st.caption("暂无关联隐患。执行中发现隐患时在此创建。")

        if not workable:
            st.caption("执行开始后，可在此创建关联隐患。")
            return

        st.markdown("**从 JSA 危害生成隐患 / 人工新增**")
        drafts = draft_hazards_from_jsa(job)
        options = ["手动新增（不引用JSA草稿）"] + [
            f"{index}. {str(item.get('description', ''))[:50]}"
            for index, item in enumerate(drafts, start=1)
        ]
        choice = st.selectbox(
            "隐患来源", options, key=f"hazard_source_{job.get('job_id', '')}"
        )
        selected_draft = (
            drafts[options.index(choice) - 1]
            if options.index(choice) > 0
            else None
        )
        if selected_draft is not None:
            st.caption(
                "来源证据："
                + str(selected_draft["source_evidence"].get("source_title", ""))
                + "（AI草稿，需审批后写入台账）"
            )
        with st.form(f"hazard_form_{job.get('job_id', '')}"):
            description = st.text_area(
                "问题描述",
                value=str(selected_draft.get("description", "")) if selected_draft else "",
                height=80,
            )
            first = st.columns(2)
            risk_level = first[0].selectbox("风险等级", RISK_LEVELS, index=2)
            owner = first[1].text_input("责任人", value="")
            second = st.columns(2)
            due_on = second[0].date_input("整改期限", value=date.today() + timedelta(days=14))
            corrective_action = st.text_area("整改措施", value="", height=60)
            third = st.columns(2)
            actor = third[0].text_input("发现/创建人", value="执行人（模拟）")
            approver = third[1].text_input("审批人", value="EHS审批（模拟）")
            submitted = st.form_submit_button(
                "审批并创建关联隐患", type="primary", use_container_width=True
            )
        if submitted:
            generated_from = (
                {
                    "source": "jsa_draft",
                    **dict(selected_draft.get("source_evidence") or {}),
                }
                if selected_draft
                else None
            )
            try:
                create_job_hazard(
                    job_records,
                    hazard_records,
                    str(job.get("job_id", "")),
                    description=description,
                    risk_level=risk_level,
                    hazard_type="其他",
                    owner=owner or "待分配（执行中发现）",
                    due_on=due_on,
                    corrective_action=corrective_action,
                    actor=actor or "执行人",
                    approved_by=approver,
                    generated_from=generated_from,
                )
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


def _render_stage_rectification(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
    job: dict[str, object],
) -> None:
    workable = str(job.get("status", "")) in {
        JOB_STATUS_EXECUTING,
        JOB_STATUS_AWAITING_REVIEW,
    }
    with _stage(8, "整改证据", _stage_state(job, done=False, active=workable)):
        open_hazards = [
            hazard
            for hazard in (
                find_hazard(hazard_records, str(hid))
                for hid in job.get("linked_hazard_ids") or ()
            )
            if hazard is not None and str(hazard.get("状态", "")) != "已关闭"
        ]
        if not open_hazards:
            st.caption("当前没有待整改的关联隐患。")
            return
        if not workable:
            st.caption("执行开始后，可在此指派责任人与上传整改证据。")
            return

        for hazard in open_hazards:
            identifier = str(hazard.get("隐患编号", ""))
            evidence_count = len(list(hazard.get("rectification_evidence") or ()))
            with st.expander(
                f"{identifier}｜{str(hazard.get('隐患描述', ''))[:40]}｜"
                f"{hazard.get('状态', '')}｜证据 {evidence_count} 条"
            ):
                st.caption(
                    f"责任人：{hazard.get('责任人', '')}｜"
                    f"整改期限：{hazard.get('整改期限', '')}｜"
                    f"整改措施：{hazard.get('整改措施', '')}"
                )
                with st.form(f"assign_form_{identifier}"):
                    st.markdown("**指定责任人与期限**")
                    first = st.columns(2)
                    owner = first[0].text_input(
                        "责任人", value=str(hazard.get("责任人", ""))
                    )
                    due_on = first[1].date_input(
                        "整改期限",
                        value=date.fromisoformat(str(hazard.get("整改期限"))),
                        key=f"assign_due_{identifier}",
                    )
                    corrective_action = st.text_area(
                        "整改措施", value=str(hazard.get("整改措施", "")), height=60
                    )
                    rectification_note = st.text_area(
                        "整改说明", value="", height=60
                    )
                    actor = st.text_input("操作人", value="EHS（模拟）")
                    if st.form_submit_button("更新整改信息", use_container_width=True):
                        try:
                            update_hazard_rectification(
                                job_records,
                                hazard_records,
                                str(job.get("job_id", "")),
                                identifier,
                                actor=actor or "EHS",
                                owner=owner,
                                due_on=due_on,
                                corrective_action=corrective_action,
                                rectification_note=rectification_note,
                            )
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))

                st.markdown("**上传整改证据**")
                uploaded = st.file_uploader(
                    "选择整改证据文件（Demo 仅记录文件信息）",
                    key=f"evidence_file_{identifier}",
                )
                with st.form(f"evidence_form_{identifier}"):
                    first = st.columns(2)
                    evidence_type = first[0].selectbox(
                        "证据类型", RECTIFICATION_EVIDENCE_TYPES
                    )
                    uploader = first[1].text_input("上传人", value="")
                    note = st.text_input("说明", value="")
                    if st.form_submit_button("上传整改证据", use_container_width=True):
                        try:
                            submit_rectification_evidence(
                                job_records,
                                hazard_records,
                                str(job.get("job_id", "")),
                                identifier,
                                file_name=uploaded.name if uploaded else "",
                                uploaded_by=uploader,
                                evidence_type=evidence_type,
                                note=note,
                            )
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))


def _render_stage_review(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
    job: dict[str, object],
) -> None:
    workable = str(job.get("status", "")) in {
        JOB_STATUS_EXECUTING,
        JOB_STATUS_AWAITING_REVIEW,
    }
    with _stage(9, "EHS复查", _stage_state(job, done=False, active=workable)):
        open_hazards = [
            hazard
            for hazard in (
                find_hazard(hazard_records, str(hid))
                for hid in job.get("linked_hazard_ids") or ()
            )
            if hazard is not None and str(hazard.get("状态", "")) != "已关闭"
        ]
        if not open_hazards:
            st.caption("当前没有待复查的关联隐患。")
            return
        if not workable:
            st.caption("执行开始后，可在此进行 EHS 复查与关闭。")
            return

        for hazard in open_hazards:
            identifier = str(hazard.get("隐患编号", ""))
            with st.container(border=True):
                st.markdown(f"**{identifier}**｜{hazard.get('隐患描述', '')}")
                evidence = list(hazard.get("rectification_evidence") or ())
                if evidence:
                    st.dataframe(
                        [
                            {
                                "文件": item.get("file_name", ""),
                                "类型": item.get("evidence_type", ""),
                                "说明": item.get("note", ""),
                                "上传人": item.get("uploaded_by", ""),
                                "上传时间": item.get("uploaded_at", ""),
                            }
                            for item in evidence
                        ],
                        hide_index=True,
                        use_container_width=True,
                    )
                else:
                    st.warning("该隐患尚无整改证据，不能关闭。")

                with st.form(f"review_form_{identifier}"):
                    first = st.columns(2)
                    reviewer = first[0].text_input(
                        "复查人", value=str(hazard.get("reviewer", ""))
                    )
                    review_date = first[1].date_input(
                        "复查日期", value=date.today(), key=f"review_date_{identifier}"
                    )
                    review_note = st.text_area(
                        "复查意见",
                        value=str(hazard.get("review_note", "")),
                        height=60,
                    )
                    if st.form_submit_button("提交复查", use_container_width=True):
                        try:
                            review_hazard(
                                job_records,
                                hazard_records,
                                str(job.get("job_id", "")),
                                identifier,
                                reviewer=reviewer,
                                review_note=review_note,
                                review_date=review_date,
                            )
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))

                blockers = close_blockers(hazard)
                if blockers:
                    st.info("暂时不能关闭：" + "；".join(blockers))
                    st.button(
                        "关闭隐患",
                        disabled=True,
                        key=f"close_disabled_{identifier}",
                        use_container_width=True,
                    )
                elif st.button(
                    "关闭隐患",
                    type="primary",
                    key=f"close_hazard_{identifier}",
                    use_container_width=True,
                ):
                    try:
                        close_hazard(
                            job_records,
                            hazard_records,
                            str(job.get("job_id", "")),
                            identifier,
                            closed_by=str(hazard.get("reviewer", "")),
                        )
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))


def _render_stage_closure(
    job_records: MutableSequence[dict[str, object]],
    hazard_records: MutableSequence[dict[str, object]],
    job: dict[str, object],
) -> None:
    status = str(job.get("status", ""))
    blockers = job_close_blockers(job, hazard_records)
    state = _stage_state(
        job,
        done=status == JOB_STATUS_CLOSED,
        active=status == JOB_STATUS_AWAITING_REVIEW,
    )
    with _stage(10, "作业关闭", state):
        if status == JOB_STATUS_CLOSED:
            info = dict(job.get("closure_info") or {})
            st.success(
                f"作业已关闭｜关闭人：{info.get('closed_by', '')}｜"
                f"关闭时间：{info.get('closed_at', '')}｜"
                f"关联隐患：{info.get('linked_hazard_count', 0)} 条"
            )
            return
        if status != JOB_STATUS_AWAITING_REVIEW:
            st.caption("执行完成后进入「待复查」，全部关联隐患关闭后方可关闭作业。")
            return
        if blockers:
            st.warning("暂时不能关闭作业：" + "；".join(blockers))
        else:
            st.success("全部关联隐患已关闭，可以关闭作业。")
        with st.form(f"close_job_form_{job.get('job_id', '')}"):
            actor = st.text_input("关闭人", value="")
            note = st.text_area("闭环说明", value="", height=60)
            if st.form_submit_button(
                "关闭作业",
                type="primary",
                disabled=bool(blockers),
                use_container_width=True,
            ):
                try:
                    close_job(
                        job_records,
                        hazard_records,
                        str(job.get("job_id", "")),
                        actor=actor or "EHS",
                        note=note,
                    )
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


def _render_tech_details(job: Mapping[str, object]) -> None:
    with st.expander("技术详情 / 执行轨迹", expanded=False):
        st.markdown("**状态流转记录**")
        st.dataframe(
            [
                {
                    "从": item.get("from", ""),
                    "到": item.get("to", ""),
                    "操作人": item.get("actor", ""),
                    "说明": item.get("note", ""),
                    "时间": item.get("at", ""),
                }
                for item in job.get("status_history") or ()
            ],
            hide_index=True,
            use_container_width=True,
        )
        st.markdown("**审批与 Guardrail 记录**")
        approvals = list(job.get("approvals") or ())
        if approvals:
            st.dataframe(approvals, hide_index=True, use_container_width=True)
        else:
            st.caption("暂无审批记录。")
        st.markdown("**原始状态（JSON）**")
        st.code(json.dumps(job, ensure_ascii=False, indent=2, default=str), language="json")


__all__ = [
    "ACTIVE_JOB_KEY",
    "JOB_FLOW",
    "NEW_JOB_KEY",
    "close_blockers",
    "find_hazard",
    "job_close_blockers",
    "job_owner_label",
    "job_risk_label",
    "load_hf_case",
    "next_action_label",
    "render_job_detail_page",
    "render_workbench_page",
]

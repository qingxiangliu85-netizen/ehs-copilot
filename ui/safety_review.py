"""Four-step Safety Review Pack workbench embedded in 作业许可."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping

import streamlit as st

import evidence_adapter
import safety_review
from services import permit_service, safety_review_service
from workflow import permissions
from workflow.safety_review_graph import (
    SafetyReviewContext,
    precheck_draft,
    run_safety_review,
)

from . import common, records, widgets


ACTIVE_DRAFT_KEY = "_safety_review_active_draft"
STEP_KEY = "_safety_review_step"
RESOURCES_KEY = "_safety_review_resources"
DEMO_PREFILL_KEY = "_safety_review_demo_prefill"
PRECHECK_KEY = "_safety_review_prechecks"

STEP_LABELS = (
    "1 填写作业",
    "2 AI信息预检",
    "3 准备资料",
    "4 生成并审核",
)


def open_draft(draft_id: str) -> None:
    st.session_state[ACTIVE_DRAFT_KEY] = str(draft_id)
    st.session_state[STEP_KEY] = 1


def close_workspace() -> None:
    st.session_state.pop(ACTIVE_DRAFT_KEY, None)
    st.session_state[STEP_KEY] = 1


def _active_draft(connection: sqlite3.Connection) -> dict[str, Any] | None:
    draft_id = str(st.session_state.get(ACTIVE_DRAFT_KEY, ""))
    return safety_review_service.get_work_draft(connection, draft_id) if draft_id else None


def _set_step(step: int) -> None:
    st.session_state[STEP_KEY] = min(max(int(step), 1), 4)


def _resource_bucket(draft_id: str) -> list[dict[str, Any]]:
    resources = st.session_state.setdefault(RESOURCES_KEY, {})
    return resources.setdefault(str(draft_id), [])


def _render_step_header(step: int) -> None:
    columns = st.columns(4)
    for index, label in enumerate(STEP_LABELS, start=1):
        columns[index - 1].markdown(f"**{label}**" if index == step else label)
    st.progress(step / 4)


def _apply_demo_prefill() -> None:
    st.session_state[DEMO_PREFILL_KEY] = True


def _default(field: str, draft: Mapping[str, Any] | None) -> Any:
    if draft:
        return draft.get(field)
    if st.session_state.get(DEMO_PREFILL_KEY):
        values = {
            "title": "HF酸洗设备检维修（模拟）",
            "work_type": "危化品作业",
            "description": "酸洗设备停机后由承包商进行管路拆卸、残液处理与检维修，涉及氢氟酸（HF）暴露风险。模拟数据，不作为真实作业SOP。",
            "location": "酸洗区（模拟区域）",
            "people_count": 2,
            "responsible_person": "现场负责人（Demo）",
            "contractor_involved": True,
            "work_steps": ["确认停机并隔离设备", "排空残液并拆卸管路", "检维修后恢复现场"],
            "chemicals": ["氢氟酸"],
            "user_risk_tags": ["化学品暴露", "腐蚀/灼伤", "环境泄漏", "承包商作业"],
        }
        return values.get(field)
    defaults = {
        "work_type": "其他高风险作业",
        "people_count": 1,
        "contractor_involved": False,
        "work_steps": [],
        "chemicals": [],
        "user_risk_tags": [],
    }
    return defaults.get(field, "")


def _render_intake(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    draft: Mapping[str, Any] | None,
) -> None:
    st.markdown("#### 1. 填写真实作业")
    st.caption("Demo仅负责预填表单；所有字段均可修改。示例不代表真实企业作业SOP。")
    if draft is None:
        st.button("预填HF酸洗Demo", on_click=_apply_demo_prefill)
    prefix = str((draft or {}).get("id") or "new")
    start_default = datetime.now().replace(second=0, microsecond=0) + timedelta(days=1)
    end_default = start_default + timedelta(hours=4)
    with st.form(f"safety_review_intake_{prefix}"):
        first = st.columns([1.3, 2.2])
        work_type = first[0].selectbox(
            "主作业类型",
            safety_review.WORK_TYPES,
            index=safety_review.WORK_TYPES.index(str(_default("work_type", draft))),
        )
        title = first[1].text_input("作业名称", value=str(_default("title", draft) or ""))
        description = st.text_area(
            "自由描述",
            value=str(_default("description", draft) or ""),
            height=100,
            placeholder="说明要做什么、设备状态、作业边界及特殊条件。",
        )
        work_steps = st.text_area(
            "作业步骤（每行一步）",
            value="\n".join(_default("work_steps", draft) or ()),
            height=100,
        )
        second = st.columns([1.8, 1.2, 1.2, 1.3])
        location = second[0].text_input("地点", value=str(_default("location", draft) or ""))
        people_count = second[1].number_input(
            "作业人数", min_value=1, max_value=100, value=int(_default("people_count", draft) or 1)
        )
        responsible = second[2].text_input(
            "负责人", value=str(_default("responsible_person", draft) or "")
        )
        contractor = second[3].checkbox(
            "涉及承包商", value=bool(_default("contractor_involved", draft))
        )
        third = st.columns(2)
        start_text = third[0].text_input(
            "计划开始时间",
            value=str((draft or {}).get("planned_start") or start_default.isoformat(timespec="minutes")),
        )
        end_text = third[1].text_input(
            "计划结束时间",
            value=str((draft or {}).get("planned_end") or end_default.isoformat(timespec="minutes")),
        )
        chemicals = st.text_input(
            "涉及化学品（多个用顿号或逗号分隔）",
            value="、".join(_default("chemicals", draft) or ()),
        )
        risk_tags = st.multiselect(
            "用户初选风险标签",
            safety_review.RISK_TAGS,
            default=list(_default("user_risk_tags", draft) or ()),
        )
        submitted = st.form_submit_button("保存并进入信息预检", type="primary")
    if not submitted:
        return
    chemical_values = [
        item.strip() for item in chemicals.replace(",", "、").replace("，", "、").split("、") if item.strip()
    ]
    payload = {
        "title": title,
        "work_type": work_type,
        "description": description,
        "location": location,
        "planned_start": start_text,
        "planned_end": end_text,
        "people_count": int(people_count),
        "responsible_person": responsible,
        "contractor_involved": contractor,
        "work_steps": [line.strip() for line in work_steps.splitlines() if line.strip()],
        "chemicals": chemical_values,
        "user_risk_tags": risk_tags,
    }
    try:
        if draft is None:
            saved = safety_review_service.create_work_draft(connection, user=user, **payload)
            st.session_state[ACTIVE_DRAFT_KEY] = saved["id"]
        else:
            saved = safety_review_service.update_work_draft(
                connection, str(draft["id"]), user=user, **payload
            )
    except Exception as exc:  # noqa: BLE001
        st.error(f"保存失败：{exc}")
        return
    st.session_state[DEMO_PREFILL_KEY] = False
    st.session_state.setdefault(PRECHECK_KEY, {}).pop(str(saved["id"]), None)
    _set_step(2)
    st.rerun()


def _render_precheck(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    draft: Mapping[str, Any],
) -> None:
    st.markdown("#### 2. AI信息预检")
    prechecks = st.session_state.setdefault(PRECHECK_KEY, {})
    result = prechecks.get(str(draft["id"]))
    if not isinstance(result, dict):
        result = precheck_draft(draft)
        prechecks[str(draft["id"])] = result
    mode_label = (
        "AI结构化预检"
        if result.get("mode") == "llm_structured"
        else "规则与语义检索模式"
    )
    st.info(f"当前：{mode_label}。AI只提出候选项，最终类型与风险标签由用户确认。")
    if result["missing_fields"]:
        st.warning("当前缺失：" + "、".join(result["missing_fields"]))
    with st.form(f"safety_precheck_{draft['id']}"):
        work_type = st.selectbox(
            "确认主作业类型",
            safety_review.WORK_TYPES,
            index=safety_review.WORK_TYPES.index(result["suggested_work_type"]),
        )
        combined_tags = list(
            dict.fromkeys(draft["user_risk_tags"] + result["suggested_risk_tags"])
        )
        tags = st.multiselect(
            "确认风险标签",
            safety_review.RISK_TAGS,
            default=combined_tags,
        )
        st.caption("AI建议标签：" + ("、".join(result["suggested_risk_tags"]) or "未识别"))
        submitted = st.form_submit_button("确认并准备资料", type="primary")
    if st.button("返回修改作业", key=f"precheck_back_{draft['id']}"):
        _set_step(1)
        st.rerun()
    if not submitted:
        return
    try:
        safety_review_service.update_work_draft(
            connection,
            str(draft["id"]),
            user=user,
            work_type=work_type,
            ai_risk_tags=result["suggested_risk_tags"],
            confirmed_risk_tags=tags,
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"确认失败：{exc}")
        return
    _set_step(3)
    st.rerun()


def _upload_resources(uploaded: Any, source_type: str, version: str) -> list[dict[str, Any]]:
    resources = []
    for item in uploaded or ():
        payload = item.getvalue()
        metadata = evidence_adapter.document_metadata(
            source_type=source_type,
            file_name=item.name,
            file_bytes=payload,
            version=version,
        )
        resources.append({**metadata, "file_bytes": payload, "chemical_name": ""})
    return resources


def _builtin_resource_options() -> dict[str, dict[str, Any]]:
    entries = {}
    for entry in evidence_adapter.demo_document_registry():
        label = (
            f"{entry.get('title') or entry.get('file_name')}"
            f"（{entry.get('source_nature') or entry.get('source_type')}）"
        )
        entries[label] = entry
    return entries


def _render_documents(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    draft: Mapping[str, Any],
) -> None:
    st.markdown("#### 3. 准备资料")
    st.caption(
        "SDS 与企业SOP都支持“选择内置资料”或“上传自己的文件”。文件只在当前会话解析；"
        "SQLite仅保存名称、版本、哈希等元数据和最终Citation快照。"
    )
    builtin_options = _builtin_resource_options()
    use_demo = st.checkbox(
        "选择当前会话的Synthetic SDS知识库",
        value=records.current_sds_knowledge_base() is not None,
        help="这是模拟资料，不替代真实化学品SDS。",
    )
    if use_demo and records.current_sds_knowledge_base() is None:
        st.warning("当前Synthetic SDS尚未加载；保存资料时将自动加载。")
    builtin_ids: list[str] = []
    if builtin_options:
        st.markdown("**内置资料库**")
        st.caption(
            "标注“真实公开 SDS”的是厂商/供应商官网获取的原始文件；"
            "标注“Synthetic Demo SOP”的是本仓库整理的模拟企业内部制度，"
            "不代表任何真实企业。"
        )
        selected_labels = st.multiselect(
            "选择内置SDS与企业SOP",
            list(builtin_options),
            help="SDS用于化学品证据；SOP用于作业流程与能量隔离证据。",
        )
        builtin_ids = [
            str(builtin_options[label]["document_id"]) for label in selected_labels
        ]
    with st.form(f"safety_documents_{draft['id']}"):
        version = st.text_input("本次上传资料版本", value="v1")
        sds_files = st.file_uploader("上传自己的SDS（PDF）", type=["pdf"], accept_multiple_files=True)
        sop_files = st.file_uploader(
            "上传自己的企业SOP/作业指导书（PDF/TXT/MD）",
            type=["pdf", "txt", "md"],
            accept_multiple_files=True,
        )
        internal_files = st.file_uploader(
            "上传其他内部资料（PDF/TXT/MD）",
            type=["pdf", "txt", "md"],
            accept_multiple_files=True,
        )
        submitted = st.form_submit_button("保存资料并进入审核包", type="primary")
    if st.button("返回信息预检", key=f"documents_back_{draft['id']}"):
        _set_step(2)
        st.rerun()
    if not submitted:
        resources = evidence_adapter.load_demo_resources(builtin_ids)
        if resources:
            st.markdown("**已选择资料**")
            st.dataframe(
                [
                    {
                        "资料": item.get("file_name"),
                        "来源性质": item.get("source_nature") or item.get("source_type"),
                        "类型": item.get("source_type"),
                        "版本": item.get("version"),
                        "来源": item.get("source_url") or item.get("issuer") or "—",
                    }
                    for item in resources
                ],
                hide_index=True,
                width="stretch",
            )
        existing = draft.get("documents") or ()
        if existing:
            st.dataframe(existing, hide_index=True, width="stretch")
        return
    resources = evidence_adapter.load_demo_resources(builtin_ids)
    resources += (
        _upload_resources(sds_files, "sds", version)
        + _upload_resources(sop_files, "sop", version)
        + _upload_resources(internal_files, "internal", version)
    )
    if use_demo:
        kb = records.load_demo_sds_for_workflow()
        resources.append(
            {
                "document_id": "DEMO-SDS-KB",
                "source_type": "sds",
                "file_name": kb.file_names[0],
                "version": "Synthetic Demo",
                "sha256": "session-cached-demo",
                "uploaded_at": safety_review.stamp(),
                "is_demo": True,
                "knowledge_base": kb,
            }
        )
    bucket = _resource_bucket(str(draft["id"]))
    bucket.clear()
    bucket.extend(resources)
    metadata = [
        {key: value for key, value in item.items() if key not in {"file_bytes", "knowledge_base"}}
        for item in resources
    ]
    try:
        safety_review_service.update_work_draft(
            connection, str(draft["id"]), user=user, documents=metadata
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"资料保存失败：{exc}")
        return
    _set_step(4)
    st.rerun()


def _stores_for(draft_id: str) -> tuple[evidence_adapter.EvidenceStores, tuple[dict[str, Any], ...]]:
    resources = tuple(_resource_bucket(draft_id))
    uploads = [item for item in resources if item.get("file_bytes")]
    custom = evidence_adapter.build_evidence_stores(uploads) if uploads else evidence_adapter.EvidenceStores()
    demo_kb = next((item.get("knowledge_base") for item in resources if item.get("knowledge_base")), None)
    return (
        evidence_adapter.EvidenceStores(
            sds=custom.sds or (demo_kb.vector_store if demo_kb else None),
            sop=custom.sop,
            internal=custom.internal,
        ),
        tuple({key: value for key, value in item.items() if key != "file_bytes"} for item in resources),
    )


def _render_pack_sections(pack: Mapping[str, Any]) -> None:
    st.markdown("### Safety Review Pack")
    widgets.render_chips(
        [
            (f"版本 v{pack.get('version', 0)}", common.NEUTRAL_CHIP),
            ("证据不足" if pack.get("evidence_insufficient") else "证据轨道已建立", None),
        ]
    )
    st.markdown("#### 作业摘要")
    st.write(pack.get("work_summary") or "—")
    sections = (
        ("风险识别", "risk_findings"),
        ("控制措施", "controls"),
        ("PPE", "ppe"),
        ("应急要求", "emergency_requirements"),
    )
    for label, key in sections:
        with st.expander(label, expanded=key in {"risk_findings", "controls"}):
            values = pack.get(key) or ()
            if not values:
                st.caption("未形成有证据支持的内容。")
            for item in values:
                refs = "、".join(item.get("evidence_refs") or ())
                st.markdown(f"- {item.get('text', '')}  `{refs or '待补证据'}`")
    with st.expander("证据来源", expanded=True):
        for item in pack.get("evidence") or ():
            source = item.get("source_name") or "用户输入"
            page = f"第{item.get('page')}页" if item.get("page") else item.get("locator", "")
            st.markdown(f"**{item.get('evidence_id')} · {source} · {page}**")
            st.caption(item.get("snippet") or "")
    with st.expander("资料冲突（Conflicts）", expanded=bool(pack.get("conflicts"))):
        conflicts = pack.get("conflicts") or ()
        if not conflicts:
            st.caption(
                pack.get("conflict_note") or "No unresolved evidence conflict detected."
            )
        for conflict in conflicts:
            if not isinstance(conflict, Mapping):
                continue
            state_label = (
                "已解决" if conflict.get("resolved") else "未解决（需人工处理）"
            )
            refs = "、".join(conflict.get("evidence_refs") or ())
            st.markdown(
                f"- **{conflict.get('conflict_id') or '—'}** · {conflict.get('description') or conflict.get('text', '')}"
                f"  \n`{state_label}` 证据：`{refs or '待补证据'}`"
            )
            selected = conflict.get("selected_version")
            if conflict.get("resolved") and selected:
                st.caption(f"人工处理：选择版本 {selected}；说明：{conflict.get('resolution_note') or '—'}")


def _review_jsa_and_save(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    draft: Mapping[str, Any],
    pack: Mapping[str, Any],
) -> None:
    st.markdown("#### EHS人工审核JSA")
    can_review = permissions.can(user, permissions.PERMIT_EHS_REVIEW)
    if not can_review:
        st.info("切换为EHS审核人身份后，可填写L/S、补充内容并确认审核包。")
        return
    edited: list[dict[str, Any]] = []
    evidence_ids = [
        str(item.get("evidence_id"))
        for item in (pack.get("evidence") or ())
        if item.get("evidence_id")
    ]
    conflict_resolutions: list[dict[str, str]] = []
    with st.form(f"review_pack_{draft['id']}_{pack.get('version', 0)}"):
        for index, item in enumerate(pack.get("jsa_draft") or (), start=1):
            st.markdown(f"**JSA {index}**")
            keep = st.checkbox("保留此项", value=True, key=f"keep_{draft['id']}_{index}")
            columns = st.columns(2)
            step = columns[0].text_input("作业步骤", value=str(item.get("work_step", "")), key=f"step_{draft['id']}_{index}")
            hazard = columns[1].text_input("危害", value=str(item.get("hazard", "")), key=f"haz_{draft['id']}_{index}")
            consequence = st.text_input("可能后果", value=str(item.get("consequence", "")), key=f"cons_{draft['id']}_{index}")
            controls = st.text_area("建议控制措施", value=str(item.get("proposed_controls", "")), key=f"ctrl_{draft['id']}_{index}", height=70)
            item_refs = st.multiselect(
                "证据引用（高/重大风险控制措施必须绑定SDS、SOP或内部资料）",
                evidence_ids,
                default=[ref for ref in (item.get("evidence_refs") or ()) if ref in evidence_ids],
                key=f"refs_{draft['id']}_{index}",
            )
            ratings = st.columns(4)
            likelihood = ratings[0].number_input("L", 1, 5, int(item.get("likelihood") or 1), key=f"l_{draft['id']}_{index}")
            severity = ratings[1].number_input("S", 1, 5, int(item.get("severity") or 1), key=f"s_{draft['id']}_{index}")
            residual_l = ratings[2].number_input("残余L", 1, 5, int(item.get("residual_likelihood") or 1), key=f"rl_{draft['id']}_{index}")
            residual_s = ratings[3].number_input("残余S", 1, 5, int(item.get("residual_severity") or 1), key=f"rs_{draft['id']}_{index}")
            if keep:
                base = {**item, "work_step": step, "hazard": hazard, "consequence": consequence, "proposed_controls": controls, "evidence_refs": item_refs}
                edited.append(
                    safety_review.apply_human_risk_rating(
                        base,
                        likelihood=int(likelihood),
                        severity=int(severity),
                        residual_likelihood=int(residual_l),
                        residual_severity=int(residual_s),
                        confirmed_by=permissions.user_id(user),
                    )
                )
        st.markdown("**新增人工JSA项（可选）**")
        add_manual_jsa = st.checkbox("添加一条人工JSA项")
        manual_jsa_step = manual_jsa_hazard = manual_jsa_consequence = ""
        manual_jsa_controls = ""
        manual_jsa_refs: list[str] = []
        manual_jsa_l = manual_jsa_s = manual_jsa_rl = manual_jsa_rs = 1
        if add_manual_jsa:
            manual_jsa_columns = st.columns(2)
            manual_jsa_step = manual_jsa_columns[0].text_input("人工项作业步骤")
            manual_jsa_hazard = manual_jsa_columns[1].text_input("人工项危害")
            manual_jsa_consequence = st.text_input("人工项可能后果")
            manual_jsa_controls = st.text_area("人工项建议控制措施", height=70)
            manual_jsa_refs = st.multiselect(
                "人工JSA项证据引用",
                evidence_ids,
                help="高/重大风险控制措施必须绑定SDS、SOP或内部资料证据。",
            )
            manual_ratings = st.columns(4)
            manual_jsa_l = manual_ratings[0].number_input("人工项L", 1, 5, 1)
            manual_jsa_s = manual_ratings[1].number_input("人工项S", 1, 5, 1)
            manual_jsa_rl = manual_ratings[2].number_input("人工项残余L", 1, 5, 1)
            manual_jsa_rs = manual_ratings[3].number_input("人工项残余S", 1, 5, 1)
        st.markdown("**人工补充（每行一项）**")
        supplements = st.columns(3)
        manual_controls = supplements[0].text_area("补充控制措施", height=80)
        manual_ppe = supplements[1].text_area("补充PPE", height=80)
        manual_emergency = supplements[2].text_area("补充应急要求", height=80)
        supplement_refs = st.multiselect(
            "人工补充内容证据引用",
            evidence_ids,
            help="新增的安全要求必须绑定当前审核包中的真实资料证据。",
        )
        if pack.get("conflicts"):
            st.markdown("**资料冲突处理**")
            for conflict_index, conflict in enumerate(pack.get("conflicts") or (), start=1):
                if not isinstance(conflict, Mapping):
                    continue
                st.markdown(f"冲突{conflict_index}：{conflict.get('description') or conflict.get('text', '')}")
                if conflict.get("kind") == "sds_version":
                    versions = [str(value) for value in (conflict.get("versions") or ())]
                    selected_version = st.selectbox(
                        f"冲突{conflict_index}：选择当前适用版本",
                        ["待处理", *versions],
                        key=f"conflict_version_{draft['id']}_{conflict_index}",
                    )
                    conflict_resolutions.append(
                        {"selected_version": selected_version, "index": str(conflict_index - 1)}
                    )
                else:
                    st.caption(
                        "此类冲突需要更换或补充匹配的资料后重新生成审核包；"
                        "在当前版本中无法通过人工说明解除。"
                    )
        conflict_note = st.text_area("资料冲突处理说明（如有）", height=60)
        submitted = st.form_submit_button("保存人工审核为新版本", type="primary")
    if not submitted:
        return
    updated = dict(pack)
    updated["jsa_draft"] = edited
    if add_manual_jsa and manual_jsa_step.strip() and manual_jsa_hazard.strip():
        manual_item = safety_review.make_jsa_item(
            step_no=len(edited) + 1,
            work_step=manual_jsa_step,
            hazard=manual_jsa_hazard,
            consequence=manual_jsa_consequence,
            proposed_controls=manual_jsa_controls,
            evidence_refs=manual_jsa_refs,
        )
        updated["jsa_draft"].append(
            safety_review.apply_human_risk_rating(
                manual_item,
                likelihood=int(manual_jsa_l),
                severity=int(manual_jsa_s),
                residual_likelihood=int(manual_jsa_rl),
                residual_severity=int(manual_jsa_rs),
                confirmed_by=permissions.user_id(user),
            )
        )
    for key, text in (
        ("controls", manual_controls), ("ppe", manual_ppe), ("emergency_requirements", manual_emergency)
    ):
        additions = [
            safety_review.make_pack_item(
                line,
                origin="human",
                evidence_refs=supplement_refs,
                requires_confirmation=False,
            )
            for line in text.splitlines() if line.strip()
        ]
        updated[key] = list(updated.get(key) or ()) + additions
    if updated.get("conflicts"):
        resolutions = {int(item["index"]): item["selected_version"] for item in conflict_resolutions}
        resolved_conflicts = []
        for index, item in enumerate(updated.get("conflicts") or ()):
            conflict = dict(item)
            if conflict.get("kind") == "sds_version" or "kind" not in conflict:
                selected = resolutions.get(index)
                resolved = bool(
                    selected not in {None, "待处理"} and conflict_note.strip()
                )
                conflict.update(
                    {
                        "resolved": resolved,
                        "status": "resolved" if resolved else "unresolved",
                        "selected_version": selected if resolved else "",
                        "resolution_note": conflict_note.strip(),
                    }
                )
            else:
                # Chemical mismatch (and similar) conflicts require corrected
                # evidence and a regenerated pack, not an in-place manual claim.
                conflict["resolved"] = False
                conflict["status"] = "unresolved"
            resolved_conflicts.append(conflict)
        updated["conflicts"] = resolved_conflicts
    # Evidence insufficiency is recalculated from unresolved missing evidence,
    # never cleared merely because a reviewer clicked save.
    evidence_missing = [
        item for item in (updated.get("missing_items") or ())
        if "JSA" not in str(item)
    ]
    updated["evidence_insufficient"] = bool(evidence_missing or [c for c in updated.get("conflicts") or () if not c.get("resolved")])
    try:
        safety_review_service.save_pack_version(
            connection, str(draft["id"]), updated, user=user
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"保存审核版本失败：{exc}")
        return
    st.rerun()


def _render_review(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    draft: Mapping[str, Any],
) -> None:
    st.markdown("#### 4. 生成并审核Safety Review Pack")
    st.caption("后台引擎负责资料检索与结构化整理；用户无需理解Agent、Tool Calling等技术术语。")
    latest = safety_review_service.get_latest_pack(connection, str(draft["id"]))
    actions = st.columns([1.5, 1.5, 3])
    generate = actions[0].button(
        "重新生成审核包" if latest else "生成作业安全审核包",
        type="primary",
        width="stretch",
    )
    if actions[1].button("返回准备资料", width="stretch"):
        _set_step(3)
        st.rerun()
    if generate:
        try:
            stores, resources = _stores_for(str(draft["id"]))
            result = run_safety_review(
                draft,
                context=SafetyReviewContext(stores=stores, resources=resources),
                thread_id=f"phase1-{draft['id']}-{int((latest or {}).get('version', 0)) + 1}",
            )
            pack = result.get("pack")
            if not isinstance(pack, Mapping):
                raise ValueError("审核包生成引擎未返回结构化结果。")
            latest = safety_review_service.save_pack_version(
                connection, str(draft["id"]), pack, user=user
            )
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"生成失败：{exc}")
    if latest is None:
        st.info("准备好SDS与SOP/内部资料后，生成带Citation和阻断项的审核包。")
        return
    draft = safety_review_service.require_work_draft(connection, str(draft["id"]))
    _render_pack_sections(latest)
    blockers = safety_review.pack_blockers(draft, latest)
    if blockers:
        st.error("当前不能确认完成：")
        for blocker in blockers:
            st.markdown(f"- {blocker}")
    _review_jsa_and_save(connection, user, draft, latest)
    can_confirm = permissions.can(user, permissions.PERMIT_EHS_REVIEW) and not blockers
    if st.button(
        "确认Safety Review Pack（Phase 1终点）",
        type="primary",
        disabled=not can_confirm,
        width="stretch",
    ):
        try:
            safety_review_service.confirm_pack(
                connection, str(draft["id"]), user=user
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"确认失败：{exc}")
        else:
            common.set_flash("Safety Review Pack已由EHS确认；Phase 1不会创建正式Permit。")
            st.rerun()


def _render_confirmed_readonly(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    draft: Mapping[str, Any],
) -> None:
    """Confirmed drafts are frozen for audit; show a read-only view only."""
    st.info("审核包已确认，该版本已冻结并用于审计追溯。")
    st.markdown("#### 作业信息（只读）")
    planned = "—"
    start = str(draft.get("planned_start") or "").strip()
    end = str(draft.get("planned_end") or "").strip()
    if start or end:
        planned = f"{start or '—'} ~ {end or '—'}"
    rows = [
        ("作业名称", str(draft.get("title") or "")),
        ("主作业类型", str(draft.get("work_type") or "")),
        ("自由描述", str(draft.get("description") or "")),
        ("地点", str(draft.get("location") or "")),
        ("计划时间", planned),
        ("作业人数", str(draft.get("people_count") or "—")),
        ("负责人", str(draft.get("responsible_person") or "")),
        ("涉及承包商", "是" if draft.get("contractor_involved") else "否"),
        ("作业步骤", "；".join(str(item) for item in (draft.get("work_steps") or ()))),
        ("涉及化学品", "、".join(str(item) for item in (draft.get("chemicals") or ()))),
        (
            "确认风险标签",
            "、".join(
                str(item)
                for item in (draft.get("confirmed_risk_tags") or draft.get("user_risk_tags") or ())
            ),
        ),
    ]
    for label, value in rows:
        st.markdown(f"- **{label}**：{value if value.strip() else '—'}")
    latest = safety_review_service.get_latest_pack(connection, str(draft["id"]))
    if latest is not None:
        st.divider()
        _render_pack_sections(latest)
    st.divider()
    _render_permit_link_section(connection, user, draft)
    st.divider()
    if not permissions.can(user, permissions.PERMIT_CREATE):
        st.caption("切换为作业申请人身份后，可将该作业复制为新草稿。")
        return
    if st.button("复制为新作业草稿", key=f"copy_confirmed_{draft['id']}"):
        try:
            created = safety_review_service.create_work_draft(
                connection,
                user=user,
                title=str(draft.get("title") or ""),
                work_type=str(draft.get("work_type") or ""),
                description=str(draft.get("description") or ""),
                location=str(draft.get("location") or ""),
                planned_start=str(draft.get("planned_start") or ""),
                planned_end=str(draft.get("planned_end") or ""),
                people_count=int(draft.get("people_count") or 1),
                responsible_person=str(draft.get("responsible_person") or ""),
                contractor_involved=bool(draft.get("contractor_involved")),
                work_steps=[str(item) for item in (draft.get("work_steps") or ())],
                chemicals=[str(item) for item in (draft.get("chemicals") or ())],
                user_risk_tags=[str(item) for item in (draft.get("user_risk_tags") or ())],
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"复制失败：{exc}")
            return
        open_draft(str(created["id"]))
        common.set_flash(
            f"已创建新作业草稿 {created['id']}；原记录 {draft['id']} 保持不变。"
        )
        st.rerun()
    st.caption(
        "新草稿不继承已确认状态与人工L/S确认结果，需重新完成资料准备、审核包生成与EHS确认。"
    )


PERMIT_ERROR_KEY = "_safety_review_permit_error"


def _open_pack_permit(permit_id: str, created: bool) -> None:
    """Callback: leave the workspace and open the permit detail.

    Widget-bound session keys (the nav radio) may only be written inside a
    callback, which runs before the widgets are instantiated on the rerun.
    """
    close_workspace()
    if created:
        common.set_flash(
            f"已创建正式作业许可 {permit_id}（草稿）。Pack确认不等于Permit批准，"
            "请继续完成EHS审核与审批流程。"
        )
    common.open_detail(common.PAGE_PERMITS, "permit", permit_id)


def _create_permit_from_pack_callback(draft_id: str) -> None:
    """Callback: create the formal Permit from the confirmed pack.

    Callbacks run before the page's own connection is opened, so this opens
    its own short-lived store connection.
    """
    with common.database() as connection:
        user = common.current_user(connection)
        try:
            outcome = safety_review_service.create_permit_from_pack(
                connection, draft_id, user=user, now=common.now()
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            st.session_state[PERMIT_ERROR_KEY] = f"创建失败：{exc}"
            return
        permit_id = str(outcome["permit"]["id"])
        created = bool(outcome["created"])
    st.session_state.pop(PERMIT_ERROR_KEY, None)
    _open_pack_permit(permit_id, created=created)


def _render_permit_link_section(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    draft: Mapping[str, Any],
) -> None:
    """Confirmed pack → formal Permit: the Phase 2 hand-over point."""
    st.markdown("#### 转入正式作业许可")
    st.caption("审核包已确认 → 创建正式作业许可 → 进入现有审核/审批/开工/执行流程。")
    error_message = st.session_state.pop(PERMIT_ERROR_KEY, None)
    if error_message:
        st.error(error_message)
    link = safety_review_service.get_pack_permit_link(connection, str(draft["id"]))
    if link is not None:
        permit = permit_service.get_permit(connection, str(link["permit_id"]))
        status_label = (
            common.permit_status_label(permit.get("status"))
            if permit is not None
            else "未知"
        )
        st.success(
            f"该审核包已创建正式作业许可 **{link['permit_id']}**（当前状态：{status_label}）。"
            "一个审核包只对应一个正式许可，不会重复创建。"
        )
        st.button(
            f"打开正式作业许可 {link['permit_id']}",
            key=f"open_permit_{draft['id']}",
            on_click=_open_pack_permit,
            args=(str(link["permit_id"]), False),
        )
        return
    if not permissions.can(user, permissions.PERMIT_CREATE):
        st.caption("切换为作业申请人身份后，可将该确认结果转入正式作业许可。")
        return
    st.button(
        "创建正式作业许可",
        type="primary",
        key=f"create_permit_{draft['id']}",
        on_click=_create_permit_from_pack_callback,
        args=(str(draft["id"]),),
    )


def render_workspace(connection: sqlite3.Connection, user: Mapping[str, Any]) -> None:
    """Render the active four-step workflow or the new-draft first step."""
    draft = _active_draft(connection)
    if draft is not None and str(draft.get("status")) == safety_review.STATUS_CONFIRMED:
        header = st.columns([4, 1])
        header[0].markdown(f"### {draft['id']} · 已确认（只读）")
        if header[1].button("关闭", key="close_safety_review"):
            close_workspace()
            st.rerun()
        _render_confirmed_readonly(connection, user, draft)
        return
    step = int(st.session_state.get(STEP_KEY, 1))
    header = st.columns([4, 1])
    header[0].markdown("### 新建高风险作业 · 安全准备")
    if header[1].button("关闭", key="close_safety_review"):
        close_workspace()
        st.rerun()
    _render_step_header(step)
    if step == 1:
        _render_intake(connection, user, draft)
    elif draft is None:
        st.warning("未找到作业草稿，请返回第一步重新创建。")
        _set_step(1)
    elif step == 2:
        _render_precheck(connection, user, draft)
    elif step == 3:
        _render_documents(connection, user, draft)
    else:
        _render_review(connection, user, draft)


def _start_new_blank_draft() -> None:
    """Primary entry: a brand-new blank WorkDraft straight into Step 1."""
    st.session_state.pop(ACTIVE_DRAFT_KEY, None)
    st.session_state[STEP_KEY] = 1
    st.session_state[DEMO_PREFILL_KEY] = False
    # Same flag as permits.CREATE_KEY (kept as literal to avoid a circular import):
    # it opens the four-step workspace instead of the draft list.
    st.session_state["_permit_create_open"] = True


def render_draft_list(connection: sqlite3.Connection, user: Mapping[str, Any]) -> None:
    """Show preparation drafts above the unchanged formal Permit list."""
    drafts = safety_review_service.list_work_drafts(connection)
    st.markdown("#### 高风险作业安全准备")
    if permissions.can(user, permissions.PERMIT_CREATE) and st.button(
        "+ 新建高风险作业", type="primary", key="start_new_work_draft_top"
    ):
        _start_new_blank_draft()
        st.rerun()
    if not drafts:
        st.caption("暂无Safety Review Pack准备记录。")
        return
    for draft in drafts[:10]:
        columns = st.columns([1.2, 2.5, 1.3, 1.2])
        columns[0].markdown(f"**{draft['id']}**")
        columns[1].write(draft["title"])
        columns[2].write(safety_review.STATUS_LABELS.get(draft["status"], draft["status"]))
        if columns[3].button("打开", key=f"open_work_draft_{draft['id']}"):
            open_draft(str(draft["id"]))
            st.rerun()


__all__ = [
    "ACTIVE_DRAFT_KEY",
    "STEP_KEY",
    "close_workspace",
    "open_draft",
    "render_draft_list",
    "render_workspace",
]

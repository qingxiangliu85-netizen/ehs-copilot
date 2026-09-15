"""作业许可 — permit list and permit detail.

The detail page is the heart of P0C: one header, one 「当前需要你做什么」 block
driven by status + persona + permission, then the full record split into
collapsed sections.  JSA and SDS are no longer standalone tools; they live here.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

import streamlit as st

from services import permit_service, persona_service, safety_review_service
from workflow import audit, permit_state, permissions, roles, sla
from workflow import actions as workflow_actions

from . import commands, common, product, safety_review as safety_review_ui, widgets, worklist

PENDING_KEY = "_permit_pending"
CREATE_KEY = "_permit_create_open"

NEXT_ACTION_LABELS: dict[str, str] = {
    permit_state.PERMIT_DRAFT: "待申请人提交EHS审核",
    permit_state.PERMIT_RETURNED: "待申请人修改后重新提交",
    permit_state.PERMIT_EHS_REVIEW: "待EHS审核",
    permit_state.PERMIT_APPROVAL_PENDING: "待审批",
    permit_state.PERMIT_APPROVED: "待开工确认",
    permit_state.PERMIT_ACTIVE: "作业执行中，待交还现场",
    permit_state.PERMIT_SUSPENDED: "作业已暂停，待恢复",
    permit_state.PERMIT_CLOSEOUT_REVIEW: "待关闭作业许可",
    permit_state.PERMIT_CLOSED: "已闭环",
    permit_state.PERMIT_CANCELLED: "已取消",
    permit_state.PERMIT_EXPIRED: "已过期",
}

TASK_TEXT: dict[str, str] = {
    "submit": "补全并提交EHS审核（需化学品、SDS证据与JSA）",
    "resubmit": "修改后重新提交EHS审核",
    "return_draft": "把作业许可退回申请人补充材料",
    "confirm": "审核 SDS 证据与 JSA，并给出 EHS 结论",
    "approve": "审批该作业许可（批准后方可开工）",
    "reject": "驳回该作业许可并说明理由",
    "prestart_confirm": "完成开工前检查并开始作业",
    "suspend": "暂停该作业并说明原因",
    "resume": "恢复该作业",
    "complete_work": "作业完成并交还现场",
    "return_to_active": "退回继续执行",
    "close": "确认关联隐患全部关闭后闭合许可",
    "cancel": "取消该作业许可",
}

# Section order used to decide what is current, done or still in the future.
_SECTION_STAGE = {
    "chemicals": 0,
    "jsa": 1,
    "approval": 2,
    "prestart": 3,
    "execution": 4,
}

_APPROVAL_ACTIONS = (
    "permit.submit",
    "permit.resubmit",
    "permit.return_draft",
    "permit.confirm",
    "permit.approve",
    "permit.reject",
)
_EXECUTION_ACTIONS = (
    "permit.prestart_confirm",
    "permit.suspend",
    "permit.resume",
    "permit.complete_work",
    "permit.return_to_active",
    "permit.close",
)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _events(connection: sqlite3.Connection, permit_id: str) -> list[dict[str, Any]]:
    return audit.list_events(
        connection, entity_type=audit.ENTITY_PERMIT, entity_id=permit_id, limit=500
    )


def _filter_events(
    events: Sequence[Mapping[str, Any]], actions: Sequence[str]
) -> list[dict[str, Any]]:
    wanted = set(actions)
    return [dict(event) for event in events if str(event.get("action", "")) in wanted]


def permit_next_action(permit: Mapping[str, Any]) -> str:
    """Return the business description of a permit's next step."""
    return NEXT_ACTION_LABELS.get(str(permit.get("status", "")), "—")


def permit_is_overdue(
    permit: Mapping[str, Any], moment: datetime | None = None
) -> bool:
    """Return whether the permit has blown its own Demo clock."""
    moment = moment or common.now()
    status = str(permit.get("status", ""))
    if status in (permit_state.PERMIT_EHS_REVIEW, permit_state.PERMIT_APPROVAL_PENDING):
        return sla.is_overdue(permit.get("approval_due_at"), moment)
    if status in (
        permit_state.PERMIT_APPROVED,
        permit_state.PERMIT_ACTIVE,
        permit_state.PERMIT_SUSPENDED,
    ):
        return sla.is_overdue(permit.get("valid_to"), moment)
    return False


def _risk_of(permit: Mapping[str, Any]) -> str:
    return str(
        permit.get("residual_risk_level", "") or permit.get("risk_level", "") or ""
    )


def _row(connection: sqlite3.Connection, permit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "编号": str(permit.get("id", "")),
        "名称": str(permit.get("title", "")),
        "作业类型": product.work_type_label(permit),
        "区域": str(permit.get("area", "") or "—"),
        "风险": common.risk_label(_risk_of(permit)),
        "状态": common.permit_status_label(permit.get("status")),
        "负责人": common.user_name(connection, str(permit.get("owner_id", ""))),
        "有效期": (
            f"{common.short_date(permit.get('valid_from'))}"
            f" ~ {common.short_date(permit.get('valid_to'))}"
        ),
        "有效期状态": common.permit_validity(permit)["label"],
        "当前下一动作": permit_next_action(permit),
        "是否逾期": "是" if permit_is_overdue(permit) else "否",
    }


# --------------------------------------------------------------------------- #
# List page
# --------------------------------------------------------------------------- #


def _owner_options(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        user
        for user in persona_service.list_users(connection)
        if str(user.get("role", ""))
        in (roles.ROLE_ACTION_OWNER, roles.ROLE_APPLICANT, roles.ROLE_DEMO_ADMIN)
    ]


def _render_create_form(connection: sqlite3.Connection, user: Mapping[str, Any]) -> None:
    st.markdown("#### 新建作业许可")
    st.caption(
        "新建的作业许可从「草稿」开始；提交前必须至少包含 1 个化学品、"
        "1 条 SDS 证据与 1 条 JSA 记录。"
    )
    with st.form("permit_create_form"):
        st.markdown("**基本信息**")
        first = st.columns(4)
        work_type = first[0].selectbox("作业类型", list(product.WORK_TYPES))
        title = first[1].text_input("作业名称", value="换热器管线焊接维修（模拟）")
        area = first[2].text_input("作业区域", value="一号线检修区（模拟区域）")
        valid_days = first[3].number_input("有效期（天）", 1, 90, 7)

        st.markdown("**化学品与 SDS 证据**")
        second = st.columns(3)
        chemical = second[0].text_input("化学品名称", value="乙炔")
        sds_file = second[1].text_input(
            "SDS 文件", value="EHS_Copilot_Demo_Synthetic_SDS.pdf"
        )
        sds_page = second[2].number_input("SDS 页码", 1, 500, 3)
        snippet = st.text_area(
            "SDS 原文片段",
            value="危险性概述：模拟数据，易燃气体，与空气混合可形成爆炸性混合物。",
            height=70,
        )

        st.markdown("**JSA 风险评估**")
        third = st.columns(2)
        work_step = third[0].text_input("作业步骤", value="管线切割与焊接")
        hazard = third[1].text_input("危害因素", value="焊接火花引燃可燃物（模拟）")
        consequence = st.text_area("可能后果", value="火灾与爆炸（模拟）", height=70)
        initial = st.columns(3)
        likelihood = initial[0].slider("可能性 L", 1, 5, 4)
        severity = initial[1].slider("严重度 S", 1, 5, 4)
        residual_l = initial[2].slider("控制后 L", 1, 5, 2)
        controls = st.text_area(
            "控制措施",
            value="动火许可、监火人、灭火器材与防火毯、可燃气体检测（模拟）",
            height=70,
        )

        st.markdown("**指派**")
        fourth = st.columns(3)
        owners = _owner_options(connection)
        reviewer_options = [
            item
            for item in persona_service.list_users(connection)
            if str(item.get("role", "")) in (roles.ROLE_EHS_REVIEWER,)
        ]
        approver_options = [
            item
            for item in persona_service.list_users(connection)
            if str(item.get("role", "")) == roles.ROLE_APPROVER
        ]
        owner_id = fourth[0].selectbox(
            "作业负责人",
            [str(item["id"]) for item in owners],
            format_func=lambda value: common.user_name(connection, value),
        )
        reviewer_id = fourth[1].selectbox(
            "EHS审核人",
            [str(item["id"]) for item in reviewer_options],
            format_func=lambda value: common.user_name(connection, value),
        )
        approver_id = fourth[2].selectbox(
            "批准人",
            [str(item["id"]) for item in approver_options],
            format_func=lambda value: common.user_name(connection, value),
        )
        submitted = st.form_submit_button(
            "创建作业许可（草稿）", type="primary", width="stretch"
        )

    if not submitted:
        return
    try:
        created = permit_service.create_permit(
            connection,
            title=title,
            permit_type=work_type,
            applicant_id=str(user.get("id", "")),
            owner_id=owner_id,
            area=area,
            ehs_reviewer_id=reviewer_id,
            designated_approver_id=approver_id,
            valid_to=(common.now() + timedelta(days=int(valid_days))).isoformat(
                timespec="seconds"
            ),
            data_label=common.DEMO_NOTICE,
            is_demo=True,
            chemicals=[{"chemical_name": chemical, "sds_status": "demo_synthetic"}],
            steps=[{"step_no": 1, "name": work_step}],
            evidence=[
                {
                    "track": "sds",
                    "source": sds_file,
                    "page": int(sds_page),
                    "snippet": snippet,
                    "data_label": "Demo / Synthetic SDS",
                    "is_demo": True,
                }
            ],
            jsa_items=[
                {
                    "work_step": work_step,
                    "hazard": hazard,
                    "consequence": consequence,
                    "likelihood": int(likelihood),
                    "severity": int(severity),
                    "existing_controls": "局部排风、耐酸碱手套、护目镜（模拟）",
                    "proposed_controls": controls,
                    "residual_likelihood": int(residual_l),
                    "residual_severity": int(severity),
                }
            ],
            actor=str(user.get("id", "")),
            now=common.now(),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        st.error(f"创建失败：{exc}")
        return
    st.session_state[CREATE_KEY] = False
    common.set_flash(f"已创建作业许可 {created.get('id', '')}（草稿）。")
    st.rerun()


def render_list(connection: sqlite3.Connection, user: Mapping[str, Any]) -> None:
    """Render the 作业许可 list page."""
    st.title("作业许可")
    st.caption("高风险作业安全准备与正式作业许可")

    can_create = permissions.can(user, permissions.PERMIT_CREATE)
    toolbar = st.columns([1.2, 3.8])
    if can_create and toolbar[0].button(
        "新建高风险作业", type="primary", width="stretch"
    ):
        st.session_state[CREATE_KEY] = not bool(st.session_state.get(CREATE_KEY))
        if st.session_state[CREATE_KEY]:
            safety_review_ui.close_workspace()
    if can_create:
        toolbar[1].caption(
            f"当前身份 {common.persona_label(user)}：完成Safety Review Pack确认后，"
            "可将其转入正式作业许可（初始为草稿，仍须走完审核与审批）。"
        )

    active_draft = bool(st.session_state.get(safety_review_ui.ACTIVE_DRAFT_KEY))
    if (can_create and st.session_state.get(CREATE_KEY)) or active_draft:
        safety_review_ui.render_workspace(connection, user)
        return

    safety_review_ui.render_draft_list(connection, user)
    st.divider()

    permits = [
        permit_service.get_permit(connection, str(row["id"]))
        for row in permit_service.list_permits(connection, limit=500)
    ]
    permits = [permit for permit in permits if permit is not None]
    st.markdown("#### 正式作业许可")
    st.caption("已确认审核包转入的许可从这里进入 EHS审核 → 审批 → 开工 → 执行 → 关闭 流程。")

    if not permits:
        widgets.empty_state("暂无作业许可记录。")
        return

    status_options = ["全部"] + [
        common.permit_status_label(status) for status in permit_state.PERMIT_STATUSES
    ]
    risk_options = ["全部"] + list(common.RISK_LEVELS)
    type_options = ["全部"] + sorted(
        {product.work_type_label(permit) for permit in permits}
    )
    owner_ids = sorted(
        {str(permit.get("owner_id", "")) for permit in permits if permit.get("owner_id")}
    )
    owner_labels = {value: common.user_name(connection, value) for value in owner_ids}

    filters = st.columns([2.0, 1.3, 1.1, 1.5, 1.3])
    keyword = filters[0].text_input("搜索", placeholder="搜索编号、名称或区域")
    status_choice = filters[1].selectbox("状态筛选", status_options)
    risk_choice = filters[2].selectbox("风险筛选", risk_options)
    type_choice = filters[3].selectbox("作业类型筛选", type_options)
    owner_choice = filters[4].selectbox(
        "负责人筛选",
        ["全部"] + owner_ids,
        format_func=lambda value: "全部" if value == "全部" else owner_labels.get(value, value),
    )

    text = str(keyword or "").strip().lower()
    visible = []
    for permit in permits:
        if status_choice != "全部" and common.permit_status_label(
            permit.get("status")
        ) != status_choice:
            continue
        if risk_choice != "全部" and _risk_of(permit) != risk_choice:
            continue
        if type_choice != "全部" and product.work_type_label(permit) != type_choice:
            continue
        if owner_choice != "全部" and str(permit.get("owner_id", "")) != owner_choice:
            continue
        if text:
            blob = " ".join(
                [
                    str(permit.get("id", "")),
                    str(permit.get("title", "")),
                    str(permit.get("area", "")),
                ]
            ).lower()
            if text not in blob:
                continue
        visible.append(permit)

    st.caption(f"共 {len(visible)} 条作业许可（全部 {len(permits)} 条）")
    if not visible:
        widgets.empty_state("没有符合条件的作业许可，请调整筛选条件。")
        return

    rows = [_row(connection, permit) for permit in visible]
    event = st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="permit_table",
    )
    selected = list(getattr(getattr(event, "selection", None), "rows", []) or [])
    if selected:
        index = int(selected[0])
        if 0 <= index < len(visible):
            permit_id = str(visible[index].get("id", ""))
            st.button(
                f"打开 {permit_id} 详情",
                type="primary",
                key=f"open_permit_{permit_id}",
                on_click=common.open_detail,
                args=(common.PAGE_PERMITS, "permit", permit_id),
            )
    else:
        st.caption("在上表中选中一行，即可打开该作业许可详情。")


# --------------------------------------------------------------------------- #
# Detail page
# --------------------------------------------------------------------------- #


def _render_role_row(
    connection: sqlite3.Connection, permit: Mapping[str, Any]
) -> None:
    """Show the four responsible roles of one permit on the first screen."""
    st.markdown("**责任角色**")
    roles = (
        ("作业申请人", permit.get("applicant_id", "")),
        ("作业负责人", permit.get("owner_id", "")),
        ("EHS审核人", permit.get("ehs_reviewer_id", "")),
        ("作业审批人", permit.get("designated_approver_id", "")),
    )
    columns = st.columns(len(roles))
    for index, (label, value) in enumerate(roles):
        with columns[index]:
            st.markdown(label)
            st.caption(common.user_name(connection, str(value or "")))


def _render_validity(permit: Mapping[str, Any]) -> None:
    """Show the permit validity window.

    This is a *time* fact and is deliberately rendered apart from the risk chip:
    a permit can be low-risk and already expired, or high-risk and still valid.
    """
    validity = common.permit_validity(permit)
    st.markdown("**许可有效期**")
    widgets.render_chips(
        [
            widgets.validity_chip(validity),
            (validity["range_text"], common.NEUTRAL_CHIP),
        ]
    )
    if validity["is_expired"]:
        st.error(
            "该作业许可已超过有效期：不能开工，也不能恢复已暂停的作业。"
            "如需继续，请重新提交审批或办理延期。"
        )
    elif validity["is_expiring"]:
        days = max(int(validity.get("days_left") or 0), 0)
        st.warning(f"许可即将到期（剩余 {days} 天），请在有效期内完成作业并交还现场。")
    else:
        st.caption("许可在有效期内。有效期是时间维度，与作业风险等级相互独立。")


def _render_header(connection: sqlite3.Connection, permit: Mapping[str, Any]) -> None:
    st.title(f"{permit.get('id', '')} · {permit.get('title', '')}")
    chips: list[tuple[str, tuple[str, str] | None]] = [
        widgets.risk_chip(_risk_of(permit)),
        widgets.permit_status_chip(permit.get("status")),
        widgets.neutral_chip(common.permit_stage_label(permit.get("status"))),
    ]
    overdue = widgets.overdue_chip(permit_is_overdue(permit))
    if overdue is not None:
        chips.append(overdue)
    widgets.render_chips(chips)

    _render_role_row(connection, permit)
    st.write("")
    _render_validity(permit)
    st.write("")

    widgets.key_values(
        [
            ("编号", permit.get("id", "")),
            ("作业名称", permit.get("title", "")),
            ("作业类型", product.work_type_label(permit)),
            ("状态", common.permit_status_label(permit.get("status"))),
            ("风险等级", common.risk_label(_risk_of(permit))),
            ("区域", permit.get("area", "") or "—"),
            ("审批截止时间", common.short_datetime(permit.get("approval_due_at"))),
            ("数据性质", str(permit.get("data_label", "") or "—")),
        ]
    )


def _prestart_checklist(permit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the Demo pre-start checklist for one permit."""
    return [
        {
            "item_code": "PPE",
            "item_text": "作业人员资质与防护装备就位",
            "result": "pass",
        },
        {"item_code": "VENT", "item_text": "通风与气体监测设备运行正常", "result": "pass"},
        {"item_code": "SPILL", "item_text": "应急冲淋、中和与吸附物资就位", "result": "pass"},
    ]


def _render_pending_form(
    connection: sqlite3.Connection,
    permit: Mapping[str, Any],
    user: Mapping[str, Any],
    code: str,
) -> None:
    permit_id = str(permit.get("id", ""))
    st.markdown("---")
    if code == "prestart_confirm":
        st.markdown("**开工前检查**")
        st.caption("所有必检项通过后，作业许可才会进入「执行中」。")
        results: list[dict[str, Any]] = []
        for index, item in enumerate(_prestart_checklist(permit), start=1):
            ok = st.checkbox(
                item["item_text"],
                value=True,
                key=f"prestart_{permit_id}_{item['item_code']}",
            )
            results.append(
                {
                    "item_code": item["item_code"],
                    "item_text": item["item_text"],
                    "required": True,
                    "result": "pass" if ok else "fail",
                }
            )
        note = st.text_input("检查备注（可选）", key=f"prestart_note_{permit_id}")
        actions = st.columns([1, 1])
        if actions[0].button("提交开工检查", type="primary", key=f"prestart_ok_{permit_id}"):
            ok, message = commands.execute(
                connection,
                "permit",
                permit_id,
                code,
                user=user,
                reason=note,
                checks=results,
            )
            st.session_state.pop(PENDING_KEY, None)
            common.set_flash(message if ok else f"操作未完成：{message}",
                             "success" if ok else "error")
            st.rerun()
        if actions[1].button("取消", key=f"prestart_cancel_{permit_id}"):
            st.session_state.pop(PENDING_KEY, None)
            st.rerun()
        return

    label = TASK_TEXT.get(code, "请说明本次操作的原因")
    spec = workflow_actions.PERMIT_ACTION_SPECS.get(code)
    heading = spec.label if spec is not None else code
    with st.form(f"pending_{permit_id}_{code}"):
        st.markdown(f"**{heading}**")
        reason = widgets.reason_input(label=label, key=f"reason_{permit_id}_{code}")
        confirm = st.form_submit_button("确认执行", type="primary")
        cancel = st.form_submit_button("取消")
    if confirm:
        if not str(reason or "").strip():
            st.error("该操作必须填写原因。")
            return
        ok, message = commands.execute(
            connection, "permit", permit_id, code, user=user, reason=reason
        )
        st.session_state.pop(PENDING_KEY, None)
        common.set_flash(
            message if ok else f"操作未完成：{message}", "success" if ok else "error"
        )
        st.rerun()
    if cancel:
        st.session_state.pop(PENDING_KEY, None)
        st.rerun()


def _render_action_area(
    connection: sqlite3.Connection,
    permit: Mapping[str, Any],
    user: Mapping[str, Any],
) -> None:
    permit_id = str(permit.get("id", ""))
    actions = worklist.actions_for("permit", permit, user)
    primary = workflow_actions.primary_action(actions)

    with st.container(border=True):
        st.markdown("#### 当前需要你做什么")
        st.caption(
            f"当前身份：{common.persona_label(user)}　｜　"
            f"当前状态：{common.permit_status_label(permit.get('status'))}"
        )
        if primary is None:
            role, todo = product.next_role_hint(permit)
            if role:
                st.info(
                    "当前身份在本阶段没有可执行的动作。"
                    f"下一步请按上方提示切换为 **{role}**（Demo）：{todo}"
                )
            else:
                st.info("当前身份在这一状态下没有可执行的动作。")
            return
        st.markdown(
            f"**当前任务：**　{TASK_TEXT.get(primary.code, primary.label)}"
        )

        # A lapsed window must block any *new* start of work.  The state machine
        # is still the real gate; disabling the button only avoids a dead click.
        disabled: dict[str, bool] = {}
        if common.permit_validity(permit)["is_expired"]:
            for code in ("prestart_confirm", "resume"):
                if any(action.code == code for action in actions):
                    disabled[code] = True
            if disabled:
                st.error(
                    "该作业许可已超过有效期，不能开工或恢复作业；"
                    "请重新提交审批或办理延期。"
                )

        pending = st.session_state.get(PENDING_KEY) or {}
        if pending.get("id") == permit_id:
            _render_pending_form(connection, permit, user, str(pending.get("code", "")))
            return
        clicked = widgets.render_actions(
            actions, key_prefix=f"permit_{permit_id}", disabled=disabled
        )
        if clicked is None:
            return
        action = next((item for item in actions if item.code == clicked), None)
        if action is None:
            return
        if action.requires_reason or action.code == "prestart_confirm":
            st.session_state[PENDING_KEY] = {"id": permit_id, "code": action.code}
            st.rerun()
        ok, message = commands.execute(
            connection, "permit", permit_id, action.code, user=user
        )
        common.set_flash(
            message if ok else f"操作未完成：{message}", "success" if ok else "error"
        )
        st.rerun()


# Sections whose content can already exist before the stage officially starts
# (inherited from a confirmed Safety Review Pack); existing data is shown
# instead of the "not started yet" placeholder.
_SECTION_DATA_KEYS = {
    "chemicals": "chemicals",
    "jsa": "jsa_items",
}


def _section_is_future(permit: Mapping[str, Any], key: str) -> bool:
    stage = _SECTION_STAGE.get(key, 99)
    if stage <= common.permit_stage_index(permit.get("status")):
        return False
    data_key = _SECTION_DATA_KEYS.get(key)
    if data_key and list(permit.get(data_key) or ()):
        return False
    return True


def _render_chemicals(connection: sqlite3.Connection, permit: Mapping[str, Any]) -> None:
    st.markdown("**化学品**")
    chemicals = list(permit.get("chemicals") or ())
    if chemicals:
        st.dataframe(
            [
                {
                    "化学品": item.get("chemical_name", ""),
                    "别名": "、".join(item.get("aliases") or ()),
                    "SDS 文件": item.get("sds_file", "") or "—",
                    "SDS 状态": item.get("sds_status", "") or "—",
                }
                for item in chemicals
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("尚未登记化学品。")

    evidence = permit.get("evidence") or {}
    sds_rows = list(evidence.get("sds") or ())
    public_rows = list(evidence.get("public_sources") or ())

    st.markdown("**【SDS 证据】**")
    if sds_rows:
        if any(bool(item.get("is_demo")) for item in sds_rows):
            widgets.render_chips([("Demo / Synthetic", common.RISK_COLORS["中"])])
        st.dataframe(
            [
                {
                    "来源文件": item.get("source", ""),
                    "页码": item.get("page", 0),
                    "章节": item.get("sections", "") or "—",
                    "原文片段": item.get("snippet", ""),
                    "数据性质": item.get("data_label", "") or "—",
                }
                for item in sds_rows
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.warning("尚无 SDS 证据。缺少 SDS 证据时不能提交 EHS 审核。")
        st.caption("公开来源安全证据不能替代 SDS。")

    st.markdown("**【公开安全证据（非 SDS）】**")
    if public_rows:
        st.dataframe(
            [
                {
                    "来源": item.get("source_title", "") or item.get("organization", ""),
                    "机构": item.get("organization", "") or "—",
                    "主题": item.get("topic", "") or "—",
                    "原文片段": item.get("snippet", ""),
                    "链接": item.get("source_url", ""),
                }
                for item in public_rows
            ],
            hide_index=True,
            width="stretch",
        )
        st.caption("公开来源证据不是 SDS，不能用于替代 SDS 结论。")
    else:
        st.caption("无公开来源证据。公开来源证据不是 SDS，不能用于替代 SDS 结论。")

    st.button(
        "在 SDS 知识库中检索该化学品",
        key=f"sds_link_{permit.get('id', '')}",
        on_click=common.go_to,
        args=(common.PAGE_RECORDS,),
    )


def _render_jsa(permit: Mapping[str, Any]) -> None:
    items = list(permit.get("jsa_items") or ())
    drafts = [item for item in items if not str(item.get("confirmed_by", "")).strip()]
    confirmed = [item for item in items if str(item.get("confirmed_by", "")).strip()]

    st.markdown("**AI 草稿（未经 EHS 确认）**")
    if drafts:
        st.dataframe(
            [
                {
                    "作业步骤": item.get("work_step", ""),
                    "危害": item.get("hazard", ""),
                    "可能后果": item.get("consequence", ""),
                    "L": item.get("likelihood"),
                    "S": item.get("severity"),
                    "风险": f"{item.get('risk_score', '')} · {item.get('risk_level', '')}",
                    "建议控制措施": item.get("proposed_controls", ""),
                }
                for item in drafts
            ],
            hide_index=True,
            width="stretch",
        )
        st.caption("草稿由 AI 生成，未经 EHS 确认不得作为正式 JSA。")
    else:
        st.caption("无待确认的 AI 草稿。")

    st.markdown("**EHS 人工确认版（最终）**")
    if confirmed:
        st.dataframe(
            [
                {
                    "作业步骤": item.get("work_step", ""),
                    "危害": item.get("hazard", ""),
                    "残余 L": item.get("residual_likelihood"),
                    "残余 S": item.get("residual_severity"),
                    "残余风险": (
                        f"{item.get('residual_risk_score', '')} · "
                        f"{item.get('residual_risk_level', '')}"
                    ),
                    "控制措施": item.get("proposed_controls", ""),
                    "确认人": item.get("confirmed_by", ""),
                    "确认时间": common.short_datetime(item.get("confirmed_at")),
                }
                for item in confirmed
            ],
            hide_index=True,
            width="stretch",
        )
        st.caption(
            "人工确认版是最终 L/S 结论，AI 不能修改已确认的人工结论。"
        )
    else:
        st.caption("尚未形成 EHS 人工确认版。")


def _render_approval(events: Sequence[Mapping[str, Any]]) -> None:
    rows = _filter_events(events, _APPROVAL_ACTIONS)
    if not rows:
        st.caption("尚无审核与审批记录。")
        return
    st.dataframe(
        [
            {
                "时间": common.short_datetime(event.get("created_at")),
                "操作者": event.get("actor", ""),
                "动作": widgets.AUDIT_ACTION_LABELS.get(
                    str(event.get("action", "")), event.get("action", "")
                ),
                "状态变化": (
                    f"{common.business_state_label('permit', event.get('from_state'))} → "
                    f"{common.business_state_label('permit', event.get('to_state'))}"
                ),
                "原因": event.get("reason", "") or "—",
            }
            for event in rows
        ],
        hide_index=True,
        width="stretch",
    )


def _render_prestart(connection: sqlite3.Connection, permit: Mapping[str, Any]) -> None:
    checks = list(permit.get("prestart_checks") or ())
    if not checks:
        st.caption("尚未完成开工前检查。")
        return
    st.dataframe(
        [
            {
                "检查项": item.get("item_text", "") or item.get("item_code", ""),
                "必检": "是" if int(item.get("required", 1) or 0) else "否",
                "结果": {"pass": "通过", "fail": "未通过", "pending": "待检查"}.get(
                    str(item.get("result", "")), str(item.get("result", ""))
                ),
                "检查人": item.get("checked_by", "") or "—",
                "检查时间": common.short_datetime(item.get("checked_at")),
                "备注": item.get("evidence_note", "") or "—",
            }
            for item in checks
        ],
        hide_index=True,
        width="stretch",
    )


def _render_execution(permit: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> None:
    rows = _filter_events(events, _EXECUTION_ACTIONS)
    if rows:
        st.dataframe(
            [
                {
                    "时间": common.short_datetime(event.get("created_at")),
                    "操作者": event.get("actor", ""),
                    "动作": widgets.AUDIT_ACTION_LABELS.get(
                        str(event.get("action", "")), event.get("action", "")
                    ),
                    "状态变化": (
                        f"{common.business_state_label('permit', event.get('from_state'))} → "
                        f"{common.business_state_label('permit', event.get('to_state'))}"
                    ),
                    "说明": event.get("reason", "") or "—",
                }
                for event in rows
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("尚无执行记录。")
    if permit.get("handback_note"):
        st.markdown(f"**现场交还说明：**　{permit.get('handback_note')}")
    if permit.get("closure_note"):
        st.markdown(f"**关闭说明：**　{permit.get('closure_note')}")


def _latest_event(
    events: Sequence[Mapping[str, Any]], action: str
) -> dict[str, Any] | None:
    """Return the most recent audit event of one action, or ``None``."""
    for event in reversed(list(events)):
        if str(event.get("action", "")) == action:
            return dict(event)
    return None


def _render_suspension(
    connection: sqlite3.Connection,
    permit: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> None:
    """Make a paused permit unmistakable — and clearly *not* a closed one."""
    st.markdown("#### 作业已暂停")
    st.error(
        "该作业当前为「已暂停」，并没有关闭：现场作业应停止，"
        "待条件恢复后再继续。"
    )
    paused = _latest_event(events, "permit.suspend")
    if paused is None:
        st.caption("未找到暂停记录。")
    else:
        widgets.key_values(
            [
                ("暂停人", common.user_name(connection, str(paused.get("actor", "")))),
                ("暂停时间", common.short_datetime(paused.get("created_at"))),
                ("暂停原因", paused.get("reason", "") or "—"),
            ]
        )
    st.caption("恢复作业前，系统会校验该许可仍在有效期内；恢复人、恢复时间都会写入审计记录。")


def _closeout_gate(
    connection: sqlite3.Connection,
    permit: Mapping[str, Any],
    user: Mapping[str, Any],
) -> list[tuple[str, bool, str]]:
    """Return the four checks that must hold before a permit may be closed."""
    status = str(permit.get("status", ""))
    handback = str(permit.get("handback_note", "") or "").strip()
    open_count = int(permit.get("open_hazard_count", 0) or 0)
    return [
        (
            "作业已结束（已提交作业完成）",
            status == permit_state.PERMIT_CLOSEOUT_REVIEW,
            "尚未提交作业完成",
        ),
        (
            "现场交还信息已填写",
            bool(handback),
            "缺少现场交还说明",
        ),
        (
            "关联未关闭隐患 = 0",
            open_count == 0,
            f"当前仍有 {open_count} 条未关闭隐患",
        ),
        (
            "当前身份具备关闭权限",
            permissions.can(user, permissions.PERMIT_CLOSE, permit),
            "当前身份无关闭权限",
        ),
    ]


def _render_closeout(
    connection: sqlite3.Connection,
    permit: Mapping[str, Any],
    user: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> None:
    """Make 「作业完成 ≠ 许可关闭」 explicit, with the pre-close checks."""
    st.markdown("#### 现场交还与确认关闭")
    st.caption(
        "作业完成不等于许可关闭：作业负责人交还现场后，"
        "必须由 EHS 确认现场与设备可恢复正常状态，作业许可才能关闭。"
    )
    handback = _latest_event(events, "permit.complete_work")
    if handback is None:
        st.caption("尚未提交作业完成。")
    else:
        widgets.key_values(
            [
                ("交还人", common.user_name(connection, str(handback.get("actor", "")))),
                ("交还时间", common.short_datetime(handback.get("created_at"))),
                (
                    "交还说明",
                    handback.get("reason", "")
                    or str(permit.get("handback_note", "") or "—"),
                ),
            ]
        )

    st.markdown("**关闭前检查**")
    for label, ok, detail in _closeout_gate(connection, permit, user):
        line = f"{'✓' if ok else '✕'}　{label}"
        if not ok and detail:
            line += f"　——　{detail}"
        st.markdown(line)
    st.caption("未关闭的隐患会阻止许可关闭；这是规则，不是提示。")


def _render_linked_hazards(
    connection: sqlite3.Connection, permit: Mapping[str, Any]
) -> None:
    from services import hazard_service

    hazards = [
        hazard_service.get_hazard(connection, str(hazard_id))
        for hazard_id in (permit.get("linked_hazard_ids") or ())
    ]
    hazards = [hazard for hazard in hazards if hazard is not None]
    if not hazards:
        st.caption("暂无关联隐患。")
        return
    st.dataframe(
        [
            {
                "隐患编号": hazard.get("id", ""),
                "描述": hazard.get("title", ""),
                "风险": common.risk_label(hazard.get("risk_level")),
                "状态": common.hazard_status_label(hazard.get("status")),
                "整改负责人": common.user_name(
                    connection, str(hazard.get("owner_id", ""))
                ),
                "整改截止": common.short_date(hazard.get("due_at")),
            }
            for hazard in hazards
        ],
        hide_index=True,
        width="stretch",
    )
    open_count = sum(
        1 for hazard in hazards if str(hazard.get("status", "")) != "closed"
    )
    if open_count:
        st.warning(f"仍有 {open_count} 条未关闭隐患，作业许可不能关闭。")
    for hazard in hazards:
        st.button(
            f"打开 {hazard.get('id', '')} 整改详情",
            key=f"open_hazard_from_permit_{hazard.get('id', '')}",
            on_click=common.open_detail,
            args=(common.PAGE_HAZARDS, "hazard", str(hazard.get("id", ""))),
        )


def _render_pack_source(
    connection: sqlite3.Connection, permit: Mapping[str, Any]
) -> None:
    """Lightweight provenance section: which confirmed pack created this permit."""
    source = safety_review_service.get_permit_source(
        connection, str(permit.get("id", ""))
    )
    if source is None:
        return
    draft_id = str(source.get("draft_id") or "—")
    pack_id = str(source.get("pack_id") or "—")
    version = int(source.get("pack_version") or 0)
    snapshot = source.get("snapshot") or {}
    tags = "、".join(str(item) for item in (snapshot.get("confirmed_risk_tags") or ()))
    with st.expander("安全准备来源", expanded=False):
        widgets.key_values(
            [
                ("作业准备草稿", draft_id),
                ("Safety Review Pack", f"{pack_id} · v{version}"),
                ("EHS确认人", common.user_name(connection, str(source.get("confirmed_by") or ""))),
                ("EHS确认时间", common.short_datetime(source.get("confirmed_at"))),
                ("确认风险标签", tags or "—"),
                ("正式许可状态", common.permit_status_label(permit.get("status"))),
            ]
        )
        st.caption(
            "已确认的JSA与SDS/SOP证据随该来源冻结；本许可仍须走完整的审核与审批流程。"
        )
        def _open_source_draft(draft_id: str = draft_id) -> None:
            # Widget-bound nav keys may only be written inside a callback.
            safety_review_ui.open_draft(draft_id)
            st.session_state[CREATE_KEY] = False
            common.close_detail(common.PAGE_PERMITS)

        st.button(
            "打开作业准备记录",
            key=f"open_source_draft_{permit.get('id', '')}",
            on_click=_open_source_draft,
        )


def render_detail(
    connection: sqlite3.Connection,
    user: Mapping[str, Any],
    permit_id: str,
) -> None:
    """Render one permit detail page."""
    permit = permit_service.get_permit(connection, permit_id)
    st.button(
        "← 返回作业许可列表",
        on_click=common.close_detail,
        args=(common.PAGE_PERMITS,),
    )
    if permit is None:
        st.error(f"未找到作业许可 {permit_id}。")
        return

    events = _events(connection, permit_id)
    _render_header(connection, permit)
    st.divider()
    product.render_flow_strip(permit)
    product.render_role_hint(permit, user)
    st.divider()
    _render_pack_source(connection, permit)
    st.divider()
    _render_action_area(connection, permit, user)

    status = str(permit.get("status", ""))
    if status == permit_state.PERMIT_SUSPENDED:
        st.divider()
        _render_suspension(connection, permit, events)
    if status == permit_state.PERMIT_CLOSEOUT_REVIEW:
        st.divider()
        _render_closeout(connection, permit, user, events)

    st.divider()

    with st.expander("基本信息", expanded=False):
        widgets.key_values(
            [
                ("作业类型", permit.get("permit_type", "")),
                ("区域", permit.get("area", "") or "—"),
                ("设备", permit.get("equipment", "") or "—"),
                ("有效期", f"{common.short_date(permit.get('valid_from'))} ~ "
                           f"{common.short_date(permit.get('valid_to'))}"),
                ("残余风险", common.risk_label(_risk_of(permit))),
                ("数据性质", permit.get("data_label", "") or "—"),
            ]
        )
        st.markdown(f"**控制措施：**　{permit.get('control_measures', '') or '—'}")
        steps = list(permit.get("steps") or ())
        if steps:
            st.markdown("**作业步骤**")
            st.dataframe(
                [
                    {"序号": item.get("step_no"), "步骤": item.get("name", ""),
                     "说明": item.get("note", "") or "—"}
                    for item in steps
                ],
                hide_index=True,
                width="stretch",
            )

    with st.expander(
        "化学品与 SDS 证据",
        expanded=not _section_is_future(permit, "chemicals")
        and common.permit_stage_label(permit.get("status")) in ("申请与提交", "EHS审核"),
    ):
        if _section_is_future(permit, "chemicals"):
            st.caption("该阶段尚未开始。")
        else:
            _render_chemicals(connection, permit)

    with st.expander(
        "JSA 风险评估",
        expanded=not _section_is_future(permit, "jsa")
        and common.permit_stage_label(permit.get("status")) == "EHS审核",
    ):
        if _section_is_future(permit, "jsa"):
            st.caption("该阶段尚未开始。")
        else:
            _render_jsa(permit)

    with st.expander(
        "审核与审批记录",
        expanded=common.permit_stage_label(permit.get("status")) == "作业审批",
    ):
        if _section_is_future(permit, "approval"):
            st.caption("该阶段尚未开始。")
        else:
            _render_approval(events)

    with st.expander(
        "开工前检查",
        expanded=common.permit_stage_label(permit.get("status")) == "开工准备",
    ):
        if _section_is_future(permit, "prestart"):
            st.caption("该阶段尚未开始。")
        else:
            _render_prestart(connection, permit)

    with st.expander(
        "执行 / 暂停 / 恢复 / 交还",
        expanded=common.permit_stage_label(permit.get("status")) == "作业执行",
    ):
        if _section_is_future(permit, "execution"):
            st.caption("该阶段尚未开始。")
        else:
            _render_execution(permit, events)

    with st.expander(
        "关联隐患",
        expanded=bool(permit.get("linked_hazard_ids")),
    ):
        product.render_hazard_subflow()
        st.divider()
        _render_linked_hazards(connection, permit)

    with st.expander("操作记录（Activity Timeline）", expanded=False):
        widgets.render_activity_timeline(events)


__all__ = [
    "NEXT_ACTION_LABELS",
    "TASK_TEXT",
    "permit_is_overdue",
    "permit_next_action",
    "render_detail",
    "render_list",
]

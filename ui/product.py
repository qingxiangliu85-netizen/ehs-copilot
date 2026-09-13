"""Product identity, the guided golden Demo and the permit main-flow strip.

Everything here is presentation vocabulary for a first-time visitor: what the
product is, what it is *not*, which work types the Demo covers, how the six-step
permit flow reads, and which role the story suggests next.  No state transition
and no permission rule is implemented here — the pages only forward clicks to
``services.*``.
"""

from __future__ import annotations

import html
from typing import Any, Mapping

import streamlit as st

from workflow import permit_state

from . import common

# --------------------------------------------------------------------------- #
# What this product is
# --------------------------------------------------------------------------- #

PRODUCT_NAME = "EHS Copilot"
PRODUCT_CATEGORY = "高风险非例行作业许可与隐患闭环工作台"
PRODUCT_ALIAS = "Digital Permit to Work / Control of Work"

PRODUCT_HERO_TITLE = "高风险作业，从申请到关闭，全程可追踪"
PRODUCT_HERO_DESCRIPTION = (
    "集中管理作业许可、SDS/JSA 证据、人工审批、隐患整改与审计记录。"
)
PRODUCT_ONE_LINER = "从作业申请到整改关闭，一张许可单管理高风险作业全过程。"
PRODUCT_AI_SCOPE = "AI 负责证据整理和草稿，安全责任决策由人工完成。"
PRODUCT_BOUNDARY = (
    "作业许可是正式的高风险作业控制与沟通流程，不代表「有许可就一定安全」："
    "人工风险评估、明确责任角色、授权、作业期限、开工前检查、交还关闭与审计留痕"
    "缺一不可。"
)

# --------------------------------------------------------------------------- #
# The guided golden Demo
# --------------------------------------------------------------------------- #

GOLDEN_PERMIT_ID = "PERMIT-DEMO-001"
GOLDEN_PERMIT_TITLE = "槽罐区管道 HF 清洗作业（模拟）"
GOLDEN_ENTRY_LABEL = "体验3分钟完整流程"
GOLDEN_ENTRY_CAPTION = (
    "跟随一条模拟非例行作业，体验 申请 → EHS审核 → 审批 → 开工 → 整改 → 关闭。"
)

# --------------------------------------------------------------------------- #
# Work types covered by the Demo
# --------------------------------------------------------------------------- #
# These are simulated Demo records only.  They do NOT claim that the prototype
# ships a regulation-grade template for every special work type.

WORK_TYPES: tuple[str, ...] = (
    "危化品作业",
    "动火作业",
    "受限空间作业",
    "电气隔离作业",
    "高处作业",
    "开挖作业",
    "其他高风险作业",
)
UNKNOWN_WORK_TYPE = "其他非例行作业"


def work_type_label(permit: Mapping[str, Any] | None) -> str:
    """Return the business work type of one permit."""
    text = str((permit or {}).get("permit_type", "") or "").strip()
    return text or UNKNOWN_WORK_TYPE


# --------------------------------------------------------------------------- #
# The hazard sub-flow of the execution stage
# --------------------------------------------------------------------------- #

HAZARD_SUBFLOW: tuple[str, ...] = (
    "现场执行",
    "发现隐患",
    "指派整改",
    "提交整改",
    "EHS验证",
    "隐患关闭",
    "继续作业 / 关闭许可",
)

HAZARD_SUBFLOW_CAPTION = (
    "隐患不是另一个孤立模块，而是作业执行过程中的异常闭环；"
    "只有 EHS 验证通过后隐患才关闭，未关闭隐患会阻止许可关闭。"
)

# --------------------------------------------------------------------------- #
# Which role the story suggests next (a hint, never an automatic decision)
# --------------------------------------------------------------------------- #

_NEXT_ROLE: dict[str, tuple[str, str]] = {
    permit_state.PERMIT_DRAFT: ("作业申请人", "补全材料并提交EHS审核"),
    permit_state.PERMIT_RETURNED: ("作业申请人", "按退回意见修改后重新提交EHS审核"),
    permit_state.PERMIT_EHS_REVIEW: ("EHS审核人", "审核 SDS 证据与 JSA，给出 EHS 结论"),
    permit_state.PERMIT_APPROVAL_PENDING: ("作业审批人", "批准或驳回该作业许可"),
    permit_state.PERMIT_APPROVED: ("作业负责人", "完成开工前检查并开始作业"),
    permit_state.PERMIT_ACTIVE: ("作业负责人", "现场执行；发现隐患时转入整改闭环"),
    permit_state.PERMIT_SUSPENDED: ("作业负责人", "确认条件后恢复作业"),
    permit_state.PERMIT_CLOSEOUT_REVIEW: ("EHS审核人", "确认关联隐患已关闭后闭合许可"),
    permit_state.PERMIT_CLOSED: ("", "该作业许可已闭环，无后续动作"),
    permit_state.PERMIT_CANCELLED: ("", "该作业许可已取消，无后续动作"),
    permit_state.PERMIT_EXPIRED: ("", "该作业许可已过期，无后续动作"),
}

# Hint role → Demo persona id (used by the one-click switch; Demo only).
PERSONA_FOR_ROLE: dict[str, str] = {
    "作业申请人": "DEMO-APPLICANT-01",
    "EHS审核人": "DEMO-EHS-01",
    "作业审批人": "DEMO-APPROVER-01",
    "作业负责人": "DEMO-OWNER-01",
    "整改负责人": "DEMO-OWNER-01",
}


def next_role_hint(permit: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(role label, what that role does next)`` for one permit."""
    status = str((permit or {}).get("status", "")).strip()
    return _NEXT_ROLE.get(status, ("", ""))


def switch_persona(user_id: str) -> None:
    """Widget callback: switch the Demo identity (runs before any widget)."""
    st.session_state[common.USER_KEY] = str(user_id)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

_BRAND_BLUE = "#1358a6"


def render_hero() -> None:
    """Render the first-screen product introduction and the golden Demo entry."""
    st.markdown(
        '<div style="border:1px solid #dfe3e8;border-radius:14px;padding:18px 20px;'
        'background:#f8fafc;">'
        f'<div style="font-size:12px;font-weight:700;letter-spacing:.10em;'
        f'color:{_BRAND_BLUE};">{html.escape(PRODUCT_NAME)}'
        f'　·　{html.escape(PRODUCT_CATEGORY)}</div>'
        '<div style="font-size:26px;font-weight:700;color:#1f2328;margin:6px 0 8px 0;'
        f'line-height:1.35;">{html.escape(PRODUCT_HERO_TITLE)}</div>'
        '<div style="font-size:14px;color:#3f4a5a;line-height:1.75;">'
        f'{html.escape(PRODUCT_HERO_DESCRIPTION)}</div>'
        '<div style="font-size:13px;color:#3f4a5a;line-height:1.75;margin-top:8px;'
        f'font-weight:600;">{html.escape(PRODUCT_ONE_LINER)}</div>'
        '<div style="font-size:12px;color:#5b6472;line-height:1.75;margin-top:8px;">'
        f'{html.escape(PRODUCT_AI_SCOPE)}</div>'
        "</div>",
        unsafe_allow_html=True,
    )
    st.write("")

    columns = st.columns([1.4, 3.6])
    columns[0].button(
        GOLDEN_ENTRY_LABEL,
        type="primary",
        width="stretch",
        key="golden_demo_entry",
        on_click=common.open_detail,
        args=(common.PAGE_PERMITS, "permit", GOLDEN_PERMIT_ID),
        help="打开黄金示例作业许可，并按页面提示逐角色走完 申请 → 关闭 的完整流程。",
    )
    columns[1].caption(str(GOLDEN_ENTRY_CAPTION))
    st.caption(
        f"黄金示例：{GOLDEN_PERMIT_TITLE}　｜　"
        "每完成一步，详情页会提示下一个建议切换的身份（Demo 仅提示，不代替你做决定）。"
    )


_FLOW_TONES: dict[str, tuple[str, str, str]] = {
    common.STAGE_DONE: ("#e7f6ec", "#1c7c46", "✓ 已完成"),
    common.STAGE_CURRENT: ("#e7f1fd", _BRAND_BLUE, "← 当前"),
    common.STAGE_TODO: ("#f2f4f7", "#8b95a5", "待开始"),
    common.STAGE_STOPPED: ("#fdeceb", "#b42318", "已终止"),
}


def flow_strip_html(flow: Mapping[str, Any]) -> str:
    """Return the six-step main flow as one HTML fragment."""
    stages = list(flow.get("stages") or ())
    parts: list[str] = []
    for position, stage in enumerate(stages):
        state = str(stage.get("state", common.STAGE_TODO))
        background, colour, marker = _FLOW_TONES.get(
            state, _FLOW_TONES[common.STAGE_TODO]
        )
        border = (
            f"2px solid {colour}"
            if state in (common.STAGE_CURRENT, common.STAGE_STOPPED)
            else "1px solid #e3e7ec"
        )
        weight = "700" if state == common.STAGE_CURRENT else "600"
        parts.append(
            f'<div style="display:flex;flex-direction:column;gap:2px;'
            f'background:{background};color:{colour};border:{border};'
            f'border-radius:10px;padding:8px 12px;min-width:104px;">'
            f'<div style="font-size:11px;opacity:.85;">第 {position + 1} 步</div>'
            f'<div style="font-size:13px;font-weight:{weight};">'
            f"{html.escape(str(stage.get('label', '')))}</div>"
            f'<div style="font-size:10px;opacity:.85;">{html.escape(marker)}</div>'
            "</div>"
        )
        if position < len(stages) - 1:
            parts.append(
                '<div style="display:flex;align-items:center;color:#8b95a5;'
                'font-size:16px;padding:0 2px;">→</div>'
            )
    return (
        '<div style="display:flex;flex-wrap:wrap;gap:2px;align-items:stretch;">'
        + "".join(parts)
        + "</div>"
    )


def render_flow_strip(permit: Mapping[str, Any]) -> None:
    """Render the permit main-flow strip with the current stage highlighted."""
    flow = common.permit_flow(permit)
    st.markdown("**主流程进度**")
    st.markdown(flow_strip_html(flow), unsafe_allow_html=True)
    st.caption(str(flow.get("position_text", "")))


def subflow_html(steps: tuple[str, ...] | list[str] = HAZARD_SUBFLOW) -> str:
    """Return a compact arrow-separated flow as one HTML fragment."""
    joined = (
        '<span style="color:#8b95a5;padding:0 4px;">→</span>'.join(
            f'<span style="display:inline-block;background:#f2f4f7;color:#3f4a5a;'
            f"border-radius:999px;padding:3px 10px;font-size:12px;"
            f'font-weight:600;">{html.escape(str(step))}</span>'
            for step in steps
        )
    )
    return f'<div style="line-height:2.2;">{joined}</div>'


def render_hazard_subflow() -> None:
    """Render the execution-stage hazard sub-flow (发现隐患 → … → 关闭)."""
    st.markdown("**执行阶段的隐患支线**")
    st.markdown(subflow_html(), unsafe_allow_html=True)
    st.caption(HAZARD_SUBFLOW_CAPTION)


def render_role_hint(permit: Mapping[str, Any], user: Mapping[str, Any]) -> None:
    """Render 「下一步建议切换为：…」 plus a Demo-only one-click switch."""
    role, todo = next_role_hint(permit)
    if not role:
        st.caption(todo or "该作业许可已闭环。")
        return
    persona_id = PERSONA_FOR_ROLE.get(role, "")
    current_id = str(user.get("id", ""))
    if persona_id and persona_id != current_id:
        columns = st.columns([2.2, 2.8])
        columns[0].button(
            f"切换为：{role}（Demo）",
            key=f"switch_role_{permit.get('id', '')}_{persona_id}",
            on_click=switch_persona,
            args=(persona_id,),
            width="stretch",
            help="只切换 Demo 身份，不会替你做出任何审批或验证决定。",
        )
        columns[1].caption(f"下一步建议切换为：**{role}** —— {todo}")
        return
    st.caption(f"下一步建议角色：**{role}**（当前身份即为该角色）—— {todo}")


__all__ = [
    "GOLDEN_ENTRY_CAPTION",
    "GOLDEN_ENTRY_LABEL",
    "GOLDEN_PERMIT_ID",
    "GOLDEN_PERMIT_TITLE",
    "HAZARD_SUBFLOW",
    "HAZARD_SUBFLOW_CAPTION",
    "PERSONA_FOR_ROLE",
    "PRODUCT_AI_SCOPE",
    "PRODUCT_ALIAS",
    "PRODUCT_BOUNDARY",
    "PRODUCT_CATEGORY",
    "PRODUCT_HERO_DESCRIPTION",
    "PRODUCT_HERO_TITLE",
    "PRODUCT_NAME",
    "PRODUCT_ONE_LINER",
    "UNKNOWN_WORK_TYPE",
    "WORK_TYPES",
    "flow_strip_html",
    "next_role_hint",
    "render_flow_strip",
    "render_hazard_subflow",
    "render_hero",
    "render_role_hint",
    "subflow_html",
    "switch_persona",
    "work_type_label",
]

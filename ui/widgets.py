"""Small business-language presentation primitives.

Risk chips, status chips, definition lists, action buttons and the activity
timeline.  Nothing here reads or writes domain state.
"""

from __future__ import annotations

import html
from typing import Any, Iterable, Mapping, Sequence

import streamlit as st

from workflow.actions import Action, primary_action, secondary_actions

from . import common

# Business wording for every operation recorded in ``audit_events``.
AUDIT_ACTION_LABELS: dict[str, str] = {
    "permit.created": "新建作业许可",
    "permit.imported": "导入历史作业许可",
    "permit.evidence_added": "追加安全证据",
    "permit.assign_owner": "变更作业负责人",
    "permit.assign_ehs_reviewer": "变更EHS审核人",
    "permit.assign_approver": "变更批准人",
    "permit.submit": "提交EHS审核",
    "permit.resubmit": "重新提交审核",
    "permit.return_draft": "退回申请人",
    "permit.confirm": "EHS审核确认并提交审批",
    "permit.approve": "批准作业",
    "permit.reject": "驳回作业",
    "permit.prestart_confirm": "完成开工检查并开始作业",
    "permit.suspend": "暂停作业",
    "permit.resume": "恢复作业",
    "permit.complete_work": "作业完成并交还现场",
    "permit.return_to_active": "退回继续执行",
    "permit.close": "关闭作业许可",
    "permit.cancel": "取消作业许可",
    "permit.expire": "作业许可过期",
    "hazard.created": "新建隐患",
    "hazard.imported": "导入历史隐患",
    "hazard.assign": "指派整改负责人",
    "hazard.assign_verifier": "指派验证人",
    "hazard.start": "开始整改",
    "hazard.submit_rectification": "提交整改证据",
    "hazard.verify_pass": "验证通过并关闭隐患",
    "hazard.verify_fail": "退回整改",
    "hazard.reopen": "重新打开隐患",
    "demo_user.seeded": "初始化 Demo 身份",
}

ENTITY_LABELS: dict[str, str] = {
    "permit": "作业许可",
    "hazard": "隐患",
    "demo_user": "Demo 身份",
}

_STATUS_TONES: dict[str, tuple[str, str]] = {
    "pending": ("#fdf3e0", "#9a6b00"),
    "active": ("#e7f1fd", "#1358a6"),
    "done": ("#e7f6ec", "#1c7c46"),
    "stopped": ("#fdeceb", "#b42318"),
    "muted": common.NEUTRAL_CHIP,
}

PERMIT_STATUS_TONES: dict[str, str] = {
    "draft": "muted",
    "returned": "stopped",
    "ehs_review": "pending",
    "approval_pending": "pending",
    "approved": "pending",
    "active": "active",
    "suspended": "stopped",
    "closeout_review": "pending",
    "closed": "done",
    "cancelled": "muted",
    "expired": "stopped",
}

HAZARD_STATUS_TONES: dict[str, str] = {
    "open": "pending",
    "assigned": "pending",
    "in_progress": "active",
    "verification_pending": "pending",
    "reopened": "stopped",
    "closed": "done",
}


def chip(text: str, colors: tuple[str, str] | None = None) -> str:
    """Return one inline chip as HTML."""
    background, colour = colors or common.NEUTRAL_CHIP
    return (
        f'<span style="display:inline-block;background:{background};color:{colour};'
        "padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;"
        f'line-height:1.7;margin:0 6px 4px 0;">{html.escape(str(text))}</span>'
    )


def chips_html(items: Iterable[tuple[str, tuple[str, str] | None]]) -> str:
    """Return a sequence of chips as one HTML fragment."""
    return "".join(chip(text, colors) for text, colors in items)


def render_chips(items: Iterable[tuple[str, tuple[str, str] | None]]) -> None:
    """Render a row of chips."""
    fragment = chips_html(items)
    if fragment:
        st.markdown(fragment, unsafe_allow_html=True)


def risk_chip(level: Any) -> tuple[str, tuple[str, str]]:
    """Return the ``高风险`` chip for one risk level."""
    text = str(level or "").strip()
    return common.risk_label(text), common.RISK_COLORS.get(text, common.NEUTRAL_CHIP)


def permit_status_chip(status: Any) -> tuple[str, tuple[str, str]]:
    """Return the business status chip of one permit."""
    key = str(status or "").strip()
    tone = _STATUS_TONES[PERMIT_STATUS_TONES.get(key, "muted")]
    return common.permit_status_label(key), tone


def hazard_status_chip(status: Any) -> tuple[str, tuple[str, str]]:
    """Return the business status chip of one hazard."""
    key = str(status or "").strip()
    tone = _STATUS_TONES[HAZARD_STATUS_TONES.get(key, "muted")]
    return common.hazard_status_label(key), tone


def neutral_chip(text: str) -> tuple[str, tuple[str, str]]:
    """Return a plain grey chip."""
    return str(text), common.NEUTRAL_CHIP


def overdue_chip(is_overdue: bool) -> tuple[str, tuple[str, str]] | None:
    """Return the 已逾期 chip, or ``None``."""
    if not is_overdue:
        return None
    return "已逾期", _STATUS_TONES["stopped"]


def validity_chip(validity: Mapping[str, Any]) -> tuple[str, tuple[str, str]]:
    """Return the 许可有效期 chip.

    This is a *time* signal (有效 / 即将到期 / 已过期) and is intentionally kept
    separate from the risk-severity chip so the two are never read as one thing.
    """
    label = str(validity.get("label", "") or "—")
    tone = validity.get("tone")
    colours = tone if isinstance(tone, tuple) and len(tone) == 2 else common.NEUTRAL_CHIP
    return f"许可有效期：{label}", colours


def key_values(pairs: Sequence[tuple[str, Any]]) -> None:
    """Render business facts as a compact definition grid."""
    columns = st.columns(2)
    for index, (label, value) in enumerate(pairs):
        with columns[index % 2]:
            text = str(value) if value not in (None, "") else "—"
            st.markdown(f"**{html.escape(str(label))}**　{html.escape(text)}")


def render_actions(
    actions: Sequence[Action],
    *,
    key_prefix: str,
    disabled: Mapping[str, bool] | None = None,
) -> str | None:
    """Render one primary plus at most two secondary buttons.

    Returns the code of the button that was pressed on this run, or ``None``.
    """
    if not actions:
        st.caption("当前身份在这一状态下没有可执行的动作。")
        return None
    blocked = dict(disabled or {})
    main = primary_action(list(actions))
    others = secondary_actions(list(actions), limit=2)
    columns = st.columns([1.7] + [1.0] * len(others))
    clicked: str | None = None
    if main is not None:
        if columns[0].button(
            main.label,
            type="primary",
            width="stretch",
            key=f"{key_prefix}_do_{main.code}",
            disabled=bool(blocked.get(main.code, False)),
        ):
            clicked = main.code
    for index, action in enumerate(others):
        if columns[index + 1].button(
            action.label,
            width="stretch",
            key=f"{key_prefix}_do_{action.code}",
            disabled=bool(blocked.get(action.code, False)),
        ):
            clicked = clicked or action.code
    return clicked


def reason_input(
    *,
    label: str,
    key: str,
    placeholder: str = "",
    height: int = 90,
) -> str:
    """Render the mandatory reason field for one action."""
    return st.text_area(
        label,
        key=key,
        placeholder=placeholder or "请说明原因；该原因会写入审计记录。",
        height=height,
    )


def render_activity_timeline(events: Sequence[Mapping[str, Any]]) -> None:
    """Render an append-only operation record as a readable timeline."""
    if not events:
        st.caption("暂无操作记录。")
        return
    blocks: list[str] = []
    total = len(events)
    for index, event in enumerate(events):
        action = str(event.get("action", ""))
        label = AUDIT_ACTION_LABELS.get(action, action or "操作")
        actor = str(event.get("actor", "") or "system")
        reason = str(event.get("reason", "") or "")
        entity_type = str(event.get("entity_type", "") or "")
        from_state = str(event.get("from_state", "") or "")
        to_state = str(event.get("to_state", "") or "")
        stamp = common.short_datetime(event.get("created_at"))
        transition = ""
        if from_state or to_state:
            transition = (
                f"{common.business_state_label(entity_type, from_state)} → "
                f"{common.business_state_label(entity_type, to_state)}"
            )
        detail_parts = [f"操作者：{html.escape(actor)}", html.escape(stamp)]
        if transition:
            detail_parts.append(html.escape(transition))
        if reason:
            detail_parts.append(f"原因：{html.escape(reason)}")
        connector = (
            ""
            if index == total - 1
            else '<div style="width:2px;flex:1;min-height:16px;background:#e5e7eb;"></div>'
        )
        blocks.append(
            '<div style="display:flex;gap:10px;align-items:stretch;">'
            '<div style="display:flex;flex-direction:column;align-items:center;width:14px;">'
            '<div style="width:10px;height:10px;border-radius:50%;background:#8b95a5;'
            "margin-top:5px;flex:0 0 auto;\"></div>"
            f"{connector}"
            "</div>"
            '<div style="flex:1;padding-bottom:12px;">'
            f'<div style="font-size:13px;font-weight:600;color:#1f2328;">{label}</div>'
            f'<div style="font-size:12px;color:#5b6472;line-height:1.6;">'
            + "　｜　".join(detail_parts)
            + "</div></div></div>"
        )
    st.markdown(
        '<div style="background:#f8f9fb;border:1px solid #e5e7eb;border-radius:10px;'
        'padding:12px 14px 2px 14px;">' + "".join(blocks) + "</div>",
        unsafe_allow_html=True,
    )


def section_open(label: str, *, expanded: bool, key: str) -> Any:
    """Open a collapsible section (thin wrapper so pages stay readable)."""
    return st.expander(label, expanded=expanded, key=key)


def empty_state(message: str) -> None:
    """Render a short, business-language empty state."""
    st.info(message)


__all__ = [
    "AUDIT_ACTION_LABELS",
    "ENTITY_LABELS",
    "chip",
    "chips_html",
    "empty_state",
    "hazard_status_chip",
    "key_values",
    "neutral_chip",
    "overdue_chip",
    "permit_status_chip",
    "reason_input",
    "render_actions",
    "render_activity_timeline",
    "render_chips",
    "risk_chip",
    "section_open",
    "validity_chip",
]

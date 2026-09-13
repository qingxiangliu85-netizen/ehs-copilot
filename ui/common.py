"""Session state, database access and business vocabulary for the V5 UI.

Everything here is presentation support: it opens the prototype store, keeps the
signed-in Demo persona, maps domain states to business words and holds the
navigation callbacks.  No state transition and no permission rule is
implemented here.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Mapping

import streamlit as st

import db
import demo_seed
from services import hazard_service, permit_service, persona_service
from workflow import hazard_state, permit_state, roles, sla

# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #

PAGE_TODAY = "今日工作台"
PAGE_PERMITS = "作业许可"
PAGE_HAZARDS = "隐患与整改"
PAGE_OVERVIEW = "风险看板"
PAGE_RECORDS = "资料与审计"

NAV_PAGES: tuple[str, ...] = (
    PAGE_TODAY,
    PAGE_PERMITS,
    PAGE_HAZARDS,
    PAGE_OVERVIEW,
    PAGE_RECORDS,
)

DEFAULT_PAGE = PAGE_TODAY

# The one and only disclaimer shown to a business user (the sidebar).
DEMO_NOTICE = (
    "Demo环境：当前人员、SLA、作业及部分安全资料为模拟数据，仅用于作品集功能演示。"
)
DEMO_IDENTITY_NOTICE = "Demo 身份模拟，不是真实登录或企业账号体系。"

NAV_KEY = "nav_page"
USER_KEY = "demo_user_id"
DETAIL_KEY = "open_detail"
FLASH_KEY = "_flash"
SDS_STATE_KEY = "_sds_state"
SDS_ERROR_KEY = "_sds_error"

# (user id, business label) — the personas offered by the switcher.
# One Demo user plays both the permit owner and the rectification owner, so the
# label names both capacities: the permit detail calls them 作业负责人 while a
# hazard calls them 整改负责人, and a reader must recognise it as the same person.
PERSONA_CHOICES: tuple[tuple[str, str], ...] = (
    ("DEMO-APPLICANT-01", "作业申请人"),
    ("DEMO-EHS-01", "EHS审核人"),
    ("DEMO-APPROVER-01", "作业审批人"),
    ("DEMO-OWNER-01", "作业负责人 / 整改负责人"),
    ("DEMO-ADMIN-01", "Demo Admin"),
)
# The product opens as the persona that shows the most value: the EHS reviewer
# reads the SDS/JSA evidence, reviews the permit and verifies the rectification.
# The administrator sees every task at once, which is confusing on a first visit,
# so it stays available for global inspection only.
DEFAULT_USER_ID = "DEMO-EHS-01"

# Only these roles may start/handle a rectification, so only they can be
# offered as a rectification owner.
OWNER_ROLES: tuple[str, ...] = (roles.ROLE_ACTION_OWNER, roles.ROLE_DEMO_ADMIN)


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    """Yield a ready prototype store and always close it afterwards.

    The Demo seed is idempotent: it only inserts when the store is still empty,
    so a reviewer's own clicks are never overwritten.
    """
    connection = db.open_database()
    try:
        demo_seed.seed_demo_data(connection)
        yield connection
    finally:
        connection.close()


def current_user(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return the signed-in Demo persona, falling back to the administrator.

    The value itself is owned by the sidebar widget (``key=USER_KEY``), so this
    helper only reads it — writing to a widget-bound key after the widget has
    been instantiated is forbidden by Streamlit.
    """
    user_id = str(st.session_state.get(USER_KEY) or DEFAULT_USER_ID)
    user = persona_service.get_user(connection, user_id)
    if user is None:
        user = persona_service.get_user(connection, DEFAULT_USER_ID)
    if user is None:
        raise RuntimeError("Demo 用户未初始化，无法继续。")
    return user


def list_personas(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return the fixed persona list in business order (switcher options)."""
    users: list[dict[str, Any]] = []
    for user_id, _ in PERSONA_CHOICES:
        user = persona_service.get_user(connection, user_id)
        if user is not None:
            users.append(user)
    return users


def persona_label(user: Mapping[str, Any]) -> str:
    """Return ``作业申请人 · 作业申请人甲（Demo）`` for one user row."""
    business = dict(PERSONA_CHOICES).get(str(user.get("id", "")), "")
    display = str(user.get("display_name", ""))
    return f"{business} · {display}" if business else display


def user_name(connection: sqlite3.Connection, user_id: str) -> str:
    """Return a readable name for a stored user id."""
    text = str(user_id or "").strip()
    if not text:
        return "—"
    user = persona_service.get_user(connection, text)
    if user is None:
        return text
    return str(user["display_name"])


# --------------------------------------------------------------------------- #
# Business vocabulary
# --------------------------------------------------------------------------- #

PERMIT_STATUS_LABELS: dict[str, str] = {
    permit_state.PERMIT_DRAFT: "草稿",
    permit_state.PERMIT_EHS_REVIEW: "待EHS审核",
    permit_state.PERMIT_APPROVAL_PENDING: "待审批",
    permit_state.PERMIT_APPROVED: "已批准（待开工）",
    permit_state.PERMIT_ACTIVE: "执行中",
    permit_state.PERMIT_SUSPENDED: "已暂停",
    permit_state.PERMIT_CLOSEOUT_REVIEW: "待关闭",
    permit_state.PERMIT_CLOSED: "已关闭",
    permit_state.PERMIT_RETURNED: "已驳回",
    permit_state.PERMIT_CANCELLED: "已取消",
    permit_state.PERMIT_EXPIRED: "已过期",
}

HAZARD_STATUS_LABELS: dict[str, str] = {
    hazard_state.HAZARD_OPEN: "待指派",
    hazard_state.HAZARD_ASSIGNED: "待整改",
    hazard_state.HAZARD_IN_PROGRESS: "整改中",
    hazard_state.HAZARD_VERIFICATION_PENDING: "待验证",
    hazard_state.HAZARD_REOPENED: "已退回整改",
    hazard_state.HAZARD_CLOSED: "已关闭",
}

# The permit main flow, exactly as a business reader sees it.  This is a display
# mapping only: which status may move to which status is decided by
# ``workflow.permit_state`` and is never re-implemented here.
PERMIT_STAGE_ORDER: tuple[str, ...] = (
    "申请",
    "EHS审核",
    "审批",
    "开工前检查",
    "执行",
    "交还/关闭",
)

PERMIT_STAGE_LABELS: dict[str, str] = {
    permit_state.PERMIT_DRAFT: "申请",
    permit_state.PERMIT_RETURNED: "申请",
    permit_state.PERMIT_EHS_REVIEW: "EHS审核",
    permit_state.PERMIT_APPROVAL_PENDING: "审批",
    permit_state.PERMIT_APPROVED: "开工前检查",
    permit_state.PERMIT_ACTIVE: "执行",
    permit_state.PERMIT_SUSPENDED: "执行",
    permit_state.PERMIT_CLOSEOUT_REVIEW: "交还/关闭",
    permit_state.PERMIT_CLOSED: "交还/关闭",
    permit_state.PERMIT_CANCELLED: "交还/关闭",
    permit_state.PERMIT_EXPIRED: "交还/关闭",
}

ACTIVE_PERMIT_STATUSES: tuple[str, ...] = (
    permit_state.PERMIT_ACTIVE,
    permit_state.PERMIT_SUSPENDED,
)

RISK_LEVELS: tuple[str, ...] = ("低", "中", "高", "重大")

# Severity palette (light surface): severity, not market direction.
RISK_COLORS: dict[str, tuple[str, str]] = {
    "低": ("#e7f6ec", "#1c7c46"),
    "中": ("#fdf3e0", "#9a6b00"),
    "高": ("#fdece0", "#b5540d"),
    "重大": ("#fdeceb", "#b42318"),
}
NEUTRAL_CHIP = ("#eef1f5", "#3f4a5a")


def risk_label(level: Any) -> str:
    """Return ``高风险`` / ``未评估`` for one risk level value."""
    text = str(level or "").strip()
    return f"{text}风险" if text in RISK_LEVELS else "未评估"


def permit_status_label(status: Any) -> str:
    """Return the business label of one permit status."""
    text = str(status or "").strip()
    return PERMIT_STATUS_LABELS.get(text, text or "—")


def hazard_status_label(status: Any) -> str:
    """Return the business label of one hazard status."""
    text = str(status or "").strip()
    return HAZARD_STATUS_LABELS.get(text, text or "—")


def permit_stage_label(status: Any) -> str:
    """Return the business stage of one permit status."""
    text = str(status or "").strip()
    return PERMIT_STAGE_LABELS.get(text, text or "—")


PERMIT_STAGE_INDEX: dict[str, int] = {
    label: index for index, label in enumerate(PERMIT_STAGE_ORDER)
}
# A cancelled / expired permit stops at the last stage; the sections before it
# are history, not future work.
_FINAL_STAGE_INDEX = PERMIT_STAGE_INDEX["交还/关闭"]

STAGE_DONE = "done"
STAGE_CURRENT = "current"
STAGE_TODO = "todo"
STAGE_STOPPED = "stopped"


def permit_stage_index(status: Any) -> int:
    """Return the pipeline position of one permit status."""
    label = permit_stage_label(status)
    return PERMIT_STAGE_INDEX.get(label, _FINAL_STAGE_INDEX)


def permit_flow(permit: Mapping[str, Any]) -> dict[str, Any]:
    """Return the six-step main flow of one permit as display data.

    Every stage is ``done`` / ``current`` / ``todo`` (plus ``stopped`` for a
    cancelled or expired permit); the underlying status still comes from
    ``workflow.permit_state``, so this never becomes a second state machine.
    """
    status = str(permit.get("status", "") or "").strip()
    index = permit_stage_index(status)
    closed = status == permit_state.PERMIT_CLOSED
    terminated = status in (
        permit_state.PERMIT_CANCELLED,
        permit_state.PERMIT_EXPIRED,
    )

    stages: list[dict[str, str]] = []
    for position, label in enumerate(PERMIT_STAGE_ORDER):
        if closed or position < index:
            state = STAGE_DONE
        elif position == index:
            state = STAGE_STOPPED if terminated else STAGE_CURRENT
        else:
            state = STAGE_TODO
        stages.append({"label": label, "state": state})

    if closed:
        position_text = "主流程六步已全部完成（作业许可已闭环）"
    elif terminated:
        position_text = (
            f"主流程在「{PERMIT_STAGE_ORDER[index]}」终止"
            f"（{permit_status_label(status)}）"
        )
    else:
        position_text = (
            f"当前阶段：{PERMIT_STAGE_ORDER[index]}"
            f"（第 {index + 1} / {len(PERMIT_STAGE_ORDER)} 步）"
        )
    return {
        "status": status,
        "index": index,
        "total": len(PERMIT_STAGE_ORDER),
        "stages": stages,
        "current_label": "" if closed or terminated else PERMIT_STAGE_ORDER[index],
        "position_text": position_text,
        "closed": closed,
        "terminated": terminated,
    }


def is_high_risk(level: Any) -> bool:
    """Return whether a risk level counts as 高/重大."""
    return str(level or "").strip() in ("高", "重大")


def is_overdue(due_at: Any, moment: datetime | None = None) -> bool:
    """Return whether one stored deadline has already passed."""
    return sla.is_overdue(due_at, moment)


# --------------------------------------------------------------------------- #
# Permit validity — time, deliberately kept apart from risk (severity)
# --------------------------------------------------------------------------- #
# A permit is valid for a fixed window (特定工作 + 特定时间范围).  Whether it is
# still in force is a *time* fact and is never merged with the work's risk level.

VALIDITY_IN_FORCE = "in_force"
VALIDITY_EXPIRING = "expiring"
VALIDITY_EXPIRED = "expired"
VALIDITY_UNSET = "unset"

VALIDITY_LABELS: dict[str, str] = {
    VALIDITY_IN_FORCE: "有效",
    VALIDITY_EXPIRING: "即将到期",
    VALIDITY_EXPIRED: "已过期",
    VALIDITY_UNSET: "未设置有效期",
}

VALIDITY_TONES: dict[str, tuple[str, str]] = {
    VALIDITY_IN_FORCE: ("#e7f6ec", "#1c7c46"),
    VALIDITY_EXPIRING: ("#fdf3e0", "#9a6b00"),
    VALIDITY_EXPIRED: ("#fdeceb", "#b42318"),
    VALIDITY_UNSET: NEUTRAL_CHIP,
}

# A permit entering its last day (or already past it) is 即将到期.
VALIDITY_EXPIRING_DAYS = 1


def _parse_moment(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def permit_validity(
    permit: Mapping[str, Any] | None, moment: datetime | None = None
) -> dict[str, Any]:
    """Return the 许可有效期 (time) view of one permit.

    ``expired`` uses the same rule as the state machine (``now > valid_to``), so
    the page can never claim a permit is usable when the service would refuse it.
    """
    values = dict(permit or {})
    stamp = moment or now()
    valid_from = _parse_moment(values.get("valid_from"))
    valid_to = _parse_moment(values.get("valid_to"))

    if valid_to is None:
        state = VALIDITY_UNSET
        days_left: int | None = None
    elif stamp > valid_to:
        state = VALIDITY_EXPIRED
        days_left = (valid_to.date() - stamp.date()).days
    else:
        days_left = (valid_to.date() - stamp.date()).days
        state = (
            VALIDITY_EXPIRING
            if days_left <= VALIDITY_EXPIRING_DAYS
            else VALIDITY_IN_FORCE
        )

    from_text = short_datetime(values.get("valid_from")) if valid_from else "—"
    to_text = short_datetime(values.get("valid_to")) if valid_to else "—"
    return {
        "state": state,
        "label": VALIDITY_LABELS[state],
        "tone": VALIDITY_TONES[state],
        "from_text": from_text,
        "to_text": to_text,
        "range_text": f"{from_text} ~ {to_text}",
        "is_expired": state == VALIDITY_EXPIRED,
        "is_expiring": state == VALIDITY_EXPIRING,
        "in_force": state in (VALIDITY_IN_FORCE, VALIDITY_EXPIRING),
        "days_left": days_left,
    }


def business_state_label(entity_type: Any, value: Any) -> str:
    """Map a stored status onto the business word shown to a reader.

    Used for audit transitions (``from_state → to_state``) so a business page
    never prints a raw identifier such as ``closeout_review``.
    """
    kind = str(entity_type or "").strip()
    text = str(value or "").strip()
    if not text:
        return "—"
    if kind == "permit":
        return permit_status_label(text)
    if kind == "hazard":
        return hazard_status_label(text)
    return text


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #


def now() -> datetime:
    """Return the current moment (single place, easy to patch in tests)."""
    return datetime.now()


def short_datetime(value: Any) -> str:
    """Render a stored ISO timestamp as ``YYYY-MM-DD HH:MM``."""
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def short_date(value: Any) -> str:
    """Render a stored ISO timestamp as ``YYYY-MM-DD``."""
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d")
    except ValueError:
        return text[:10] or text


def set_flash(message: str, kind: str = "success") -> None:
    """Queue a one-shot message to show on the next run."""
    st.session_state[FLASH_KEY] = (kind, message)


def render_flash() -> None:
    """Show and clear the queued one-shot message."""
    payload = st.session_state.pop(FLASH_KEY, None)
    if not payload:
        return
    kind, message = payload
    renderer = {
        "success": st.success,
        "warning": st.warning,
        "error": st.error,
        "info": st.info,
    }.get(str(kind), st.info)
    renderer(message)


def open_detail(page: str, entity_type: str, entity_id: str) -> None:
    """Navigate to a detail page (only ever called from a widget callback)."""
    st.session_state[NAV_KEY] = page
    st.session_state[DETAIL_KEY] = {
        "type": str(entity_type),
        "id": str(entity_id),
        "page": str(page),
    }


def close_detail(page: str) -> None:
    """Return from a detail page to its list (widget callback only)."""
    st.session_state[NAV_KEY] = page
    st.session_state[DETAIL_KEY] = {}


def go_to(page: str) -> None:
    """Switch page and clear any open detail (widget callback only)."""
    st.session_state[NAV_KEY] = page
    st.session_state[DETAIL_KEY] = {}


def current_detail(page: str | None = None) -> dict[str, str]:
    """Return the open detail record, or an empty mapping.

    A detail record remembers the page it belongs to, so switching pages in the
    sidebar never shows a detail that belongs somewhere else.
    """
    payload = st.session_state.get(DETAIL_KEY) or {}
    if not isinstance(payload, Mapping):
        return {}
    kind = str(payload.get("type", "")).strip()
    entity_id = str(payload.get("id", "")).strip()
    owner_page = str(payload.get("page", "")).strip()
    if not kind or not entity_id:
        return {}
    if page is not None and owner_page and owner_page != str(page):
        return {}
    return {"type": kind, "id": entity_id, "page": owner_page}


def list_hazards_for_permit(
    connection: sqlite3.Connection, permit_id: str
) -> list[dict[str, Any]]:
    """Return the hazard rows linked to one permit."""
    return hazard_service.list_hazards(connection, permit_id=permit_id, limit=500)


def list_active_owners(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return the Demo users who may be assigned a rectification."""
    return [
        user
        for user in persona_service.list_users(connection)
        if str(user.get("role", "")) in OWNER_ROLES
    ]


def get_permit(connection: sqlite3.Connection, permit_id: str) -> dict[str, Any] | None:
    """Return one permit aggregate."""
    return permit_service.get_permit(connection, permit_id)


def get_hazard(connection: sqlite3.Connection, hazard_id: str) -> dict[str, Any] | None:
    """Return one hazard aggregate."""
    return hazard_service.get_hazard(connection, hazard_id)


__all__ = [
    "ACTIVE_PERMIT_STATUSES",
    "DEFAULT_PAGE",
    "DEFAULT_USER_ID",
    "DEMO_IDENTITY_NOTICE",
    "DEMO_NOTICE",
    "DETAIL_KEY",
    "FLASH_KEY",
    "HAZARD_STATUS_LABELS",
    "NAV_KEY",
    "NAV_PAGES",
    "NEUTRAL_CHIP",
    "OWNER_ROLES",
    "PAGE_HAZARDS",
    "PAGE_OVERVIEW",
    "PAGE_PERMITS",
    "PAGE_RECORDS",
    "PAGE_TODAY",
    "PERSONA_CHOICES",
    "PERMIT_STAGE_INDEX",
    "PERMIT_STAGE_LABELS",
    "PERMIT_STAGE_ORDER",
    "PERMIT_STATUS_LABELS",
    "RISK_COLORS",
    "RISK_LEVELS",
    "SDS_ERROR_KEY",
    "SDS_STATE_KEY",
    "STAGE_CURRENT",
    "STAGE_DONE",
    "STAGE_STOPPED",
    "STAGE_TODO",
    "USER_KEY",
    "VALIDITY_EXPIRED",
    "VALIDITY_EXPIRING",
    "VALIDITY_EXPIRING_DAYS",
    "VALIDITY_IN_FORCE",
    "VALIDITY_LABELS",
    "VALIDITY_TONES",
    "VALIDITY_UNSET",
    "business_state_label",
    "close_detail",
    "current_detail",
    "current_user",
    "database",
    "get_hazard",
    "get_permit",
    "go_to",
    "hazard_status_label",
    "is_high_risk",
    "is_overdue",
    "list_active_owners",
    "list_hazards_for_permit",
    "list_personas",
    "now",
    "open_detail",
    "permit_flow",
    "permit_stage_label",
    "permit_stage_index",
    "permit_status_label",
    "permit_validity",
    "persona_label",
    "render_flash",
    "risk_label",
    "set_flash",
    "short_date",
    "short_datetime",
    "user_name",
]

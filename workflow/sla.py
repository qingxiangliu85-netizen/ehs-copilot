"""Lightweight deadline rules for the V5 prototype.

Demo policy only
----------------
Every number in :data:`SLA_POLICY_DEMO` is a simulated portfolio-demo rule.
It is **not** a legal requirement, an industry standard or a claim about any
real company's EHS procedure.  The prototype uses natural calendar days and
does not model work calendars, holidays, shifts or pause-aware clocks.

Three clocks are kept separate
------------------------------
* ``permits.approval_due_at``         — from submission / resubmission,
* ``hazards.due_at``                  — rectification deadline (a.k.a.
  ``rectification_due_at``),
* ``hazards.verification_due_at``     — from the rectification submission.

Risk severity and time urgency are deliberately independent: a high-risk item
is not necessarily overdue, and a low-risk item can be urgent.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from hazards import RISK_LEVELS


DEADLINE_NORMAL = "normal"
DEADLINE_DUE_SOON = "due_soon"
DEADLINE_DUE_TODAY = "due_today"
DEADLINE_OVERDUE = "overdue"

DEADLINE_STATUSES: tuple[str, ...] = (
    DEADLINE_NORMAL,
    DEADLINE_DUE_SOON,
    DEADLINE_DUE_TODAY,
    DEADLINE_OVERDUE,
)

SLA_POLICY_DEMO: dict[str, dict[str, int]] = {
    "approval": {"重大": 1, "高": 2, "中": 3, "低": 5},
    "rectification": {"重大": 1, "高": 3, "中": 7, "低": 14},
    "verification": {"重大": 1, "高": 2, "中": 3, "低": 5},
}

SLA_RULE_LABEL = "Demo policy（模拟规则，非法规要求）"

_POLICY_FALLBACK = "中"


def _policy_days(kind: str, risk_level: str) -> int:
    policy = SLA_POLICY_DEMO.get(str(kind), {})
    level = str(risk_level or "").strip()
    if level not in RISK_LEVELS:
        level = _POLICY_FALLBACK
    return int(policy.get(level, policy.get(_POLICY_FALLBACK, 3)))


def approval_days(risk_level: str) -> int:
    """Return the Demo approval window in natural days."""
    return _policy_days("approval", risk_level)


def rectification_days(risk_level: str) -> int:
    """Return the Demo rectification window in natural days."""
    return _policy_days("rectification", risk_level)


def verification_days(risk_level: str) -> int:
    """Return the Demo verification window in natural days."""
    return _policy_days("verification", risk_level)


def calculate_due_at(start_at: datetime | date | None, days: int) -> datetime:
    """Return ``start_at + days`` as a datetime (dates start at midnight)."""
    if isinstance(start_at, datetime):
        base = start_at
    elif isinstance(start_at, date):
        base = datetime.combine(start_at, time.min)
    else:
        base = datetime.now()
    return base + timedelta(days=int(days))


def parse_deadline(value: object) -> datetime | None:
    """Parse a stored ISO deadline; return ``None`` when absent or invalid."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.combine(date.fromisoformat(text), time.min)
    except ValueError:
        return None


def classify_deadline(
    due_at: datetime | date | str | None, now: datetime | None = None
) -> str:
    """Classify a deadline as normal / due_soon / due_today / overdue."""
    moment = now or datetime.now()
    if isinstance(due_at, datetime):
        due = due_at
    elif isinstance(due_at, date):
        due = datetime.combine(due_at, time.min)
    else:
        due = parse_deadline(due_at)
    if due is None:
        return DEADLINE_NORMAL
    due_day = due.date()
    today = moment.date()
    if due_day < today:
        return DEADLINE_OVERDUE
    if due_day == today:
        return DEADLINE_DUE_TODAY
    if due_day == today + timedelta(days=1):
        return DEADLINE_DUE_SOON
    return DEADLINE_NORMAL


def is_overdue(
    due_at: datetime | date | str | None, now: datetime | None = None
) -> bool:
    """Return whether the deadline day is before today."""
    return classify_deadline(due_at, now) == DEADLINE_OVERDUE


DEADLINE_WEIGHTS: dict[str, int] = {
    DEADLINE_OVERDUE: 100,
    DEADLINE_DUE_TODAY: 60,
    DEADLINE_DUE_SOON: 30,
    DEADLINE_NORMAL: 0,
}

RISK_WEIGHTS: dict[str, int] = {
    "低": 0,
    "中": 10,
    "高": 20,
    "重大": 30,
}


def deadline_weight(status: str) -> int:
    """Return the urgency weight of one deadline status."""
    return DEADLINE_WEIGHTS.get(str(status), 0)


def risk_weight(risk_level: str) -> int:
    """Return the severity weight of one risk level."""
    return RISK_WEIGHTS.get(str(risk_level or "").strip(), 0)


def priority_score(risk_level: str, deadline_status: str) -> int:
    """Combine time urgency and severity for queue ordering.

    The deadline weight dominates so an overdue low-risk item outranks a
    not-yet-due high-risk item; severity only breaks ties within one window.
    """
    return deadline_weight(deadline_status) + risk_weight(risk_level)


__all__ = [
    "DEADLINE_DUE_SOON",
    "DEADLINE_DUE_TODAY",
    "DEADLINE_NORMAL",
    "DEADLINE_OVERDUE",
    "DEADLINE_STATUSES",
    "DEADLINE_WEIGHTS",
    "RISK_WEIGHTS",
    "SLA_POLICY_DEMO",
    "SLA_RULE_LABEL",
    "approval_days",
    "calculate_due_at",
    "classify_deadline",
    "deadline_weight",
    "is_overdue",
    "parse_deadline",
    "priority_score",
    "rectification_days",
    "risk_weight",
    "verification_days",
]

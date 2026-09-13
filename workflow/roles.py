"""Demo role identifiers shared by the V5 state machines and services.

P0A only defines the identifiers and the role labels; the full permission
matrix and persona storage arrive in P0B.  Roles are plain strings so they can
travel through service calls, audit events and (later) SQLite rows.
"""

from __future__ import annotations


ROLE_APPLICANT = "applicant"
ROLE_EHS_REVIEWER = "ehs_reviewer"
ROLE_APPROVER = "approver"
ROLE_ACTION_OWNER = "action_owner"
ROLE_DEMO_ADMIN = "demo_admin"
ROLE_SYSTEM = "system"

ROLES: tuple[str, ...] = (
    ROLE_APPLICANT,
    ROLE_EHS_REVIEWER,
    ROLE_APPROVER,
    ROLE_ACTION_OWNER,
    ROLE_DEMO_ADMIN,
    ROLE_SYSTEM,
)

ROLE_LABELS: dict[str, str] = {
    ROLE_APPLICANT: "作业申请人（Demo）",
    ROLE_EHS_REVIEWER: "EHS审核人（Demo）",
    ROLE_APPROVER: "作业批准人（Demo）",
    ROLE_ACTION_OWNER: "整改负责人（Demo）",
    ROLE_DEMO_ADMIN: "演示管理员（Demo）",
    ROLE_SYSTEM: "系统任务",
}


def role_label(role: str) -> str:
    """Return the human-readable label for one role code."""
    return ROLE_LABELS.get(str(role), str(role))


__all__ = [
    "ROLE_ACTION_OWNER",
    "ROLE_APPLICANT",
    "ROLE_APPROVER",
    "ROLE_DEMO_ADMIN",
    "ROLE_EHS_REVIEWER",
    "ROLE_LABELS",
    "ROLE_SYSTEM",
    "ROLES",
    "role_label",
]

"""Demo persona store.

This is a portfolio-demo identity simulation, not an authentication system:
there are no passwords, tokens, SSO, OAuth, multi-tenant rules or corporate
directories.  It only maps fixed demo user ids to a display name and one role
so the permission layer and action queue can work with real user ids.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Iterable, Mapping

from workflow import audit, permissions, roles


DEMO_USERS: tuple[dict[str, Any], ...] = (
    {
        "id": "DEMO-APPLICANT-01",
        "display_name": "作业申请人甲（Demo）",
        "role": roles.ROLE_APPLICANT,
    },
    {
        "id": "DEMO-EHS-01",
        "display_name": "EHS审核人甲（Demo）",
        "role": roles.ROLE_EHS_REVIEWER,
    },
    {
        "id": "DEMO-APPROVER-01",
        "display_name": "批准人甲（Demo）",
        "role": roles.ROLE_APPROVER,
    },
    {
        "id": "DEMO-APPROVER-02",
        "display_name": "批准人乙（Demo）",
        "role": roles.ROLE_APPROVER,
    },
    {
        "id": "DEMO-OWNER-01",
        "display_name": "整改负责人甲（Demo）",
        "role": roles.ROLE_ACTION_OWNER,
    },
    {
        "id": "DEMO-OWNER-02",
        "display_name": "整改负责人乙（Demo）",
        "role": roles.ROLE_ACTION_OWNER,
    },
    {
        "id": "DEMO-ADMIN-01",
        "display_name": "演示管理员（Demo）",
        "role": roles.ROLE_DEMO_ADMIN,
    },
)

DEMO_IDENTITY_NOTICE = "当前为 Demo 身份模拟，不代表真实企业账号体系。"


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    record["active"] = bool(record.get("active", 1))
    record["is_demo"] = bool(record.get("is_demo", 1))
    return record


def seed_demo_users(
    connection: sqlite3.Connection, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Insert the fixed demo users once; returns every demo user afterwards."""
    created: list[str] = []
    correlation = audit.new_correlation_id()
    stamp = _stamp(now)
    with connection:
        for user in DEMO_USERS:
            existing = connection.execute(
                "SELECT 1 FROM users WHERE id = ?", (user["id"],)
            ).fetchone()
            if existing is not None:
                continue
            connection.execute(
                "INSERT INTO users (id, display_name, role, active, is_demo, created_at) "
                "VALUES (?, ?, ?, 1, 1, ?)",
                (
                    str(user["id"]),
                    str(user["display_name"]),
                    str(user["role"]),
                    stamp,
                ),
            )
            created.append(str(user["id"]))
            audit.record_event(
                connection,
                audit.build_event(
                    entity_type="demo_user",
                    entity_id=str(user["id"]),
                    action="demo_user.seeded",
                    actor="system",
                    from_state="",
                    to_state="active",
                    reason="初始化 Demo 身份（模拟）",
                    before=None,
                    after={
                        "id": user["id"],
                        "display_name": user["display_name"],
                        "role": user["role"],
                    },
                    correlation_id=correlation,
                    now=now,
                ),
            )
    return list_users(connection)


def get_user(
    connection: sqlite3.Connection, user_id: str
) -> dict[str, Any] | None:
    """Return one demo user record, or ``None``."""
    row = connection.execute(
        "SELECT * FROM users WHERE id = ?", (str(user_id or "").strip(),)
    ).fetchone()
    return _row_dict(row)


def require_user(connection: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """Return one active demo user or raise."""
    record = get_user(connection, user_id)
    if record is None:
        raise ValueError(f"未找到 Demo 用户：{user_id!r}。")
    if not record.get("active"):
        raise ValueError(f"Demo 用户已停用：{user_id!r}。")
    return record


def list_users(
    connection: sqlite3.Connection, *, role: str | None = None
) -> list[dict[str, Any]]:
    """Return every demo user, optionally filtered by role."""
    if role:
        cursor = connection.execute(
            "SELECT * FROM users WHERE role = ? ORDER BY id", (str(role),)
        )
    else:
        cursor = connection.execute("SELECT * FROM users ORDER BY id")
    return [
        record
        for record in (_row_dict(row) for row in cursor.fetchall())
        if record is not None
    ]


def require_role_user(
    connection: sqlite3.Connection,
    user_id: str,
    permission: str,
) -> dict[str, Any]:
    """Return an active user whose role also grants the permission."""
    record = require_user(connection, user_id)
    if not permissions.has_permission(str(record["role"]), permission):
        raise ValueError(
            f"Demo 用户 {user_id!r}（{record['role']}）不具备 {permission} 权限。"
        )
    return record


def public_view(user: Mapping[str, Any]) -> dict[str, Any]:
    """Return the fields safe to render in a UI."""
    return {
        "id": str(user.get("id", "")),
        "display_name": str(user.get("display_name", "")),
        "role": str(user.get("role", "")),
        "role_label": roles.role_label(str(user.get("role", ""))),
        "active": bool(user.get("active", True)),
        "is_demo": bool(user.get("is_demo", True)),
    }


def users_to_csv(users: Iterable[Mapping[str, Any]]) -> str:
    """Return a small CSV for debugging; demo data only."""
    lines = ["user_id,display_name,role,active"]
    for user in users:
        lines.append(
            ",".join(
                [
                    str(user.get("id", "")),
                    str(user.get("display_name", "")),
                    str(user.get("role", "")),
                    "1" if user.get("active", True) else "0",
                ]
            )
        )
    return "\n".join(lines)


__all__ = [
    "DEMO_IDENTITY_NOTICE",
    "DEMO_USERS",
    "get_user",
    "list_users",
    "public_view",
    "require_role_user",
    "require_user",
    "seed_demo_users",
    "users_to_csv",
]

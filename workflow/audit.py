"""Append-only audit events for the V5 service layer.

Design rules
------------
* ``record_event`` only ever INSERTs.  There is deliberately no update or delete
  helper, and none should be added for ordinary business code.
* The service layer calls ``record_event`` *inside* the same
  ``with connection:`` transaction as the business write, so either both commit
  or both roll back.
* ``before`` / ``after`` are JSON snapshots; ``reason`` answers "why", and
  ``from_state`` / ``to_state`` answer "from what to what".

The prototype calls this an append-only operation record, not a
regulatory-grade immutable audit log.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4


ENTITY_PERMIT = "permit"
ENTITY_HAZARD = "hazard"

AUDIT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "correlation_id",
    "entity_type",
    "entity_id",
    "action",
    "actor",
    "from_state",
    "to_state",
    "reason",
    "before_json",
    "after_json",
    "created_at",
)


def new_event_id() -> str:
    """Return a fresh unique event identifier."""
    return uuid4().hex


def new_correlation_id() -> str:
    """Return a fresh correlation identifier for one service command."""
    return uuid4().hex


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _jsonable(value: Any) -> str:
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


@dataclass(frozen=True)
class AuditEvent:
    """One append-only audit record."""

    event_id: str
    correlation_id: str
    entity_type: str
    entity_id: str
    action: str
    actor: str
    from_state: str = ""
    to_state: str = ""
    reason: str = ""
    before: Any = None
    after: Any = None
    created_at: str = ""

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.event_id,
            self.correlation_id,
            self.entity_type,
            self.entity_id,
            self.action,
            self.actor,
            self.from_state,
            self.to_state,
            self.reason,
            _jsonable(self.before),
            _jsonable(self.after),
            self.created_at,
        )

    def to_dict(self, *, expand_json: bool = False) -> dict[str, Any]:
        record = {
            "event_id": self.event_id,
            "correlation_id": self.correlation_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "action": self.action,
            "actor": self.actor,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "before_json": _jsonable(self.before),
            "after_json": _jsonable(self.after),
            "created_at": self.created_at,
        }
        if expand_json:
            record["before"] = self.before
            record["after"] = self.after
        return record


def build_event(
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    actor: str,
    from_state: str = "",
    to_state: str = "",
    reason: str = "",
    before: Any = None,
    after: Any = None,
    correlation_id: str = "",
    event_id: str = "",
    now: datetime | None = None,
) -> AuditEvent:
    """Build one audit event without touching the database."""
    return AuditEvent(
        event_id=str(event_id or "").strip() or new_event_id(),
        correlation_id=str(correlation_id or "").strip() or new_correlation_id(),
        entity_type=str(entity_type or "").strip(),
        entity_id=str(entity_id or "").strip(),
        action=str(action or "").strip(),
        actor=str(actor or "").strip(),
        from_state=str(from_state or "").strip(),
        to_state=str(to_state or "").strip(),
        reason=str(reason or "").strip(),
        before=before,
        after=after,
        created_at=_stamp(now),
    )


def record_event(connection: sqlite3.Connection, event: AuditEvent) -> str:
    """Insert one audit event; the caller owns the surrounding transaction."""
    connection.execute(
        f"INSERT INTO audit_events ({', '.join(AUDIT_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in AUDIT_COLUMNS)})",
        event.to_row(),
    )
    return event.event_id


def get_event(connection: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    """Return one audit event by id, or ``None``."""
    cursor = connection.execute(
        "SELECT * FROM audit_events WHERE event_id = ?", (str(event_id),)
    )
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def list_events(
    connection: sqlite3.Connection,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    correlation_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return audit events in chronological order with optional filters."""
    clauses: list[str] = []
    values: list[Any] = []
    if entity_type:
        clauses.append("entity_type = ?")
        values.append(str(entity_type))
    if entity_id:
        clauses.append("entity_id = ?")
        values.append(str(entity_id))
    if action:
        clauses.append("action = ?")
        values.append(str(action))
    if correlation_id:
        clauses.append("correlation_id = ?")
        values.append(str(correlation_id))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        limit_value = max(int(limit), 0)
    except (TypeError, ValueError):
        limit_value = 200
    cursor = connection.execute(
        f"SELECT * FROM audit_events {where} "
        "ORDER BY created_at ASC, rowid ASC LIMIT ?",
        (*values, limit_value),
    )
    return [dict(row) for row in cursor.fetchall()]


def count_events(
    connection: sqlite3.Connection,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> int:
    """Return the number of audit events matching the optional filters."""
    clauses: list[str] = []
    values: list[Any] = []
    if entity_type:
        clauses.append("entity_type = ?")
        values.append(str(entity_type))
    if entity_id:
        clauses.append("entity_id = ?")
        values.append(str(entity_id))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cursor = connection.execute(
        f"SELECT COUNT(*) AS total FROM audit_events {where}", tuple(values)
    )
    return int(cursor.fetchone()["total"])


__all__ = [
    "AUDIT_COLUMNS",
    "AuditEvent",
    "ENTITY_HAZARD",
    "ENTITY_PERMIT",
    "build_event",
    "count_events",
    "get_event",
    "list_events",
    "new_correlation_id",
    "new_event_id",
    "record_event",
]

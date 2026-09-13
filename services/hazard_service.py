"""Hazard service: the only writer of hazard state and its audit events.

Commands mirror the pure ``workflow.hazard_state`` machine: assign, start,
submit rectification, verify (pass/fail) and reopen.  Rectification evidence and
the corrective actions are written in the same transaction as the state change
and its audit event.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Iterable, Mapping
from uuid import uuid4

from hazards import RISK_LEVELS
from workflow import audit, hazard_state, roles

from . import legacy_adapter


DEFAULT_HAZARD_TYPE = "其他"
DEFAULT_RISK_LEVEL = "中"

_SNAPSHOT_FIELDS = (
    "id",
    "permit_id",
    "title",
    "status",
    "risk_level",
    "owner_id",
    "verifier_id",
    "verification_result",
    "due_at",
    "data_label",
    "is_demo",
)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _hazard_row(connection: sqlite3.Connection, hazard_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM hazards WHERE id = ?", (str(hazard_id),)
    ).fetchone()
    return _row_dict(row)


def next_hazard_id(connection: sqlite3.Connection) -> str:
    """Return the next ``HZ-###`` identifier."""
    numbers: list[int] = []
    for row in connection.execute("SELECT id FROM hazards").fetchall():
        value = str(row["id"])
        if value.startswith("HZ-") and value[3:].isdigit():
            numbers.append(int(value[3:]))
    return f"HZ-{max(numbers, default=0) + 1:03d}"


def _normalise_action(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    text = str(item.get("action_text", "") or item.get("measure", "")).strip()
    if not text:
        raise ValueError(f"corrective_actions 第 {index} 项缺少整改措施内容。")
    status = str(item.get("status", "planned")).strip() or "planned"
    if status not in {"planned", "in_progress", "completed"}:
        raise ValueError(f"corrective_actions 第 {index} 项状态无效：{status!r}。")
    return {
        "id": str(item.get("id", "") or "").strip() or uuid4().hex,
        "action_text": text,
        "owner_id": str(item.get("owner_id", "")).strip(),
        "due_at": str(item.get("due_at", "") or "").strip(),
        "status": status,
        "completed_at": str(item.get("completed_at", "") or "").strip(),
    }


def _normalise_evidence(
    item: Mapping[str, Any],
    index: int,
    *,
    fallback_actor: str,
    stamp: str,
) -> dict[str, Any]:
    file_name = str(item.get("file_name", "")).strip()
    if not file_name:
        raise ValueError(f"evidence 第 {index} 项缺少文件名（file_name）。")
    return {
        "file_name": file_name,
        "evidence_type": str(item.get("evidence_type", "其他") or "其他").strip(),
        "note": str(item.get("note", "")).strip(),
        "uploaded_by": str(item.get("uploaded_by", "")).strip() or fallback_actor,
        "uploaded_at": str(item.get("uploaded_at", "")).strip() or stamp,
    }


def _insert_actions(
    connection: sqlite3.Connection,
    hazard_id: str,
    rows: Iterable[Mapping[str, Any]],
) -> None:
    for row in rows:
        connection.execute(
            "INSERT INTO corrective_actions "
            "(id, hazard_id, action_text, owner_id, due_at, status, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                hazard_id,
                row["action_text"],
                row["owner_id"],
                row["due_at"],
                row["status"],
                row["completed_at"],
            ),
        )


def _insert_evidence(
    connection: sqlite3.Connection,
    hazard_id: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    is_demo: bool,
) -> None:
    for row in rows:
        connection.execute(
            "INSERT INTO hazard_evidence "
            "(hazard_id, file_name, evidence_type, note, uploaded_by, uploaded_at, is_demo) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                hazard_id,
                row["file_name"],
                row["evidence_type"],
                row["note"],
                row["uploaded_by"],
                row["uploaded_at"],
                1 if is_demo else 0,
            ),
        )


def _snapshot(hazard: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: hazard.get(field, "")
        for field in _SNAPSHOT_FIELDS
        if field in hazard
    }


def _hazard_context(
    connection: sqlite3.Connection,
    hazard: Mapping[str, Any],
) -> dict[str, Any]:
    actions = list(hazard.get("corrective_actions") or ())
    evidence = list(hazard.get("evidence") or ())
    return {
        "owner_id": str(hazard.get("owner_id", "") or ""),
        "permit_id": str(hazard.get("permit_id", "") or ""),
        "corrective_action_count": len(actions),
        "evidence_count": len(evidence),
        "verification_notes": str(hazard.get("verification_notes", "") or ""),
        "verification_result": str(hazard.get("verification_result", "") or ""),
    }


def get_hazard(
    connection: sqlite3.Connection, hazard_id: str
) -> dict[str, Any] | None:
    """Return the full hazard aggregate, or ``None``."""
    hazard = _hazard_row(connection, hazard_id)
    if hazard is None:
        return None
    hazard["is_demo"] = bool(hazard.get("is_demo"))
    hazard["corrective_actions"] = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM corrective_actions WHERE hazard_id = ? ORDER BY rowid",
            (str(hazard_id),),
        ).fetchall()
    ]
    hazard["evidence"] = [
        {**dict(row), "is_demo": bool(row["is_demo"])}
        for row in connection.execute(
            "SELECT * FROM hazard_evidence WHERE hazard_id = ? ORDER BY id",
            (str(hazard_id),),
        ).fetchall()
    ]
    return hazard


def list_hazards(
    connection: sqlite3.Connection,
    *,
    permit_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return hazard rows (children omitted) oldest first."""
    clauses: list[str] = []
    values: list[Any] = []
    if permit_id:
        clauses.append("permit_id = ?")
        values.append(str(permit_id))
    if status:
        clauses.append("status = ?")
        values.append(str(status))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        limit_value = max(int(limit), 0)
    except (TypeError, ValueError):
        limit_value = 200
    cursor = connection.execute(
        f"SELECT * FROM hazards {where} ORDER BY created_at, id LIMIT ?",
        (*values, limit_value),
    )
    return [{**dict(row), "is_demo": bool(row["is_demo"])} for row in cursor.fetchall()]


def create_hazard(
    connection: sqlite3.Connection,
    *,
    title: str,
    permit_id: str = "",
    hazard_id: str = "",
    description: str = "",
    hazard_type: str = DEFAULT_HAZARD_TYPE,
    risk_level: str = DEFAULT_RISK_LEVEL,
    reported_by_id: str = "",
    owner_id: str = "",
    status: str = hazard_state.HAZARD_OPEN,
    due_at: object = "",
    verification_due_at: object = "",
    verification: Mapping[str, Any] | None = None,
    closed_by: str = "",
    closed_at: object = "",
    corrective_actions: Iterable[Mapping[str, Any]] = (),
    evidence: Iterable[Mapping[str, Any]] = (),
    data_label: str = "",
    is_demo: bool | None = None,
    actor: str = "",
    audit_action: str = "hazard.created",
    created_at: object = "",
    updated_at: object = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one hazard aggregate and its audit event in one transaction."""
    name = str(title or "").strip()
    if not name:
        raise ValueError("隐患标题（title）不能为空。")
    state = str(status or "").strip()
    if state not in hazard_state.HAZARD_STATUSES:
        raise ValueError(f"未知隐患状态：{status!r}。")
    level = str(risk_level or "").strip() or DEFAULT_RISK_LEVEL
    if level not in RISK_LEVELS:
        raise ValueError(f"风险等级必须是 {RISK_LEVELS} 之一，当前为 {risk_level!r}。")

    identifier = str(hazard_id or "").strip() or next_hazard_id(connection)
    if _hazard_row(connection, identifier) is not None:
        raise ValueError(f"隐患编号已存在：{identifier}。")

    stamp = _stamp(now)
    action_rows = [
        _normalise_action(item, index)
        for index, item in enumerate(corrective_actions, start=1)
    ]
    evidence_rows = [
        _normalise_evidence(item, index, fallback_actor=str(actor), stamp=stamp)
        for index, item in enumerate(evidence, start=1)
    ]

    label = str(data_label or "").strip()
    demo = legacy_adapter.is_demo_label(label) if is_demo is None else bool(is_demo)
    verification_data = dict(verification or {})
    created_stamp = str(created_at or "").strip() or stamp
    updated_stamp = str(updated_at or "").strip() or stamp
    correlation = audit.new_correlation_id()

    with connection:
        connection.execute(
            "INSERT INTO hazards ("
            "id, permit_id, title, description, hazard_type, risk_level, "
            "reported_by_id, owner_id, status, due_at, verification_due_at, "
            "verification_result, verification_notes, verifier_id, verified_at, "
            "reopened_reason, closed_by, closed_at, data_label, is_demo, "
            "created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                str(permit_id or "").strip(),
                name,
                str(description or "").strip() or name,
                str(hazard_type or DEFAULT_HAZARD_TYPE).strip() or DEFAULT_HAZARD_TYPE,
                level,
                str(reported_by_id or "").strip(),
                str(owner_id or "").strip(),
                state,
                str(due_at or "").strip(),
                str(verification_due_at or "").strip(),
                str(verification_data.get("result", "")).strip(),
                str(verification_data.get("notes", "")).strip(),
                str(verification_data.get("verifier_id", "")).strip(),
                str(verification_data.get("verified_at", "")).strip(),
                "",
                str(closed_by or "").strip(),
                str(closed_at or "").strip(),
                label,
                1 if demo else 0,
                created_stamp,
                updated_stamp,
            ),
        )
        _insert_actions(connection, identifier, action_rows)
        _insert_evidence(connection, identifier, evidence_rows, is_demo=demo)
        after = _hazard_row(connection, identifier)
        audit.record_event(
            connection,
            audit.build_event(
                entity_type=audit.ENTITY_HAZARD,
                entity_id=identifier,
                action=str(audit_action or "hazard.created"),
                actor=str(actor or "").strip(),
                from_state="",
                to_state=state,
                reason="",
                before=None,
                after=after,
                correlation_id=correlation,
                now=now,
            ),
        )
    return get_hazard(connection, identifier)


def open_hazard_count(connection: sqlite3.Connection, permit_id: str) -> int:
    """Return the number of non-closed hazards linked to a permit."""
    row = connection.execute(
        "SELECT COUNT(*) AS total FROM hazards WHERE permit_id = ? AND status != ?",
        (str(permit_id), hazard_state.HAZARD_CLOSED),
    ).fetchone()
    return int(row["total"]) if row else 0


def _write_transition(
    connection: sqlite3.Connection,
    hazard: Mapping[str, Any],
    action: str,
    to_status: str,
    *,
    actor: str,
    reason: str,
    extra: Mapping[str, Any] | None = None,
    evidence_rows: Iterable[Mapping[str, Any]] = (),
    is_demo: bool = False,
    notes: str = "",
    now: datetime | None = None,
) -> None:
    stamp = _stamp(now)
    correlation = audit.new_correlation_id()
    before = _snapshot(hazard)
    changes = dict(extra or {})
    with connection:
        _insert_evidence(connection, str(hazard["id"]), evidence_rows, is_demo=is_demo)
        fields: dict[str, Any] = {"status": to_status, "updated_at": stamp}
        fields.update(changes)
        assignments = ", ".join(f"{name} = ?" for name in fields)
        connection.execute(
            f"UPDATE hazards SET {assignments} WHERE id = ?",
            (*fields.values(), str(hazard["id"])),
        )
        after = {**before, **changes, "status": to_status, "updated_at": stamp}
        audit.record_event(
            connection,
            audit.build_event(
                entity_type=audit.ENTITY_HAZARD,
                entity_id=str(hazard["id"]),
                action=f"hazard.{action}",
                actor=actor,
                from_state=str(hazard.get("status", "")),
                to_state=to_status,
                reason=reason or notes,
                before=before,
                after=after,
                correlation_id=correlation,
                now=now,
            ),
        )


def transition_hazard(
    connection: sqlite3.Connection,
    hazard_id: str,
    action: str,
    *,
    actor: str,
    actor_role: str = "",
    reason: str = "",
    context: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and apply one hazard transition with its audit event."""
    hazard = get_hazard(connection, hazard_id)
    if hazard is None:
        raise KeyError(f"未找到隐患：{hazard_id}")
    values = _hazard_context(connection, hazard)
    values.update(dict(context or {}))
    result = hazard_state.validate_hazard_transition(
        str(hazard.get("status", "")),
        action,
        actor_role=actor_role,
        actor_id=actor,
        owner_id=str(hazard.get("owner_id", "")),
        reason=reason,
        context=values,
    )
    if not result.allowed:
        raise ValueError(result.message)
    _write_transition(
        connection,
        hazard,
        action,
        result.to_status,
        actor=str(actor or "").strip(),
        reason=str(reason or "").strip(),
        now=now,
    )
    return get_hazard(connection, str(hazard_id))


def assign_hazard(
    connection: sqlite3.Connection,
    hazard_id: str,
    *,
    owner_id: str,
    actor: str,
    due_at: object = "",
    reason: str = "",
    actor_role: str = roles.ROLE_EHS_REVIEWER,
    now: datetime | None = None,
) -> dict[str, Any]:
    """open/reopened/assigned → assigned, recording the new owner."""
    extra: dict[str, Any] = {"owner_id": str(owner_id or "").strip()}
    if str(due_at or "").strip():
        extra["due_at"] = str(due_at).strip()
    return _apply_with_extra(
        connection,
        hazard_id,
        "assign",
        actor=actor,
        actor_role=actor_role,
        reason=reason,
        context={"target_owner_id": owner_id},
        extra=extra,
        now=now,
    )


def start_rectification(
    connection: sqlite3.Connection,
    hazard_id: str,
    *,
    actor: str,
    actor_role: str = roles.ROLE_ACTION_OWNER,
    now: datetime | None = None,
) -> dict[str, Any]:
    """assigned → in_progress, executed by the assigned owner."""
    return transition_hazard(
        connection,
        hazard_id,
        "start",
        actor=actor,
        actor_role=actor_role,
        now=now,
    )


def submit_rectification(
    connection: sqlite3.Connection,
    hazard_id: str,
    *,
    actor: str,
    evidence: Iterable[Mapping[str, Any]],
    notes: str = "",
    actor_role: str = roles.ROLE_ACTION_OWNER,
    now: datetime | None = None,
) -> dict[str, Any]:
    """in_progress → verification_pending, attaching rectification evidence."""
    hazard = get_hazard(connection, hazard_id)
    if hazard is None:
        raise KeyError(f"未找到隐患：{hazard_id}")
    stamp = _stamp(now)
    evidence_rows = [
        _normalise_evidence(item, index, fallback_actor=str(actor), stamp=stamp)
        for index, item in enumerate(evidence, start=1)
    ]
    if not evidence_rows:
        raise ValueError("提交整改必须至少包含 1 条整改证据。")
    result = hazard_state.validate_hazard_transition(
        str(hazard.get("status", "")),
        "submit_rectification",
        actor_role=actor_role,
        actor_id=actor,
        owner_id=str(hazard.get("owner_id", "")),
        reason=notes,
        context={
            "corrective_action_count": len(hazard.get("corrective_actions") or ()),
            "evidence_count": len(evidence_rows),
        },
    )
    if not result.allowed:
        raise ValueError(result.message)
    _write_transition(
        connection,
        hazard,
        "submit_rectification",
        result.to_status,
        actor=str(actor or "").strip(),
        reason=str(notes or "").strip(),
        notes=str(notes or "").strip(),
        evidence_rows=evidence_rows,
        is_demo=bool(hazard.get("is_demo")),
        now=now,
    )
    return get_hazard(connection, str(hazard_id))


def verify_hazard(
    connection: sqlite3.Connection,
    hazard_id: str,
    *,
    actor: str,
    result: str,
    notes: str,
    actor_role: str = roles.ROLE_EHS_REVIEWER,
    now: datetime | None = None,
) -> dict[str, Any]:
    """verification_pending → closed (pass) or reopened (fail)."""
    hazard = get_hazard(connection, hazard_id)
    if hazard is None:
        raise KeyError(f"未找到隐患：{hazard_id}")
    decision = str(result or "").strip()
    if decision not in {"pass", "fail"}:
        raise ValueError("验证结果必须是 pass 或 fail。")
    text = str(notes or "").strip()
    if decision == "pass" and not text:
        raise ValueError("验证通过必须填写验证说明。")
    if decision == "fail" and not text:
        raise ValueError("验证不通过必须填写原因或验证说明。")
    action = "verify_pass" if decision == "pass" else "verify_fail"
    stamp = _stamp(now)
    extra: dict[str, Any] = {
        "verification_result": decision,
        "verification_notes": text,
        "verifier_id": str(actor or "").strip(),
        "verified_at": stamp,
        "reopened_reason": "",
    }
    if decision == "pass":
        extra["closed_by"] = str(actor or "").strip()
        extra["closed_at"] = stamp
    else:
        extra["reopened_reason"] = text
    machine = hazard_state.validate_hazard_transition(
        str(hazard.get("status", "")),
        action,
        actor_role=actor_role,
        actor_id=actor,
        owner_id=str(hazard.get("owner_id", "")),
        reason=text,
        context={
            "verification_notes": text,
            "verification_result": decision,
        },
    )
    if not machine.allowed:
        raise ValueError(machine.message)
    _write_transition(
        connection,
        hazard,
        action,
        machine.to_status,
        actor=str(actor or "").strip(),
        reason=text,
        notes=text,
        extra=extra,
        now=now,
    )
    return get_hazard(connection, str(hazard_id))


def reopen_hazard(
    connection: sqlite3.Connection,
    hazard_id: str,
    *,
    actor: str,
    reason: str,
    actor_role: str = roles.ROLE_EHS_REVIEWER,
    now: datetime | None = None,
) -> dict[str, Any]:
    """closed → reopened; a reason is mandatory and previous verification is cleared."""
    text = str(reason or "").strip()
    if not text:
        raise ValueError("重新打开隐患必须填写原因（reason）。")
    return _apply_with_extra(
        connection,
        hazard_id,
        "reopen",
        actor=actor,
        actor_role=actor_role,
        reason=text,
        extra={
            "verification_result": "",
            "verification_notes": "",
            "verifier_id": "",
            "verified_at": "",
            "closed_by": "",
            "closed_at": "",
            "reopened_reason": text,
        },
        now=now,
    )


def _apply_with_extra(
    connection: sqlite3.Connection,
    hazard_id: str,
    action: str,
    *,
    actor: str,
    actor_role: str,
    reason: str,
    context: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    hazard = get_hazard(connection, hazard_id)
    if hazard is None:
        raise KeyError(f"未找到隐患：{hazard_id}")
    values = _hazard_context(connection, hazard)
    values.update(dict(context or {}))
    result = hazard_state.validate_hazard_transition(
        str(hazard.get("status", "")),
        action,
        actor_role=actor_role,
        actor_id=actor,
        owner_id=str(hazard.get("owner_id", "")),
        reason=reason,
        context=values,
    )
    if not result.allowed:
        raise ValueError(result.message)
    _write_transition(
        connection,
        hazard,
        action,
        result.to_status,
        actor=str(actor or "").strip(),
        reason=str(reason or "").strip(),
        extra=extra,
        now=now,
    )
    return get_hazard(connection, str(hazard_id))


def import_legacy_hazard(
    connection: sqlite3.Connection,
    record: Mapping[str, Any],
    *,
    actor: str = "legacy_adapter",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Import one V4 hazard dict through the adapter into the V5 store."""
    mapped = legacy_adapter.legacy_hazard_to_v5(record)
    if not mapped.get("hazard_id"):
        raise ValueError("V4 隐患记录缺少隐患编号。")
    if not mapped.get("status"):
        raise ValueError(f"无法识别的 V4 隐患状态：{record.get('状态')!r}。")
    if _hazard_row(connection, mapped["hazard_id"]) is not None:
        raise ValueError(f"隐患已存在：{mapped['hazard_id']}。")
    return create_hazard(
        connection,
        hazard_id=str(mapped["hazard_id"]),
        permit_id=str(mapped.get("permit_id", "")),
        title=str(mapped.get("title", "")),
        description=str(mapped.get("description", "")),
        hazard_type=str(mapped.get("hazard_type", DEFAULT_HAZARD_TYPE)),
        risk_level=str(mapped.get("risk_level", DEFAULT_RISK_LEVEL)),
        reported_by_id=str(mapped.get("reported_by_id", "")),
        owner_id=str(mapped.get("owner_id", "")),
        status=str(mapped["status"]),
        due_at=mapped.get("due_at", ""),
        verification_due_at=mapped.get("verification_due_at", ""),
        verification=mapped.get("verification"),
        closed_by=str(mapped.get("closed_by", "")),
        closed_at=mapped.get("closed_at", ""),
        corrective_actions=mapped.get("corrective_actions", ()),
        evidence=mapped.get("evidence", ()),
        data_label=str(mapped.get("data_label", "")),
        is_demo=bool(mapped.get("is_demo")),
        actor=actor,
        audit_action="hazard.imported",
        created_at=mapped.get("created_at", ""),
        updated_at=mapped.get("created_at", ""),
        now=now,
    )


__all__ = [
    "DEFAULT_HAZARD_TYPE",
    "DEFAULT_RISK_LEVEL",
    "assign_hazard",
    "create_hazard",
    "get_hazard",
    "import_legacy_hazard",
    "list_hazards",
    "next_hazard_id",
    "open_hazard_count",
    "reopen_hazard",
    "start_rectification",
    "submit_rectification",
    "transition_hazard",
    "verify_hazard",
]

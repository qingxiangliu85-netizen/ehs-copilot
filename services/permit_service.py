"""Permit service: the only writer of permit state and its audit events.

Every command follows the same contract:

1. load the aggregate and validate state / role / preconditions with the pure
   ``workflow.permit_state`` machine;
2. perform the business write and the ``audit_events`` insert inside one
   ``with connection:`` transaction;
3. return the refreshed aggregate.

No Streamlit, LangGraph or AI code may change a permit status directly.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable, Mapping

import jsa
from job_review import attach_public_evidence, attach_sds_evidence
from workflow import audit, permit_state, permissions, roles, sla

from . import legacy_adapter, persona_service


DEFAULT_PERMIT_TYPE = "危化品非例行作业"

_EVIDENCE_TRACKS = ("public_sources", "sds")

_EVIDENCE_COLUMNS = (
    "permit_id, track, evidence_id, source, page, sections, snippet, topic, "
    "organization, source_title, source_url, captured_at, data_label, is_demo"
)

_SNAPSHOT_FIELDS = (
    "id",
    "title",
    "status",
    "risk_level",
    "residual_risk_level",
    "control_measures",
    "applicant_id",
    "owner_id",
    "designated_approver_id",
    "ehs_reviewer_id",
    "approval_due_at",
    "valid_from",
    "valid_to",
    "handback_note",
    "closed_by",
    "closed_at",
    "data_label",
    "is_demo",
)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _resolve_actor(
    user: Mapping[str, Any] | None, actor: str, actor_role: str
) -> tuple[str, str]:
    """Return the effective (actor id, role) for one command call."""
    if user is None:
        return str(actor or "").strip(), str(actor_role or "").strip()
    resolved_actor = permissions.user_id(user) or str(actor or "").strip()
    resolved_role = permissions.user_role(user) or str(actor_role or "").strip()
    return resolved_actor, resolved_role


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def next_permit_id(connection: sqlite3.Connection) -> str:
    """Return the next ``PERMIT-###`` identifier."""
    numbers: list[int] = []
    for row in connection.execute("SELECT id FROM permits").fetchall():
        value = str(row["id"])
        if value.startswith("PERMIT-") and value[7:].isdigit():
            numbers.append(int(value[7:]))
    return f"PERMIT-{max(numbers, default=0) + 1:03d}"


def _normalise_chemical(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    name = str(item.get("chemical_name", "") or item.get("name", "")).strip()
    if not name:
        raise ValueError(f"chemicals 第 {index} 项缺少化学品名称。")
    aliases = item.get("aliases") or []
    if not isinstance(aliases, (list, tuple)):
        raise ValueError(f"chemicals 第 {index} 项的 aliases 必须是列表。")
    return {
        "chemical_name": name,
        "aliases": [str(alias).strip() for alias in aliases if str(alias).strip()],
        "sds_file": str(item.get("sds_file", "") or "").strip(),
        "sds_status": str(item.get("sds_status", "") or "").strip(),
    }


def _normalise_step(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    name = str(item.get("name", "") or item.get("step", "")).strip()
    if not name:
        raise ValueError(f"steps 第 {index} 项缺少步骤名称。")
    try:
        step_no = int(item.get("step_no", item.get("order", index)) or index)
    except (TypeError, ValueError):
        step_no = index
    return {
        "step_no": step_no,
        "name": name,
        "note": str(item.get("note", "") or "").strip(),
    }


def _normalise_evidence(track: str, items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate evidence with the existing V4 two-track rules and return rows."""
    kind = str(track or "").strip()

    def candidate(item: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(item)
        if "passage" not in row and row.get("snippet"):
            row["passage"] = row["snippet"]
        if "retrieved_at" not in row and row.get("captured_at"):
            row["retrieved_at"] = row["captured_at"]
        return row

    rows = [candidate(item) for item in items]
    if kind == legacy_adapter.EVIDENCE_TRACK_SDS:
        holder: dict[str, Any] = {"public_evidence": [], "sds_evidence": []}
        attach_sds_evidence(holder, rows)
        return list(holder["sds_evidence"])
    if kind == legacy_adapter.EVIDENCE_TRACK_PUBLIC:
        holder = {"public_evidence": [], "sds_evidence": []}
        attach_public_evidence(holder, rows)
        return list(holder["public_evidence"])
    raise ValueError(f"未知证据轨道：{track!r}。")


def _canonical_risk_level(value: Any) -> str:
    """Map ``低风险`` onto ``低`` so the state machine sees one vocabulary."""
    text = str(value or "").strip()
    if text.endswith("风险"):
        text = text[: -len("风险")].strip()
    return text


def _dedup_segments(text: str) -> str:
    """Join control measures without repeating identical segments."""
    seen: set[str] = set()
    ordered: list[str] = []
    for part in str(text).split("；"):
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            ordered.append(part)
    return "；".join(ordered)


def _normalise_jsa_item(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    row = dict(item)
    try:
        step_no = int(row.get("step_no", index) or index)
    except (TypeError, ValueError):
        step_no = index
    likelihood = row.get("likelihood")
    severity = row.get("severity")
    if likelihood is not None and severity is not None:
        try:
            score, level = jsa.calculate_risk(int(likelihood), int(severity))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"JSA 第 {index} 项风险输入无效：{exc}") from exc
        row["risk_score"] = score
        row["risk_level"] = _canonical_risk_level(level)
    residual_l = row.get("residual_likelihood")
    residual_s = row.get("residual_severity")
    if residual_l is not None and residual_s is not None:
        try:
            score, level = jsa.calculate_risk(int(residual_l), int(residual_s))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"JSA 第 {index} 项残余风险输入无效：{exc}") from exc
        row["residual_risk_score"] = score
        row["residual_risk_level"] = _canonical_risk_level(level)
    row["step_no"] = step_no
    return row


def _insert_chemicals(
    connection: sqlite3.Connection,
    permit_id: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    is_demo: bool,
) -> None:
    for row in rows:
        connection.execute(
            "INSERT INTO permit_chemicals "
            "(permit_id, chemical_name, aliases_json, sds_file, sds_status, is_demo) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                permit_id,
                row["chemical_name"],
                json.dumps(row["aliases"], ensure_ascii=False),
                row["sds_file"],
                row["sds_status"],
                1 if is_demo else 0,
            ),
        )


def _insert_steps(
    connection: sqlite3.Connection,
    permit_id: str,
    rows: Iterable[Mapping[str, Any]],
) -> None:
    for row in rows:
        connection.execute(
            "INSERT INTO permit_steps (permit_id, step_no, name, note) VALUES (?, ?, ?, ?)",
            (permit_id, row["step_no"], row["name"], row["note"]),
        )


def _insert_evidence_rows(
    connection: sqlite3.Connection,
    permit_id: str,
    track: str,
    items: Iterable[Mapping[str, Any]],
    *,
    is_demo: bool,
    data_label: str,
) -> int:
    count = 0
    for item in items:
        if track == legacy_adapter.EVIDENCE_TRACK_SDS:
            page = item.get("page", 0)
            try:
                page = int(page or 0)
            except (TypeError, ValueError):
                page = 0
            values = (
                permit_id,
                track,
                str(item.get("evidence_id", "") or f"sds:{item.get('source', '')}:{page}"),
                str(item.get("source", "")),
                page,
                str(item.get("sections", "")),
                str(item.get("snippet", "") or item.get("passage", "")),
                str(item.get("topic", "")),
                "",
                "",
                "",
                str(item.get("captured_at", "") or item.get("retrieved_at", "")),
                str(item.get("data_label", "") or data_label),
                1 if is_demo else 0,
            )
        else:
            values = (
                permit_id,
                track,
                str(item.get("evidence_id", "")),
                "",
                0,
                "",
                str(item.get("snippet", "") or item.get("passage", "")),
                str(item.get("topic", "")),
                str(item.get("organization", "")),
                str(item.get("source_title", "")),
                str(item.get("source_url", "")),
                str(item.get("captured_at", "") or item.get("retrieved_at", "")),
                str(item.get("data_label", "") or data_label),
                1 if is_demo else 0,
            )
        placeholders = ", ".join("?" for _ in range(14))
        connection.execute(
            f"INSERT INTO permit_evidence ({_EVIDENCE_COLUMNS}) "
            f"VALUES ({placeholders})",
            values,
        )
        count += 1
    return count


def _insert_jsa_items(
    connection: sqlite3.Connection,
    permit_id: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    is_demo: bool,
) -> None:
    for row in rows:
        connection.execute(
            "INSERT INTO permit_jsa_items ("
            "permit_id, step_no, work_step, hazard, consequence, likelihood, severity, "
            "risk_score, risk_level, existing_controls, proposed_controls, "
            "residual_likelihood, residual_severity, residual_risk_score, "
            "residual_risk_level, confirmed_by, confirmed_at, is_demo"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                permit_id,
                row.get("step_no", 1),
                str(row.get("work_step", "")),
                str(row.get("hazard", "")),
                str(row.get("consequence", "")),
                row.get("likelihood"),
                row.get("severity"),
                row.get("risk_score"),
                str(row.get("risk_level", "")),
                str(row.get("existing_controls", "")),
                str(row.get("proposed_controls", "")),
                row.get("residual_likelihood"),
                row.get("residual_severity"),
                row.get("residual_risk_score"),
                str(row.get("residual_risk_level", "")),
                str(row.get("confirmed_by", "")),
                str(row.get("confirmed_at", "")),
                1 if is_demo else 0,
            ),
        )


def _snapshot(permit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: permit.get(field, "")
        for field in _SNAPSHOT_FIELDS
        if field in permit
    }


def _permit_context(
    connection: sqlite3.Connection,
    permit: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = permit.get("evidence") or {}
    prestart = list(permit.get("prestart_checks") or ())
    required = [row for row in prestart if int(row.get("required", 1) or 0)]
    return {
        "chemical_count": len(permit.get("chemicals") or ()),
        "sds_evidence_count": len(evidence.get(legacy_adapter.EVIDENCE_TRACK_SDS, [])),
        "public_evidence_count": len(
            evidence.get(legacy_adapter.EVIDENCE_TRACK_PUBLIC, [])
        ),
        "jsa_item_count": len(permit.get("jsa_items") or ()),
        "residual_risk_level": str(permit.get("residual_risk_level", "") or ""),
        "control_measures": str(permit.get("control_measures", "") or ""),
        "prestart_passed": bool(required)
        and all(str(row.get("result", "")) == "pass" for row in required),
        "open_hazard_count": int(permit.get("open_hazard_count", 0) or 0),
        "valid_to": str(permit.get("valid_to", "") or ""),
        "handback_note": str(permit.get("handback_note", "") or ""),
    }


def _permit_row(connection: sqlite3.Connection, permit_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM permits WHERE id = ?", (str(permit_id),)
    ).fetchone()
    return _row_dict(row)


def create_permit(
    connection: sqlite3.Connection,
    *,
    title: str,
    permit_id: str = "",
    permit_type: str = DEFAULT_PERMIT_TYPE,
    applicant_id: str = "",
    owner_id: str = "",
    site: str = "",
    area: str = "",
    equipment: str = "",
    status: str = permit_state.PERMIT_DRAFT,
    risk_level: str = "",
    residual_risk_level: str = "",
    control_measures: str = "",
    designated_approver_id: str = "",
    ehs_reviewer_id: str = "",
    valid_from: object = "",
    valid_to: object = "",
    handback_note: str = "",
    closed_by: str = "",
    closed_at: str = "",
    closure_note: str = "",
    data_label: str = "",
    is_demo: bool | None = None,
    chemicals: Iterable[Mapping[str, Any]] = (),
    steps: Iterable[Mapping[str, Any]] = (),
    evidence: Iterable[Mapping[str, Any]] = (),
    jsa_items: Iterable[Mapping[str, Any]] = (),
    actor: str = "",
    user: Mapping[str, Any] | None = None,
    allow_non_draft: bool = False,
    audit_action: str = "permit.created",
    created_at: str = "",
    updated_at: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one permit aggregate and its audit event in a single transaction."""
    name = str(title or "").strip()
    if not name:
        raise ValueError("作业许可标题（title）不能为空。")

    state = str(status or "").strip()
    if state not in permit_state.PERMIT_STATUSES:
        raise ValueError(f"未知作业许可状态：{status!r}。")
    if state != permit_state.PERMIT_DRAFT and not allow_non_draft:
        raise ValueError(
            "新建作业许可只能从草稿状态开始；其他状态必须通过状态机命令或导入进入。"
        )
    if user is not None:
        permissions.assert_can(user, permissions.PERMIT_CREATE)

    identifier = str(permit_id or "").strip() or next_permit_id(connection)
    if _permit_row(connection, identifier) is not None:
        raise ValueError(f"作业许可编号已存在：{identifier}。")

    chemical_rows = [
        _normalise_chemical(item, index)
        for index, item in enumerate(chemicals, start=1)
    ]
    step_rows = [
        _normalise_step(item, index) for index, item in enumerate(steps, start=1)
    ]
    jsa_rows = [
        _normalise_jsa_item(item, index) for index, item in enumerate(jsa_items, start=1)
    ]

    evidence_by_track: dict[str, list[dict[str, Any]]] = {
        legacy_adapter.EVIDENCE_TRACK_PUBLIC: [],
        legacy_adapter.EVIDENCE_TRACK_SDS: [],
    }
    for index, item in enumerate(evidence, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"evidence 第 {index} 项必须是一个对象。")
        track = str(item.get("track", "")).strip()
        payload = {key: value for key, value in item.items() if key != "track"}
        if track not in _EVIDENCE_TRACKS:
            raise ValueError(f"evidence 第 {index} 项缺少有效 track。")
        evidence_by_track[track].append(payload)
    normalised_evidence = {
        track: _normalise_evidence(track, items)
        for track, items in evidence_by_track.items()
        if items
    }

    if not residual_risk_level and jsa_rows:
        residual_risk_level = permit_state.highest_risk_level(
            row.get("residual_risk_level", "") for row in jsa_rows
        )
    if not risk_level and jsa_rows:
        risk_level = permit_state.highest_risk_level(
            row.get("risk_level", "") for row in jsa_rows
        )
    if not control_measures and jsa_rows:
        parts = [
            str(row.get("proposed_controls", "") or row.get("existing_controls", ""))
            for row in jsa_rows
        ]
        control_measures = _dedup_segments("；".join(part for part in parts if part))

    label = str(data_label or "").strip()
    demo = legacy_adapter.is_demo_label(label) if is_demo is None else bool(is_demo)
    stamp = _stamp(now)
    created_stamp = str(created_at or "").strip() or stamp
    updated_stamp = str(updated_at or "").strip() or stamp
    correlation = audit.new_correlation_id()
    created_by = permissions.user_id(user) or str(actor or "").strip()

    with connection:
        connection.execute(
            "INSERT INTO permits ("
            "id, title, permit_type, site, area, equipment, applicant_id, owner_id, "
            "status, risk_level, residual_risk_level, control_measures, "
            "designated_approver_id, ehs_reviewer_id, valid_from, valid_to, "
            "handback_note, closure_note, "
            "closed_by, closed_at, version, data_label, is_demo, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (
                identifier,
                name,
                str(permit_type or "").strip() or DEFAULT_PERMIT_TYPE,
                str(site or "").strip(),
                str(area or "").strip(),
                str(equipment or "").strip(),
                str(applicant_id or "").strip(),
                str(owner_id or "").strip(),
                state,
                str(risk_level or "").strip(),
                str(residual_risk_level or "").strip(),
                str(control_measures or "").strip(),
                str(designated_approver_id or "").strip(),
                str(ehs_reviewer_id or "").strip(),
                str(valid_from or "").strip(),
                str(valid_to or "").strip(),
                str(handback_note or "").strip(),
                str(closure_note or "").strip(),
                str(closed_by or "").strip(),
                str(closed_at or "").strip(),
                label,
                1 if demo else 0,
                created_stamp,
                updated_stamp,
            ),
        )
        _insert_chemicals(connection, identifier, chemical_rows, is_demo=demo)
        _insert_steps(connection, identifier, step_rows)
        for track, items in normalised_evidence.items():
            _insert_evidence_rows(
                connection,
                identifier,
                track,
                items,
                is_demo=demo,
                data_label=label,
            )
        _insert_jsa_items(connection, identifier, jsa_rows, is_demo=demo)
        after = _permit_row(connection, identifier)
        audit.record_event(
            connection,
            audit.build_event(
                entity_type=audit.ENTITY_PERMIT,
                entity_id=identifier,
                action=str(audit_action or "permit.created"),
                actor=created_by,
                from_state="",
                to_state=state,
                reason="",
                before=None,
                after=after,
                correlation_id=correlation,
                now=now,
            ),
        )
    return get_permit(connection, identifier)


def add_evidence(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    track: str,
    item: Mapping[str, Any],
    actor: str = "",
    user: Mapping[str, Any] | None = None,
    is_demo: bool | None = None,
    data_label: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one evidence row to a permit's explicit evidence track."""
    permit = get_permit(connection, permit_id)
    if permit is None:
        raise KeyError(f"未找到作业许可：{permit_id}")
    if user is not None:
        permissions.assert_can(user, permissions.PERMIT_EDIT, entity=permit)
    effective_actor = permissions.user_id(user) or str(actor or "").strip()
    kind = str(track or "").strip()
    rows = _normalise_evidence(kind, [item])
    if not rows:
        raise ValueError("证据条目不能为空。")
    label = str(data_label or permit.get("data_label", ""))
    demo = bool(permit.get("is_demo")) if is_demo is None else bool(is_demo)
    before = {
        "public_sources": len(
            (permit.get("evidence") or {}).get(legacy_adapter.EVIDENCE_TRACK_PUBLIC, [])
        ),
        "sds": len(
            (permit.get("evidence") or {}).get(legacy_adapter.EVIDENCE_TRACK_SDS, [])
        ),
    }
    correlation = audit.new_correlation_id()
    with connection:
        _insert_evidence_rows(
            connection,
            str(permit_id),
            kind,
            rows,
            is_demo=demo,
            data_label=label,
        )
        after = dict(before)
        after["public_sources" if kind == legacy_adapter.EVIDENCE_TRACK_PUBLIC else "sds"] += 1
        audit.record_event(
            connection,
            audit.build_event(
                entity_type=audit.ENTITY_PERMIT,
                entity_id=str(permit_id),
                action="permit.evidence_added",
                actor=effective_actor,
                from_state=str(permit.get("status", "")),
                to_state=str(permit.get("status", "")),
                reason=f"追加证据轨道 {kind}",
                before=before,
                after=after,
                correlation_id=correlation,
                now=now,
            ),
        )
    return get_permit(connection, str(permit_id))


def get_permit(
    connection: sqlite3.Connection, permit_id: str
) -> dict[str, Any] | None:
    """Return the full permit aggregate, or ``None`` when missing."""
    permit = _permit_row(connection, permit_id)
    if permit is None:
        return None
    permit["is_demo"] = bool(permit.get("is_demo"))

    chemicals: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT * FROM permit_chemicals WHERE permit_id = ? ORDER BY id",
        (str(permit_id),),
    ).fetchall():
        item = dict(row)
        raw_aliases = str(item.pop("aliases_json", "") or "[]")
        try:
            item["aliases"] = json.loads(raw_aliases)
        except ValueError:
            item["aliases"] = []
        item["is_demo"] = bool(item.get("is_demo"))
        chemicals.append(item)
    permit["chemicals"] = chemicals

    permit["steps"] = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM permit_steps WHERE permit_id = ? ORDER BY step_no, id",
            (str(permit_id),),
        ).fetchall()
    ]

    evidence: dict[str, list[dict[str, Any]]] = {
        legacy_adapter.EVIDENCE_TRACK_PUBLIC: [],
        legacy_adapter.EVIDENCE_TRACK_SDS: [],
    }
    for row in connection.execute(
        "SELECT * FROM permit_evidence WHERE permit_id = ? ORDER BY id",
        (str(permit_id),),
    ).fetchall():
        item = dict(row)
        track = str(item.pop("track"))
        item["is_demo"] = bool(item.get("is_demo"))
        evidence.setdefault(track, []).append(item)
    permit["evidence"] = evidence

    permit["jsa_items"] = [
        {
            **dict(row),
            "is_demo": bool(row["is_demo"]),
        }
        for row in connection.execute(
            "SELECT * FROM permit_jsa_items WHERE permit_id = ? ORDER BY step_no, id",
            (str(permit_id),),
        ).fetchall()
    ]

    permit["prestart_checks"] = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM prestart_checks WHERE permit_id = ? ORDER BY id",
            (str(permit_id),),
        ).fetchall()
    ]

    linked = [
        str(row["id"])
        for row in connection.execute(
            "SELECT id, status FROM hazards WHERE permit_id = ? ORDER BY created_at, id",
            (str(permit_id),),
        ).fetchall()
    ]
    open_rows = connection.execute(
        "SELECT COUNT(*) AS total FROM hazards WHERE permit_id = ? AND status != ?",
        (str(permit_id), "closed"),
    ).fetchone()
    permit["linked_hazard_ids"] = linked
    permit["open_hazard_count"] = int(open_rows["total"]) if open_rows else 0
    return permit


def list_permits(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return permit rows (aggregate children omitted) newest first."""
    try:
        limit_value = max(int(limit), 0)
    except (TypeError, ValueError):
        limit_value = 200
    if status:
        cursor = connection.execute(
            "SELECT * FROM permits WHERE status = ? ORDER BY created_at DESC, id LIMIT ?",
            (str(status), limit_value),
        )
    else:
        cursor = connection.execute(
            "SELECT * FROM permits ORDER BY created_at DESC, id LIMIT ?",
            (limit_value,),
        )
    rows = []
    for row in cursor.fetchall():
        item = dict(row)
        item["is_demo"] = bool(item.get("is_demo"))
        rows.append(item)
    return rows


def _update_permit(
    connection: sqlite3.Connection,
    permit_id: str,
    status: str,
    stamp: str,
    extra: Mapping[str, Any] | None = None,
) -> None:
    fields: dict[str, Any] = {"status": status, "updated_at": stamp}
    fields.update(dict(extra or {}))
    assignments = ", ".join(f"{name} = ?" for name in fields)
    connection.execute(
        f"UPDATE permits SET {assignments}, version = version + 1 WHERE id = ?",
        (*fields.values(), str(permit_id)),
    )


def _write_transition(
    connection: sqlite3.Connection,
    permit: Mapping[str, Any],
    action: str,
    to_status: str,
    *,
    actor: str,
    reason: str,
    extra: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    stamp = _stamp(now)
    correlation = audit.new_correlation_id()
    before = _snapshot(permit)
    changes = dict(extra or {})
    if action == "close":
        changes.update({"closed_at": stamp, "closed_by": actor, "closure_note": reason})
    if action == "complete_work":
        changes["handback_note"] = reason
    with connection:
        _update_permit(connection, str(permit["id"]), to_status, stamp, changes)
        after = {**before, **changes, "status": to_status, "updated_at": stamp}
        audit.record_event(
            connection,
            audit.build_event(
                entity_type=audit.ENTITY_PERMIT,
                entity_id=str(permit["id"]),
                action=f"permit.{action}",
                actor=actor,
                from_state=str(permit.get("status", "")),
                to_state=to_status,
                reason=reason,
                before=before,
                after=after,
                correlation_id=correlation,
                now=now,
            ),
        )


def transition_permit(
    connection: sqlite3.Connection,
    permit_id: str,
    action: str,
    *,
    actor: str = "",
    actor_role: str = "",
    reason: str = "",
    context: Mapping[str, Any] | None = None,
    user: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and apply one permit transition with its audit event.

    ``user`` is the Demo persona mapping.  When supplied, the permission layer
    runs first (who may attempt the action) and the state machine second
    (whether the status allows it); both must pass.
    """
    permit = get_permit(connection, permit_id)
    if permit is None:
        raise KeyError(f"未找到作业许可：{permit_id}")
    effective_actor, effective_role = _resolve_actor(user, actor, actor_role)
    if user is not None:
        permission = permissions.permission_for_permit_action(action)
        if permission:
            permissions.assert_can(user, permission, entity=permit)
    values = _permit_context(connection, permit)
    values.update(dict(context or {}))
    result = permit_state.validate_permit_transition(
        str(permit.get("status", "")),
        action,
        actor_role=effective_role,
        actor_id=effective_actor,
        reason=reason,
        context=values,
        now=now,
    )
    if not result.allowed:
        raise ValueError(result.message)
    extra: dict[str, Any] = {}
    if action in {"submit", "resubmit"}:
        risk = str(
            permit.get("residual_risk_level", "")
            or permit.get("risk_level", "")
            or ""
        )
        extra["approval_due_at"] = sla.calculate_due_at(
            now or datetime.now(), sla.approval_days(risk)
        ).isoformat(timespec="seconds")
    _write_transition(
        connection,
        permit,
        action,
        result.to_status,
        actor=effective_actor,
        reason=str(reason or "").strip(),
        extra=extra or None,
        now=now,
    )
    return get_permit(connection, str(permit_id))


def submit_permit(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    actor: str = "",
    actor_role: str = roles.ROLE_APPLICANT,
    user: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """草稿 → 待EHS审核.  Requires chemicals, SDS evidence and JSA items."""
    return transition_permit(
        connection,
        permit_id,
        "submit",
        actor=actor,
        actor_role=actor_role,
        user=user,
        now=now,
    )


def complete_ehs_review(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    actor: str = "",
    decision: str,
    reason: str = "",
    actor_role: str = roles.ROLE_EHS_REVIEWER,
    user: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """EHS 审核结论：``confirm`` 进入审批，``return_draft`` 退回草稿。"""
    action = str(decision or "").strip()
    if action not in {"confirm", "return_draft"}:
        raise ValueError("EHS 审核决定必须是 confirm 或 return_draft。")
    return transition_permit(
        connection,
        permit_id,
        action,
        actor=actor,
        actor_role=actor_role,
        reason=reason,
        user=user,
        now=now,
    )


def decide_approval(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    actor: str = "",
    decision: str,
    comment: str = "",
    actor_role: str = roles.ROLE_APPROVER,
    user: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """批准人决定：``approve`` 进入已批准，``reject`` 退回到 returned。"""
    action = str(decision or "").strip()
    if action not in {"approve", "reject"}:
        raise ValueError("审批决定必须是 approve 或 reject。")
    permit = _permit_row(connection, permit_id)
    if permit is None:
        raise KeyError(f"未找到作业许可：{permit_id}")
    effective_actor, effective_role = _resolve_actor(user, actor, actor_role)
    if user is not None:
        permissions.assert_can(user, permissions.PERMIT_APPROVE, entity=permit)
    designated = str(permit.get("designated_approver_id", "") or "").strip()
    if designated and effective_actor != designated:
        raise ValueError(
            f"该作业许可指定批准人为 {designated}，当前操作人无权批准。"
        )
    return transition_permit(
        connection,
        permit_id,
        action,
        actor=effective_actor,
        actor_role=effective_role,
        reason=comment,
        now=now,
    )


def _normalise_check(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    code = str(item.get("item_code", "")).strip()
    if not code:
        raise ValueError(f"开工前检查第 {index} 项缺少 item_code。")
    result = str(item.get("result", "pending")).strip() or "pending"
    if result not in {"pending", "pass", "fail"}:
        raise ValueError(f"开工前检查第 {index} 项结果必须是 pending/pass/fail。")
    return {
        "item_code": code,
        "item_text": str(item.get("item_text", "")).strip(),
        "required": 1 if item.get("required", True) else 0,
        "result": result,
        "checked_by": str(item.get("checked_by", "")).strip(),
        "checked_at": str(item.get("checked_at", "")).strip(),
        "evidence_note": str(item.get("evidence_note", "")).strip(),
    }


def confirm_prestart(
    connection: sqlite3.Connection,
    permit_id: str,
    checks: Iterable[Mapping[str, Any]],
    *,
    actor: str = "",
    actor_role: str = roles.ROLE_ACTION_OWNER,
    reason: str = "",
    user: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the pre-start checks and activate the permit when all pass."""
    permit = get_permit(connection, permit_id)
    if permit is None:
        raise KeyError(f"未找到作业许可：{permit_id}")
    effective_actor, effective_role = _resolve_actor(user, actor, actor_role)
    if user is not None:
        permissions.assert_can(
            user, permissions.PERMIT_PRESTART_CONFIRM, entity=permit
        )
    rows = [
        _normalise_check(item, index) for index, item in enumerate(checks, start=1)
    ]
    if not rows:
        raise ValueError("开工前检查不能为空。")
    required = [row for row in rows if row["required"]]
    if not required:
        raise ValueError("开工前检查至少需要 1 条必检项。")
    passed = all(row["result"] == "pass" for row in required)
    result = permit_state.validate_permit_transition(
        str(permit.get("status", "")),
        "prestart_confirm",
        actor_role=effective_role,
        actor_id=effective_actor,
        reason=reason,
        context={
            "prestart_passed": passed,
            "valid_to": permit.get("valid_to", ""),
        },
        now=now,
    )
    if not result.allowed:
        raise ValueError(result.message)

    stamp = _stamp(now)
    correlation = audit.new_correlation_id()
    before = _snapshot(permit)
    checked_by = effective_actor
    with connection:
        for row in rows:
            connection.execute(
                "INSERT INTO prestart_checks ("
                "permit_id, item_code, item_text, required, result, checked_by, "
                "checked_at, evidence_note"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(permit_id),
                    row["item_code"],
                    row["item_text"],
                    row["required"],
                    row["result"],
                    row["checked_by"] or checked_by,
                    row["checked_at"] or stamp,
                    row["evidence_note"],
                ),
            )
        _update_permit(connection, str(permit_id), result.to_status, stamp)
        audit.record_event(
            connection,
            audit.build_event(
                entity_type=audit.ENTITY_PERMIT,
                entity_id=str(permit_id),
                action="permit.prestart_confirm",
                actor=checked_by,
                from_state=str(permit.get("status", "")),
                to_state=result.to_status,
                reason=reason,
                before=before,
                after={
                    **before,
                    "status": result.to_status,
                    "updated_at": stamp,
                    "prestart_checks": rows,
                },
                correlation_id=correlation,
                now=now,
            ),
        )
    return get_permit(connection, str(permit_id))


def complete_work(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    actor: str = "",
    handback_note: str,
    actor_role: str = roles.ROLE_ACTION_OWNER,
    user: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """作业完成并交还现场：active → closeout_review."""
    return transition_permit(
        connection,
        permit_id,
        "complete_work",
        actor=actor,
        actor_role=actor_role,
        reason=handback_note,
        user=user,
        now=now,
    )


def suspend_permit(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    actor: str = "",
    reason: str,
    actor_role: str = roles.ROLE_ACTION_OWNER,
    user: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """暂停执行：active → suspended."""
    return transition_permit(
        connection,
        permit_id,
        "suspend",
        actor=actor,
        actor_role=actor_role,
        reason=reason,
        user=user,
        now=now,
    )


def resume_permit(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    actor: str = "",
    actor_role: str = roles.ROLE_ACTION_OWNER,
    user: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """恢复执行：suspended → active."""
    return transition_permit(
        connection,
        permit_id,
        "resume",
        actor=actor,
        actor_role=actor_role,
        user=user,
        now=now,
    )


def close_permit(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    actor: str = "",
    note: str = "",
    actor_role: str = roles.ROLE_EHS_REVIEWER,
    user: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """关闭作业许可：closeout_review → closed；不允许有未关闭隐患。"""
    return transition_permit(
        connection,
        permit_id,
        "close",
        actor=actor,
        actor_role=actor_role,
        reason=note,
        user=user,
        now=now,
    )


def cancel_permit(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    actor: str = "",
    reason: str,
    actor_role: str = roles.ROLE_APPLICANT,
    user: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """取消未激活的作业许可（终态）。"""
    return transition_permit(
        connection,
        permit_id,
        "cancel",
        actor=actor,
        actor_role=actor_role,
        reason=reason,
        user=user,
        now=now,
    )


def expire_permit(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    actor: str = "system",
    actor_role: str = roles.ROLE_SYSTEM,
    now: datetime | None = None,
) -> dict[str, Any]:
    """标记超过 valid_to 的作业许可（终态）。"""
    return transition_permit(
        connection,
        permit_id,
        "expire",
        actor=actor,
        actor_role=actor_role,
        now=now,
    )


def _apply_assignment(
    connection: sqlite3.Connection,
    permit: Mapping[str, Any],
    field: str,
    value: str,
    action: str,
    *,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if str(permit.get(field, "") or "").strip() == value:
        return get_permit(connection, str(permit["id"]))
    before = _snapshot(permit)
    stamp = _stamp(now)
    correlation = audit.new_correlation_id()
    with connection:
        connection.execute(
            f"UPDATE permits SET {field} = ?, updated_at = ?, "
            "version = version + 1 WHERE id = ?",
            (value, stamp, str(permit["id"])),
        )
        after = {**before, field: value, "updated_at": stamp}
        audit.record_event(
            connection,
            audit.build_event(
                entity_type=audit.ENTITY_PERMIT,
                entity_id=str(permit["id"]),
                action=f"permit.{action}",
                actor=actor,
                from_state=str(permit.get("status", "")),
                to_state=str(permit.get("status", "")),
                reason=reason,
                before=before,
                after=after,
                correlation_id=correlation,
                now=now,
            ),
        )
    return get_permit(connection, str(permit["id"]))


def _assignment_target(
    connection: sqlite3.Connection, target_id: str, permission: str
) -> dict[str, Any]:
    return persona_service.require_role_user(connection, target_id, permission)


def assign_permit_owner(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    owner_id: str,
    actor: str = "",
    user: Mapping[str, Any] | None = None,
    reason: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assign the permit owner to an active demo user."""
    permit = get_permit(connection, permit_id)
    if permit is None:
        raise KeyError(f"未找到作业许可：{permit_id}")
    if str(permit.get("status", "")) in permit_state.PERMIT_TERMINAL_STATUSES:
        raise ValueError("已终结的作业许可不能变更负责人。")
    if user is not None:
        permissions.assert_can(user, permissions.PERMIT_EDIT, entity=permit)
    target = _assignment_target(
        connection, owner_id, permissions.PERMIT_PRESTART_CONFIRM
    )
    effective_actor = permissions.user_id(user) or str(actor or "").strip()
    return _apply_assignment(
        connection,
        permit,
        "owner_id",
        str(target["id"]),
        "assign_owner",
        actor=effective_actor,
        reason=str(reason or "").strip()
        or f"指派作业负责人为 {target['display_name']}",
        now=now,
    )


def assign_permit_ehs_reviewer(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    reviewer_id: str,
    actor: str = "",
    user: Mapping[str, Any] | None = None,
    reason: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assign the EHS reviewer of one permit to an active demo user."""
    permit = get_permit(connection, permit_id)
    if permit is None:
        raise KeyError(f"未找到作业许可：{permit_id}")
    if str(permit.get("status", "")) in permit_state.PERMIT_TERMINAL_STATUSES:
        raise ValueError("已终结的作业许可不能变更 EHS 审核人。")
    if user is not None:
        permissions.assert_can(user, permissions.PERMIT_EDIT, entity=permit)
    target = _assignment_target(
        connection, reviewer_id, permissions.PERMIT_EHS_REVIEW
    )
    effective_actor = permissions.user_id(user) or str(actor or "").strip()
    return _apply_assignment(
        connection,
        permit,
        "ehs_reviewer_id",
        str(target["id"]),
        "assign_ehs_reviewer",
        actor=effective_actor,
        reason=str(reason or "").strip()
        or f"指派 EHS 审核人为 {target['display_name']}",
        now=now,
    )


def assign_permit_approver(
    connection: sqlite3.Connection,
    permit_id: str,
    *,
    approver_id: str,
    actor: str = "",
    user: Mapping[str, Any] | None = None,
    reason: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assign the designated approver of one permit to an active demo user."""
    permit = get_permit(connection, permit_id)
    if permit is None:
        raise KeyError(f"未找到作业许可：{permit_id}")
    if str(permit.get("status", "")) in permit_state.PERMIT_TERMINAL_STATUSES:
        raise ValueError("已终结的作业许可不能变更批准人。")
    if user is not None:
        permissions.assert_can(user, permissions.PERMIT_EDIT, entity=permit)
    target = _assignment_target(
        connection, approver_id, permissions.PERMIT_APPROVE
    )
    effective_actor = permissions.user_id(user) or str(actor or "").strip()
    return _apply_assignment(
        connection,
        permit,
        "designated_approver_id",
        str(target["id"]),
        "assign_approver",
        actor=effective_actor,
        reason=str(reason or "").strip()
        or f"指派批准人为 {target['display_name']}",
        now=now,
    )


def import_legacy_job(
    connection: sqlite3.Connection,
    job: Mapping[str, Any],
    *,
    actor: str = "legacy_adapter",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Import one V4 JobRecord through the adapter into the V5 store."""
    mapped = legacy_adapter.job_to_permit(job)
    if not mapped.get("permit_id"):
        raise ValueError("V4 作业单缺少 job_id，无法导入。")
    if not mapped.get("status"):
        raise ValueError(
            f"无法识别的 V4 作业状态：{job.get('status')!r}。"
        )
    if _permit_row(connection, mapped["permit_id"]) is not None:
        raise ValueError(f"作业许可已存在：{mapped['permit_id']}。")
    return create_permit(
        connection,
        permit_id=str(mapped["permit_id"]),
        title=str(mapped["title"]),
        permit_type=str(mapped["permit_type"] or DEFAULT_PERMIT_TYPE),
        applicant_id=str(mapped.get("applicant_id", "")),
        owner_id=str(mapped.get("owner_id", "")),
        area=str(mapped.get("area", "")),
        status=str(mapped["status"]),
        risk_level=str(mapped.get("risk_level", "")),
        residual_risk_level=str(mapped.get("residual_risk_level", "")),
        control_measures=str(mapped.get("control_measures", "")),
        valid_from=str(mapped.get("valid_from", "")),
        valid_to=str(mapped.get("valid_to", "")),
        handback_note=str(mapped.get("handback_note", "")),
        closed_by=str(mapped.get("closed_by", "")),
        closed_at=str(mapped.get("closed_at", "")),
        closure_note=str(mapped.get("closure_note", "")),
        data_label=str(mapped.get("data_label", "")),
        is_demo=bool(mapped.get("is_demo")),
        chemicals=mapped.get("chemicals", ()),
        steps=mapped.get("steps", ()),
        evidence=mapped.get("evidence", ()),
        jsa_items=mapped.get("jsa_items", ()),
        actor=actor,
        audit_action="permit.imported",
        allow_non_draft=True,
        created_at=str(mapped.get("created_at", "")),
        updated_at=str(mapped.get("updated_at", "")),
        now=now,
    )


__all__ = [
    "DEFAULT_PERMIT_TYPE",
    "add_evidence",
    "assign_permit_approver",
    "assign_permit_ehs_reviewer",
    "assign_permit_owner",
    "cancel_permit",
    "close_permit",
    "complete_ehs_review",
    "complete_work",
    "confirm_prestart",
    "create_permit",
    "decide_approval",
    "expire_permit",
    "get_permit",
    "import_legacy_job",
    "list_permits",
    "next_permit_id",
    "resume_permit",
    "submit_permit",
    "suspend_permit",
    "transition_permit",
]

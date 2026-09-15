"""Persistence and audit service for Phase 1 work drafts and review packs."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Iterable, Mapping

import evidence_adapter
import safety_review
from workflow import audit, permissions
from workflow import permit_state

from . import permit_service, persona_service


ENTITY_WORK_DRAFT = "work_draft"
ENTITY_SAFETY_REVIEW_PACK = "safety_review_pack"

_JSON_COLUMNS = {
    "work_steps": "work_steps_json",
    "chemicals": "chemicals_json",
    "user_risk_tags": "user_risk_tags_json",
    "ai_risk_tags": "ai_risk_tags_json",
    "confirmed_risk_tags": "confirmed_risk_tags_json",
    "documents": "documents_json",
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _stamp(now: datetime | None = None) -> str:
    return safety_review.stamp(now)


def _actor_id(user: Mapping[str, Any]) -> str:
    return permissions.user_id(user)


def _resolve_owner_id(connection: sqlite3.Connection, draft: Mapping[str, Any]) -> str:
    """Map the WorkDraft's ``负责人`` onto a permit owner id.

    The draft stores a free-text name; when it matches a demo user's display
    name that user becomes the owner, otherwise the draft creator (applicant)
    carries the permit until it is reassigned. Permissions are untouched.
    """
    person = str(draft.get("responsible_person") or "").strip()
    if person:
        for user in persona_service.list_users(connection):
            if str(user.get("display_name") or "").strip() == person:
                return str(user.get("id") or "")
    return str(draft.get("created_by") or "")


def _chemical_key(name: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(name).casefold())


def _canonical_chemical(name: str) -> str:
    """Return the canonical Chinese name for a declared chemical.

    ``Methanol`` / ``甲醇`` / ``methyl alcohol`` all normalise to ``甲醇``
    using the existing alias vocabulary; unknown names pass through as-is.
    """
    key = _chemical_key(name)
    if not key:
        return str(name).strip()
    for canonical, aliases in evidence_adapter._CHEMICAL_ALIASES.items():
        if key in {_chemical_key(alias) for alias in aliases}:
            return canonical
    return str(name).strip()


def _permit_chemicals(
    draft: Mapping[str, Any], pack: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Deduplicate draft chemicals and attach the confirmed SDS citation.

    Repeated names in different spellings collapse into one entity (extra
    spellings become aliases), and the entity's ``sds_file``/``sds_status``
    come from the pack's complete SDS citation that matches the chemical —
    the same evidence the permit already carries in its SDS track.
    """
    sds_citations = [
        item
        for item in (pack.get("evidence") or ())
        if isinstance(item, Mapping)
        and str(item.get("source_type") or "") == "sds"
        and safety_review.citation_is_complete(item)
    ]
    rows: list[dict[str, Any]] = []
    for name in draft.get("chemicals") or ():
        label = str(name).strip()
        if not label:
            continue
        canonical = _canonical_chemical(label)
        row = next((r for r in rows if r["chemical_name"] == canonical), None)
        if row is None:
            match = next(
                (
                    citation
                    for citation in sds_citations
                    if evidence_adapter.citation_matches_chemical(citation, canonical)
                ),
                None,
            )
            row = {
                "chemical_name": canonical,
                "aliases": [],
                "sds_file": str(match.get("source_name") or "") if match else "",
                "sds_status": "confirmed" if match else "",
            }
            rows.append(row)
        if label != canonical and label not in row["aliases"]:
            row["aliases"].append(label)
    return rows


def _next_id(connection: sqlite3.Connection) -> str:
    rows = connection.execute("SELECT id FROM work_drafts").fetchall()
    numbers = []
    for row in rows:
        value = str(row[0])
        if value.startswith("WD-") and value[3:].isdigit():
            numbers.append(int(value[3:]))
    return f"WD-{max(numbers, default=0) + 1:04d}"


def _decode_draft(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    for public_name, column_name in _JSON_COLUMNS.items():
        try:
            record[public_name] = json.loads(record.pop(column_name) or "[]")
        except (TypeError, ValueError):
            record[public_name] = []
    record["contractor_involved"] = bool(record.get("contractor_involved"))
    return safety_review.normalize_work_draft(record)


def get_work_draft(connection: sqlite3.Connection, draft_id: str) -> dict[str, Any] | None:
    return _decode_draft(
        connection.execute("SELECT * FROM work_drafts WHERE id = ?", (str(draft_id),)).fetchone()
    )


def require_work_draft(connection: sqlite3.Connection, draft_id: str) -> dict[str, Any]:
    draft = get_work_draft(connection, draft_id)
    if draft is None:
        raise ValueError(f"未找到高风险作业草稿：{draft_id!r}。")
    return draft


def list_work_drafts(connection: sqlite3.Connection, *, limit: int = 200) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM work_drafts ORDER BY updated_at DESC, id DESC LIMIT ?",
        (max(int(limit), 0),),
    ).fetchall()
    return [item for item in (_decode_draft(row) for row in rows) if item is not None]


def create_work_draft(
    connection: sqlite3.Connection,
    *,
    user: Mapping[str, Any],
    now: datetime | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Create a WorkDraft. Only the existing permit-create role may do this."""
    permissions.assert_can(user, permissions.PERMIT_CREATE)
    actor = _actor_id(user)
    timestamp = _stamp(now)
    record = safety_review.normalize_work_draft(
        {
            **fields,
            "id": str(fields.get("id") or _next_id(connection)),
            "status": safety_review.STATUS_DRAFT,
            "created_by": actor,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )
    if not record["title"]:
        raise ValueError("作业名称不能为空。")
    correlation = audit.new_correlation_id()
    with connection:
        connection.execute(
            """
            INSERT INTO work_drafts (
                id, title, work_type, description, location, planned_start,
                planned_end, people_count, responsible_person, contractor_involved,
                work_steps_json, chemicals_json, user_risk_tags_json,
                ai_risk_tags_json, confirmed_risk_tags_json, documents_json,
                status, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["id"], record["title"], record["work_type"], record["description"],
                record["location"], record["planned_start"], record["planned_end"],
                record["people_count"], record["responsible_person"],
                int(record["contractor_involved"]), _json(record["work_steps"]),
                _json(record["chemicals"]), _json(record["user_risk_tags"]),
                _json(record["ai_risk_tags"]), _json(record["confirmed_risk_tags"]),
                _json(record["documents"]), record["status"], record["created_by"],
                record["created_at"], record["updated_at"],
            ),
        )
        audit.record_event(
            connection,
            audit.build_event(
                entity_type=ENTITY_WORK_DRAFT,
                entity_id=record["id"],
                action="work_draft.created",
                actor=actor,
                to_state=record["status"],
                reason="创建高风险作业准备草稿",
                after=record,
                correlation_id=correlation,
                now=now,
            ),
        )
    return require_work_draft(connection, record["id"])


def update_work_draft(
    connection: sqlite3.Connection,
    draft_id: str,
    *,
    user: Mapping[str, Any],
    now: datetime | None = None,
    **changes: Any,
) -> dict[str, Any]:
    permissions.assert_can(user, permissions.PERMIT_EDIT)
    before = require_work_draft(connection, draft_id)
    if before["status"] == safety_review.STATUS_CONFIRMED:
        raise ValueError("已确认的审核包不能直接修改；请新建作业准备草稿。")
    allowed = set(before) - {"id", "created_by", "created_at", "updated_at"}
    merged = {**before, **{key: value for key, value in changes.items() if key in allowed}}
    merged["updated_at"] = _stamp(now)
    after = safety_review.normalize_work_draft(merged)
    actor = _actor_id(user)
    with connection:
        connection.execute(
            """
            UPDATE work_drafts SET
                title=?, work_type=?, description=?, location=?, planned_start=?,
                planned_end=?, people_count=?, responsible_person=?, contractor_involved=?,
                work_steps_json=?, chemicals_json=?, user_risk_tags_json=?,
                ai_risk_tags_json=?, confirmed_risk_tags_json=?, documents_json=?,
                status=?, updated_at=? WHERE id=?
            """,
            (
                after["title"], after["work_type"], after["description"], after["location"],
                after["planned_start"], after["planned_end"], after["people_count"],
                after["responsible_person"], int(after["contractor_involved"]),
                _json(after["work_steps"]), _json(after["chemicals"]),
                _json(after["user_risk_tags"]), _json(after["ai_risk_tags"]),
                _json(after["confirmed_risk_tags"]), _json(after["documents"]),
                after["status"], after["updated_at"], draft_id,
            ),
        )
        audit.record_event(
            connection,
            audit.build_event(
                entity_type=ENTITY_WORK_DRAFT,
                entity_id=draft_id,
                action="work_draft.updated",
                actor=actor,
                from_state=before["status"],
                to_state=after["status"],
                reason="更新高风险作业准备草稿",
                before=before,
                after=after,
                now=now,
            ),
        )
    return require_work_draft(connection, draft_id)


def _decode_pack(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    try:
        payload = json.loads(record.pop("payload_json") or "{}")
    except (TypeError, ValueError):
        payload = {}
    result = dict(payload) if isinstance(payload, dict) else {}
    result.update(record)
    result["version"] = int(result.get("version") or 0)
    return result


def get_latest_pack(connection: sqlite3.Connection, draft_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM safety_review_packs WHERE draft_id=? ORDER BY version DESC LIMIT 1",
        (str(draft_id),),
    ).fetchone()
    return _decode_pack(row)


def list_pack_versions(connection: sqlite3.Connection, draft_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM safety_review_packs WHERE draft_id=? ORDER BY version DESC",
        (str(draft_id),),
    ).fetchall()
    return [item for item in (_decode_pack(row) for row in rows) if item is not None]


def save_pack_version(
    connection: sqlite3.Connection,
    draft_id: str,
    pack: Mapping[str, Any],
    *,
    user: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    permissions.assert_can(user, permissions.PERMIT_EDIT)
    draft = require_work_draft(connection, draft_id)
    if draft["status"] == safety_review.STATUS_CONFIRMED:
        raise ValueError("审核包已确认，不能再生成新版本。")
    latest = get_latest_pack(connection, draft_id)
    version = int(latest.get("version", 0) if latest else 0) + 1
    pack_id = str((latest or {}).get("pack_id") or pack.get("pack_id") or f"SRP-{draft_id[3:]}")
    payload = dict(pack)
    payload.update(
        {
            "pack_id": pack_id,
            "draft_id": draft_id,
            "version": version,
            "created_at": _stamp(now),
            "confirmed_at": "",
        }
    )
    blockers = safety_review.pack_blockers(draft, payload)
    new_status = safety_review.STATUS_NEEDS_INPUT if blockers else safety_review.STATUS_REVIEW_READY
    actor = _actor_id(user)
    with connection:
        connection.execute(
            "INSERT INTO safety_review_packs "
            "(pack_id, draft_id, version, payload_json, created_at, confirmed_by, confirmed_at) "
            "VALUES (?, ?, ?, ?, ?, '', '')",
            (pack_id, draft_id, version, _json(payload), payload["created_at"]),
        )
        connection.execute(
            "UPDATE work_drafts SET status=?, updated_at=? WHERE id=?",
            (new_status, payload["created_at"], draft_id),
        )
        audit.record_event(
            connection,
            audit.build_event(
                entity_type=ENTITY_SAFETY_REVIEW_PACK,
                entity_id=pack_id,
                action="safety_review_pack.version_created",
                actor=actor,
                from_state=draft["status"],
                to_state=new_status,
                reason=f"生成Safety Review Pack v{version}",
                before=latest,
                after={**payload, "blockers": blockers},
                now=now,
            ),
        )
    return get_latest_pack(connection, draft_id) or payload


def confirm_pack(
    connection: sqlite3.Connection,
    draft_id: str,
    *,
    user: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Confirm the latest complete pack. This never creates a Permit."""
    permissions.assert_can(user, permissions.PERMIT_EHS_REVIEW)
    draft = require_work_draft(connection, draft_id)
    pack = get_latest_pack(connection, draft_id)
    if pack is None:
        raise ValueError("尚未生成Safety Review Pack。")
    blockers = safety_review.pack_blockers(draft, pack)
    if blockers:
        raise ValueError("审核包不能确认：" + "；".join(blockers))
    if draft["status"] == safety_review.STATUS_CONFIRMED:
        return pack
    timestamp = _stamp(now)
    actor = _actor_id(user)
    payload = {key: value for key, value in pack.items() if key not in {
        "confirmed_by", "confirmed_at"
    }}
    payload["confirmed_at"] = timestamp
    with connection:
        connection.execute(
            "UPDATE safety_review_packs SET payload_json=?, confirmed_by=?, confirmed_at=? "
            "WHERE pack_id=? AND version=?",
            (_json(payload), actor, timestamp, pack["pack_id"], int(pack["version"])),
        )
        connection.execute(
            "UPDATE work_drafts SET status=?, updated_at=? WHERE id=?",
            (safety_review.STATUS_CONFIRMED, timestamp, draft_id),
        )
        audit.record_event(
            connection,
            audit.build_event(
                entity_type=ENTITY_SAFETY_REVIEW_PACK,
                entity_id=str(pack["pack_id"]),
                action="safety_review_pack.confirmed",
                actor=actor,
                from_state=draft["status"],
                to_state=safety_review.STATUS_CONFIRMED,
                reason="EHS人工确认Safety Review Pack",
                before=pack,
                after=payload,
                now=now,
            ),
        )
    return get_latest_pack(connection, draft_id) or payload


def get_pack_permit_link(connection: sqlite3.Connection, draft_id: str) -> dict[str, Any] | None:
    """Return the permit created from this draft's confirmed pack, or ``None``."""
    row = connection.execute(
        "SELECT * FROM permit_sources WHERE draft_id = ? ORDER BY created_at DESC LIMIT 1",
        (str(draft_id),),
    ).fetchone()
    return _decode_link(row)


def get_permit_source(connection: sqlite3.Connection, permit_id: str) -> dict[str, Any] | None:
    """Return the Safety Review Pack provenance of one permit, or ``None``."""
    row = connection.execute(
        "SELECT * FROM permit_sources WHERE permit_id = ?", (str(permit_id),)
    ).fetchone()
    return _decode_link(row)


def _decode_link(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    try:
        record["snapshot"] = json.loads(record.pop("snapshot_json") or "{}")
    except (TypeError, ValueError):
        record["snapshot"] = {}
    return record


def create_permit_from_pack(
    connection: sqlite3.Connection,
    draft_id: str,
    *,
    user: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create the formal Permit from a confirmed Safety Review Pack.

    Guardrails (Phase 1 rules are never relaxed):

    - the WorkDraft must be ``confirmed`` and the pack carry a confirmation stamp;
    - ``safety_review.pack_blockers`` must be empty (covers unresolved
      chemical mismatches, evidence-insufficient and unconfirmed human L/S);
    - the resulting Permit always starts from the existing draft state and
      walks the unchanged review/approval flow afterwards.

    The operation is idempotent: one confirmed pack maps to exactly one
    Permit; repeated calls return the existing Permit with ``created=False``.
    """
    permissions.assert_can(user, permissions.PERMIT_CREATE)
    actor = _actor_id(user)
    draft = require_work_draft(connection, draft_id)

    existing = get_pack_permit_link(connection, draft_id)
    if existing is not None:
        permit = permit_service.get_permit(connection, str(existing["permit_id"]))
        if permit is not None:
            return {"permit": permit, "created": False, "source": existing}

    if draft["status"] != safety_review.STATUS_CONFIRMED:
        raise ValueError(
            "Safety Review Pack 尚未由EHS确认，不能创建正式作业许可。"
        )
    pack = get_latest_pack(connection, draft_id)
    if pack is None:
        raise ValueError("尚未生成Safety Review Pack，不能创建正式作业许可。")
    if not str(pack.get("confirmed_at") or "").strip():
        raise ValueError("Safety Review Pack 缺少确认记录，不能创建正式作业许可。")
    blockers = safety_review.pack_blockers(draft, pack)
    if blockers:
        raise ValueError(
            "审核包存在未解决的阻断项，不能创建正式作业许可：" + "；".join(blockers)
        )

    jsa_items = [
        {**dict(item), "confirmed_at": str(pack.get("confirmed_at") or "")}
        for item in (pack.get("jsa_draft") or ())
        if isinstance(item, Mapping)
    ]
    # The pack's confirmed SDS citations are the permit's SDS evidence track
    # (same evidence, same track; the strict sds rules require a real PDF
    # source name, page and snippet — exactly what a complete citation holds).
    # SOP/internal citations stay reachable through the pack provenance link.
    sds_evidence = [
        {
            "track": "sds",
            "evidence_id": str(item.get("evidence_id") or ""),
            "source": str(item.get("source_name") or ""),
            "page": int(item.get("page") or 0),
            "sections": str(item.get("locator") or ""),
            "snippet": str(item.get("snippet") or ""),
            "topic": "chemical",
            "captured_at": "",
        }
        for item in (pack.get("evidence") or ())
        if isinstance(item, Mapping)
        and str(item.get("source_type") or "") == "sds"
        and safety_review.citation_is_complete(item)
    ]
    timestamp = _stamp(now)
    created = permit_service.create_permit(
        connection,
        title=str(draft.get("title") or ""),
        permit_type=str(draft.get("work_type") or "") or permit_service.DEFAULT_PERMIT_TYPE,
        applicant_id=str(draft.get("created_by") or ""),
        owner_id=_resolve_owner_id(connection, draft),
        area=str(draft.get("location") or ""),
        valid_from=str(draft.get("planned_start") or ""),
        valid_to=str(draft.get("planned_end") or ""),
        chemicals=_permit_chemicals(draft, pack),
        steps=[
            {"step_no": index, "name": str(step)}
            for index, step in enumerate(draft.get("work_steps") or (), start=1)
        ],
        evidence=sds_evidence,
        jsa_items=jsa_items,
        is_demo=True,
        user=user,
        audit_action="permit.created_from_pack",
        now=now,
    )
    permit_id = str(created["id"])
    snapshot = {
        "description": str(draft.get("description") or ""),
        "people_count": draft.get("people_count"),
        "contractor_involved": bool(draft.get("contractor_involved")),
        "responsible_person": str(draft.get("responsible_person") or ""),
        "confirmed_risk_tags": list(draft.get("confirmed_risk_tags") or ()),
        "risk_findings": [
            str(item.get("text") if isinstance(item, Mapping) else item)
            for item in (pack.get("risk_findings") or ())
        ],
    }
    correlation = audit.new_correlation_id()
    with connection:
        connection.execute(
            "INSERT INTO permit_sources ("
            "permit_id, draft_id, pack_id, pack_version, snapshot_json, "
            "confirmed_by, confirmed_at, created_by, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                permit_id,
                str(draft_id),
                str(pack.get("pack_id") or ""),
                int(pack.get("version") or 0),
                _json(snapshot),
                str(pack.get("confirmed_by") or ""),
                str(pack.get("confirmed_at") or ""),
                actor,
                timestamp,
            ),
        )
        audit.record_event(
            connection,
            audit.build_event(
                entity_type=ENTITY_WORK_DRAFT,
                entity_id=str(draft_id),
                action="work_draft.permit_created",
                actor=actor,
                from_state=safety_review.STATUS_CONFIRMED,
                to_state=safety_review.STATUS_CONFIRMED,
                reason=(
                    f"已确认Safety Review Pack v{pack.get('version')} 创建正式作业许可 {permit_id}"
                ),
                before=None,
                after={
                    "permit_id": permit_id,
                    "pack_id": str(pack.get("pack_id") or ""),
                    "pack_version": int(pack.get("version") or 0),
                    "pack_status": str(pack.get("status") or ""),
                    "permit_status": permit_state.PERMIT_DRAFT,
                },
                correlation_id=correlation,
                now=now,
            ),
        )
    return {"permit": created, "created": True, "source": get_permit_source(connection, permit_id)}


__all__ = [
    "ENTITY_SAFETY_REVIEW_PACK",
    "ENTITY_WORK_DRAFT",
    "confirm_pack",
    "create_permit_from_pack",
    "create_work_draft",
    "get_latest_pack",
    "get_pack_permit_link",
    "get_permit_source",
    "get_work_draft",
    "list_pack_versions",
    "list_work_drafts",
    "require_work_draft",
    "save_pack_version",
    "update_work_draft",
]

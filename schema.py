"""SQLite schema for the V5 P0A domain model.

The schema is additive and idempotent: :func:`initialize` may run on every new
connection without destroying existing data.  A ``schema_meta`` row records the
schema version so a later migration can compare versions instead of guessing.

SQLite is used as a portfolio-prototype store only.  Nothing here claims
production-grade durability, concurrent-write safety or regulatory-grade audit.
"""

from __future__ import annotations

import sqlite3


SCHEMA_VERSION = "3"

DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        is_demo INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS permits (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        permit_type TEXT NOT NULL DEFAULT '危化品非例行作业',
        site TEXT NOT NULL DEFAULT '',
        area TEXT NOT NULL DEFAULT '',
        equipment TEXT NOT NULL DEFAULT '',
        applicant_id TEXT NOT NULL DEFAULT '',
        owner_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        risk_level TEXT NOT NULL DEFAULT '',
        residual_risk_level TEXT NOT NULL DEFAULT '',
        control_measures TEXT NOT NULL DEFAULT '',
        designated_approver_id TEXT NOT NULL DEFAULT '',
        ehs_reviewer_id TEXT NOT NULL DEFAULT '',
        approval_due_at TEXT NOT NULL DEFAULT '',
        valid_from TEXT NOT NULL DEFAULT '',
        valid_to TEXT NOT NULL DEFAULT '',
        handback_note TEXT NOT NULL DEFAULT '',
        closure_note TEXT NOT NULL DEFAULT '',
        closed_by TEXT NOT NULL DEFAULT '',
        closed_at TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 1,
        data_label TEXT NOT NULL DEFAULT '',
        is_demo INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS permit_chemicals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        permit_id TEXT NOT NULL REFERENCES permits(id) ON DELETE CASCADE,
        chemical_name TEXT NOT NULL,
        aliases_json TEXT NOT NULL DEFAULT '[]',
        sds_file TEXT NOT NULL DEFAULT '',
        sds_status TEXT NOT NULL DEFAULT '',
        is_demo INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS permit_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        permit_id TEXT NOT NULL REFERENCES permits(id) ON DELETE CASCADE,
        step_no INTEGER NOT NULL DEFAULT 1,
        name TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT ''
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS permit_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        permit_id TEXT NOT NULL REFERENCES permits(id) ON DELETE CASCADE,
        track TEXT NOT NULL CHECK (track IN ('public_sources', 'sds')),
        evidence_id TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        page INTEGER NOT NULL DEFAULT 0,
        sections TEXT NOT NULL DEFAULT '',
        snippet TEXT NOT NULL DEFAULT '',
        topic TEXT NOT NULL DEFAULT '',
        organization TEXT NOT NULL DEFAULT '',
        source_title TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '',
        captured_at TEXT NOT NULL DEFAULT '',
        data_label TEXT NOT NULL DEFAULT '',
        is_demo INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS permit_jsa_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        permit_id TEXT NOT NULL REFERENCES permits(id) ON DELETE CASCADE,
        step_no INTEGER NOT NULL DEFAULT 1,
        work_step TEXT NOT NULL DEFAULT '',
        hazard TEXT NOT NULL DEFAULT '',
        consequence TEXT NOT NULL DEFAULT '',
        likelihood INTEGER,
        severity INTEGER,
        risk_score INTEGER,
        risk_level TEXT NOT NULL DEFAULT '',
        existing_controls TEXT NOT NULL DEFAULT '',
        proposed_controls TEXT NOT NULL DEFAULT '',
        residual_likelihood INTEGER,
        residual_severity INTEGER,
        residual_risk_score INTEGER,
        residual_risk_level TEXT NOT NULL DEFAULT '',
        confirmed_by TEXT NOT NULL DEFAULT '',
        confirmed_at TEXT NOT NULL DEFAULT '',
        is_demo INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS prestart_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        permit_id TEXT NOT NULL REFERENCES permits(id) ON DELETE CASCADE,
        item_code TEXT NOT NULL,
        item_text TEXT NOT NULL DEFAULT '',
        required INTEGER NOT NULL DEFAULT 1,
        result TEXT NOT NULL DEFAULT 'pending',
        checked_by TEXT NOT NULL DEFAULT '',
        checked_at TEXT NOT NULL DEFAULT '',
        evidence_note TEXT NOT NULL DEFAULT ''
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS hazards (
        id TEXT PRIMARY KEY,
        permit_id TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        hazard_type TEXT NOT NULL DEFAULT '其他',
        risk_level TEXT NOT NULL DEFAULT '中',
        reported_by_id TEXT NOT NULL DEFAULT '',
        owner_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        due_at TEXT NOT NULL DEFAULT '',
        verification_due_at TEXT NOT NULL DEFAULT '',
        verification_result TEXT NOT NULL DEFAULT '',
        verification_notes TEXT NOT NULL DEFAULT '',
        verifier_id TEXT NOT NULL DEFAULT '',
        verified_at TEXT NOT NULL DEFAULT '',
        reopened_reason TEXT NOT NULL DEFAULT '',
        closed_by TEXT NOT NULL DEFAULT '',
        closed_at TEXT NOT NULL DEFAULT '',
        data_label TEXT NOT NULL DEFAULT '',
        is_demo INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS corrective_actions (
        id TEXT PRIMARY KEY,
        hazard_id TEXT NOT NULL REFERENCES hazards(id) ON DELETE CASCADE,
        action_text TEXT NOT NULL DEFAULT '',
        owner_id TEXT NOT NULL DEFAULT '',
        due_at TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'planned',
        completed_at TEXT NOT NULL DEFAULT ''
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS hazard_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hazard_id TEXT NOT NULL REFERENCES hazards(id) ON DELETE CASCADE,
        file_name TEXT NOT NULL,
        evidence_type TEXT NOT NULL DEFAULT '其他',
        note TEXT NOT NULL DEFAULT '',
        uploaded_by TEXT NOT NULL DEFAULT '',
        uploaded_at TEXT NOT NULL DEFAULT '',
        is_demo INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        event_id TEXT PRIMARY KEY,
        correlation_id TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT '',
        from_state TEXT NOT NULL DEFAULT '',
        to_state TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        before_json TEXT NOT NULL DEFAULT '',
        after_json TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS work_drafts (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        work_type TEXT NOT NULL DEFAULT '其他高风险作业',
        description TEXT NOT NULL DEFAULT '',
        location TEXT NOT NULL DEFAULT '',
        planned_start TEXT NOT NULL DEFAULT '',
        planned_end TEXT NOT NULL DEFAULT '',
        people_count INTEGER NOT NULL DEFAULT 0,
        responsible_person TEXT NOT NULL DEFAULT '',
        contractor_involved INTEGER NOT NULL DEFAULT 0,
        work_steps_json TEXT NOT NULL DEFAULT '[]',
        chemicals_json TEXT NOT NULL DEFAULT '[]',
        user_risk_tags_json TEXT NOT NULL DEFAULT '[]',
        ai_risk_tags_json TEXT NOT NULL DEFAULT '[]',
        confirmed_risk_tags_json TEXT NOT NULL DEFAULT '[]',
        documents_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'draft',
        created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS safety_review_packs (
        pack_id TEXT NOT NULL,
        draft_id TEXT NOT NULL REFERENCES work_drafts(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        confirmed_by TEXT NOT NULL DEFAULT '',
        confirmed_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (pack_id, version)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS permit_sources (
        permit_id TEXT PRIMARY KEY REFERENCES permits(id) ON DELETE CASCADE,
        draft_id TEXT NOT NULL,
        pack_id TEXT NOT NULL,
        pack_version INTEGER NOT NULL,
        snapshot_json TEXT NOT NULL DEFAULT '{}',
        confirmed_by TEXT NOT NULL DEFAULT '',
        confirmed_at TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events (entity_type, entity_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_audit_correlation ON audit_events (correlation_id);",
    "CREATE INDEX IF NOT EXISTS idx_permits_status ON permits (status);",
    "CREATE INDEX IF NOT EXISTS idx_permits_owner ON permits (owner_id);",
    "CREATE INDEX IF NOT EXISTS idx_permits_approver ON permits (designated_approver_id);",
    "CREATE INDEX IF NOT EXISTS idx_hazards_permit ON hazards (permit_id, status);",
    "CREATE INDEX IF NOT EXISTS idx_hazards_owner ON hazards (owner_id, status);",
    "CREATE INDEX IF NOT EXISTS idx_work_drafts_status ON work_drafts (status, updated_at);",
    "CREATE INDEX IF NOT EXISTS idx_review_packs_draft ON safety_review_packs (draft_id, version);",
    "CREATE INDEX IF NOT EXISTS idx_permit_sources_draft ON permit_sources (draft_id);",
)

EXPECTED_TABLES: tuple[str, ...] = (
    "schema_meta",
    "users",
    "permits",
    "permit_chemicals",
    "permit_steps",
    "permit_evidence",
    "permit_jsa_items",
    "prestart_checks",
    "hazards",
    "corrective_actions",
    "hazard_evidence",
    "audit_events",
    "work_drafts",
    "safety_review_packs",
    "permit_sources",
)


_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("permits", "ehs_reviewer_id", "TEXT NOT NULL DEFAULT ''"),
    ("permits", "approval_due_at", "TEXT NOT NULL DEFAULT ''"),
)


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    existing = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def initialize(connection: sqlite3.Connection) -> sqlite3.Connection:
    """Create every table if missing, apply additive columns and record the version."""
    connection.executescript("\n".join(DDL_STATEMENTS))
    for table, column, definition in _COLUMN_MIGRATIONS:
        _ensure_column(connection, table, column, definition)
    connection.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    connection.commit()
    return connection


def schema_version(connection: sqlite3.Connection) -> str:
    """Return the recorded schema version, or an empty string when unset."""
    cursor = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    )
    row = cursor.fetchone()
    if row is None:
        return ""
    return str(row[0] if not isinstance(row, sqlite3.Row) else row["value"])


def table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Return the existing user table names in a stable order."""
    cursor = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    names = sorted(
        str(row[0] if not isinstance(row, sqlite3.Row) else row["name"])
        for row in cursor.fetchall()
    )
    return tuple(names)


__all__ = [
    "DDL_STATEMENTS",
    "EXPECTED_TABLES",
    "SCHEMA_VERSION",
    "initialize",
    "schema_version",
    "table_names",
]

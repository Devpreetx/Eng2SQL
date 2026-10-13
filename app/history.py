"""
Persistent query history.

Every query — AI-generated or manual (SQL Workspace) — is recorded here
with enough information to inspect or reproduce what happened. Backed by
a dedicated SQLite file (data/history.sqlite), separate from any user
database, so history survives independently of which databases are
currently loaded in memory.

This module owns ONLY history storage/retrieval. It never validates or
executes SQL itself — main.py calls insert_history()/update_history()
at the same points it already calls db_utils, using data it already has.

Lifecycle for a single query:
  - Immediate (SELECT, rejected, error): ONE insert_history() call with
    the final status. Nothing to update later.
  - Confirmation-based (INSERT/UPDATE/DELETE/CREATE): ONE insert_history()
    call with status="confirmation_required" when the preview is
    returned, then ONE update_history() call when /confirm resolves it
    (status becomes "executed", "cancelled", or "error"). This avoids
    the duplicate-entry problem described in the spec — the same
    history_id is reused, not a new row.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from app.config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "history.sqlite")

VALID_STATUSES = {
    "executed",
    "confirmation_required",
    "cancelled",
    "rejected",
    "error",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Creates the query_history table and its indexes if they don't exist yet."""
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS query_history (
                history_id          TEXT PRIMARY KEY,
                timestamp           TEXT NOT NULL,
                session_id          TEXT,
                db_id               TEXT NOT NULL,
                database_name       TEXT,
                question            TEXT,
                standalone_question TEXT,
                sql_json            TEXT,
                status              TEXT NOT NULL,
                execution_time_ms   REAL,
                row_count           INTEGER,
                chart_type          TEXT,
                explanation         TEXT
            )
            """
        )
        # Serves the only list query: one database, newest first.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_db_time "
            "ON query_history(db_id, timestamp DESC)"
        )
        # Added after the first release: starred ("saved") queries.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(query_history)")}
        if "starred" not in columns:
            conn.execute("ALTER TABLE query_history ADD COLUMN starred INTEGER NOT NULL DEFAULT 0")
        conn.execute("DROP INDEX IF EXISTS idx_history_db_id")
        conn.execute("DROP INDEX IF EXISTS idx_history_timestamp")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_session_id ON query_history(session_id)"
        )
        conn.commit()
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_history(
    *,
    db_id: str,
    status: str,
    database_name: str | None = None,
    session_id: str | None = None,
    question: str | None = None,
    standalone_question: str | None = None,
    sql: list[str] | None = None,
    execution_time_ms: float | None = None,
    row_count: int | None = None,
    chart_type: str | None = None,
    explanation: str | None = None,
) -> str:
    """
    Inserts one history row and returns its history_id. Never stores API
    keys/secrets/passwords — callers only ever pass question/SQL/status/
    metadata, never credentials, so there is nothing to strip here.
    """
    if status not in VALID_STATUSES:
        status = "error"

    history_id = str(uuid.uuid4())

    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO query_history (
                history_id, timestamp, session_id, db_id, database_name,
                question, standalone_question, sql_json, status,
                execution_time_ms, row_count, chart_type, explanation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                history_id,
                _now_iso(),
                session_id,
                db_id,
                database_name,
                question,
                standalone_question,
                json.dumps(sql or []),
                status,
                execution_time_ms,
                row_count,
                chart_type,
                explanation,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return history_id


def update_history(
    history_id: str,
    *,
    status: str | None = None,
    execution_time_ms: float | None = None,
    row_count: int | None = None,
    chart_type: str | None = None,
    explanation: str | None = None,
) -> None:
    """
    Updates an existing row IN PLACE — used to move a
    confirmation_required entry to its final outcome (executed /
    cancelled / error) without creating a second, duplicate entry.
    Only columns explicitly passed are touched.
    """
    fields = []
    values = []

    if status is not None:
        if status not in VALID_STATUSES:
            status = "error"
        fields.append("status = ?")
        values.append(status)
    if execution_time_ms is not None:
        fields.append("execution_time_ms = ?")
        values.append(execution_time_ms)
    if row_count is not None:
        fields.append("row_count = ?")
        values.append(row_count)
    if chart_type is not None:
        fields.append("chart_type = ?")
        values.append(chart_type)
    if explanation is not None:
        fields.append("explanation = ?")
        values.append(explanation)

    if not fields:
        return

    values.append(history_id)

    conn = _connect()
    try:
        conn.execute(
            f"UPDATE query_history SET {', '.join(fields)} WHERE history_id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_summary(row: sqlite3.Row) -> dict:
    return {
        "history_id": row["history_id"],
        "timestamp": row["timestamp"],
        "session_id": row["session_id"],
        "db_id": row["db_id"],
        "database_name": row["database_name"],
        "question": row["question"],
        "standalone_question": row["standalone_question"],
        "sql": json.loads(row["sql_json"]) if row["sql_json"] else [],
        "status": row["status"],
        "execution_time_ms": row["execution_time_ms"],
        "row_count": row["row_count"],
        "chart_type": row["chart_type"],
        "explanation": row["explanation"],
        "starred": bool(row["starred"]),
    }


def list_history(
    db_id: str,
    limit: int = 20,
    offset: int = 0,
    search: str | None = None,
    status: str | None = None,
    starred: bool = False,
) -> dict:
    """
    Returns {"items": [...], "total": N} for the given database, newest
    first. `search` matches question, standalone_question, or the SQL
    text (case-insensitive substring). `status` filters to an exact
    status, or is ignored if None/"all". `starred` keeps starred rows only.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    where = ["db_id = ?"]
    params: list = [db_id]

    if status and status.lower() != "all":
        where.append("status = ?")
        params.append(status.lower())

    if starred:
        where.append("starred = 1")

    if search and search.strip():
        term = f"%{search.strip()}%"
        where.append(
            "(question LIKE ? OR standalone_question LIKE ? OR sql_json LIKE ?)"
        )
        params.extend([term, term, term])

    where_clause = " AND ".join(where)

    conn = _connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM query_history WHERE {where_clause}",
            params,
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT * FROM query_history
            WHERE {where_clause}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

        items = [_row_to_summary(r) for r in rows]
    finally:
        conn.close()

    return {"items": items, "total": total}


def get_history_detail(history_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM query_history WHERE history_id = ?",
            (history_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return _row_to_summary(row)

def set_starred(history_id: str, starred: bool) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE query_history SET starred = ? WHERE history_id = ?",
            (1 if starred else 0, history_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_for_db(db_id: str) -> None:
    """Remove every history row of a database that is being deleted."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM query_history WHERE db_id = ?", (db_id,))
        conn.commit()
    finally:
        conn.close()

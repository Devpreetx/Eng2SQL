"""
Persistent registry of known databases (data/registry.sqlite).

Maps db_id -> display name, file path and owning user, so uploaded/created
databases — and the history, conversation memory and schema index keyed
by their db_id — are still reachable after a server restart.
"""

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from app.config import DATA_DIR

REGISTRY_DB = os.path.join(DATA_DIR, "registry.sqlite")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(REGISTRY_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_registry() -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS databases (
            db_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            source_type TEXT,
            owner_id TEXT
        )""")
        # Registries created before accounts existed have no owner column.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(databases)")}
        if "owner_id" not in columns:
            conn.execute("ALTER TABLE databases ADD COLUMN owner_id TEXT")


def register_db(db_id: str, name: str, path: str, source_type: str = "sqlite",
                owner_id: str | None = None) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute(
            """INSERT INTO databases(db_id, name, path, created_at, source_type, owner_id)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(db_id) DO UPDATE SET
                 name=excluded.name,
                 path=excluded.path,
                 source_type=excluded.source_type""",
            (db_id, name, path, datetime.now(timezone.utc).isoformat(), source_type, owner_id),
        )


def rename_db(db_id: str, name: str) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("UPDATE databases SET name=? WHERE db_id=?", (name, db_id))


def claim_unowned(owner_id: str) -> None:
    """Give every database that has no owner yet to this user."""
    with closing(_conn()) as conn, conn:
        conn.execute("UPDATE databases SET owner_id=? WHERE owner_id IS NULL", (owner_id,))


def list_registered() -> list[dict]:
    """Oldest first, so the sidebar order survives a restart."""
    with closing(_conn()) as conn:
        rows = conn.execute("SELECT * FROM databases ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def unregister_db(db_id: str) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM databases WHERE db_id=?", (db_id,))

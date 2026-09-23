import os
import sqlite3
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
REGISTRY_DB = os.path.join(DATA_DIR, "registry.sqlite")
os.makedirs(DATA_DIR, exist_ok=True)

def _conn():
    conn = sqlite3.connect(REGISTRY_DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_registry():
    conn = _conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS databases (
        db_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        source_type TEXT
    )""")
    conn.commit()
    conn.close()

def register_db(db_id, name, path, source_type="sqlite"):
    conn = _conn()
    conn.execute("""INSERT INTO databases(db_id,name,path,created_at,source_type)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(db_id) DO UPDATE SET
                      name=excluded.name,
                      path=excluded.path,
                      source_type=excluded.source_type""",
                 (db_id, name, path, datetime.now(timezone.utc).isoformat(), source_type))
    conn.commit()
    conn.close()

def list_registered():
    conn = _conn()
    rows = conn.execute("SELECT * FROM databases ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def unregister_db(db_id):
    conn = _conn()
    conn.execute("DELETE FROM databases WHERE db_id=?", (db_id,))
    conn.commit()
    conn.close()

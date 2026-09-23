"""
DB utilities: schema extraction, SQL validation, dry-run previews,
safe execution, and backups.

Supports MULTIPLE statements per request (e.g. "create a table and
insert some rows"). All statements in a request are validated together,
then executed together in a single transaction — either all succeed or
all roll back. One backup is taken per confirmed batch, not per statement.
"""

import os
import shutil
import sqlite3
import time

import sqlglot
from sqlglot import exp

DIALECT = "sqlite"

# SQLite system tables — exempt from the "unknown table" check since they
# aren't part of the user's schema but are legitimate to query (e.g. for
# "show me the tables" -> SELECT name FROM sqlite_master WHERE type='table').
SYSTEM_TABLES = {"sqlite_master", "sqlite_sequence", "sqlite_temp_master"}

# Statement types we ever allow the model to produce.
ALLOWED_TYPES = {
    "select": exp.Select,
    "insert": exp.Insert,
    "update": exp.Update,
    "delete": exp.Delete,
    "create": exp.Create,
}

# Anything containing these is rejected outright, regardless of statement type.
# NOTE: CREATE is intentionally NOT in this list — CREATE TABLE is allowed.
BLOCKED_KEYWORDS = [
    "DROP", "TRUNCATE", "ALTER", "ATTACH", "DETACH", "PRAGMA",
    "VACUUM", "GRANT", "REVOKE", "REPLACE INTO",
]


# --------------------------------------------------------------------------
# Connection helpers
# --------------------------------------------------------------------------

def get_connection(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# --------------------------------------------------------------------------
# Schema extraction
# --------------------------------------------------------------------------

def extract_schema(path: str, sample_rows: int = 3) -> dict:
    """Introspect a SQLite file and return tables, columns, FKs, and sample rows."""
    conn = get_connection(path)
    schema = {}
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()

        for t in tables:
            table_name = t["name"]
            cols = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            fks = conn.execute(f'PRAGMA foreign_key_list("{table_name}")').fetchall()
            try:
                samples = conn.execute(
                    f'SELECT * FROM "{table_name}" LIMIT {sample_rows}'
                ).fetchall()
            except sqlite3.Error:
                samples = []

            schema[table_name] = {
                "columns": [
                    {"name": c["name"], "type": c["type"], "primary_key": bool(c["pk"])}
                    for c in cols
                ],
                "foreign_keys": [
                    {"column": fk["from"], "ref_table": fk["table"], "ref_column": fk["to"]}
                    for fk in fks
                ],
                "sample_rows": [dict(r) for r in samples],
            }
    finally:
        conn.close()
    return schema


# --------------------------------------------------------------------------
# Validation — single statement
# --------------------------------------------------------------------------

def validate_sql(sql: str):
    """
    Parse and validate a single SQL statement.
    Returns (parsed_expression, statement_type) or raises ValueError.
    """
    sql_stripped = sql.strip().rstrip(";").strip()
    if not sql_stripped:
        raise ValueError("Empty SQL statement.")

    upper = f" {sql_stripped.upper()} "

    for kw in BLOCKED_KEYWORDS:
        if f" {kw} " in upper or upper.strip().startswith(kw):
            raise ValueError(f"Statement type '{kw}' is not permitted for safety reasons.")

    try:
        parsed = sqlglot.parse(sql_stripped, read=DIALECT)
    except Exception as e:
        raise ValueError(f"Generated SQL failed to parse: {e}")

    parsed = [p for p in parsed if p is not None]
    if len(parsed) != 1:
        raise ValueError(
            f"Expected exactly one statement in '{sql_stripped[:60]}...', "
            f"found {len(parsed)}. Split into separate list entries."
        )

    expr = parsed[0]

    stmt_type = None
    for name, cls in ALLOWED_TYPES.items():
        if isinstance(expr, cls):
            stmt_type = name
            break
    if stmt_type is None:
        raise ValueError(f"Statement type '{type(expr).__name__}' is not permitted.")

    if stmt_type in ("update", "delete"):
        where = expr.args.get("where")
        if where is None:
            raise ValueError(
                f"{stmt_type.upper()} without a WHERE clause is blocked for safety. "
                "Be specific about which rows to change."
            )

    return expr, stmt_type


def _referenced_tables(expr) -> set:
    return {t.name for t in expr.find_all(exp.Table) if t.name}


def _created_table_name(expr) -> str | None:
    """Best-effort extraction of the table name from a CREATE TABLE statement."""
    table = expr.find(exp.Table)
    return table.name if table else None


# --------------------------------------------------------------------------
# Validation — a batch of statements (multi-statement support)
# --------------------------------------------------------------------------

def validate_statements(sql_list: list[str], known_tables: set) -> list[tuple]:
    """
    Validates each statement in order. Tracks tables created earlier in the
    SAME batch (e.g. CREATE TABLE foo; INSERT INTO foo ...) so later
    statements referencing them aren't wrongly rejected as "unknown table".

    Returns a list of (sql, expr, stmt_type) tuples, in order.
    Raises ValueError on the first problem found (nothing gets executed
    unless the WHOLE batch validates).
    """
    if not sql_list:
        raise ValueError("No SQL statements to validate.")

    tables_in_scope = set(known_tables)
    validated = []

    for i, sql in enumerate(sql_list, start=1):
        try:
            expr, stmt_type = validate_sql(sql)
        except ValueError as e:
            raise ValueError(f"Statement {i} rejected: {e}")

        if stmt_type == "create":
            new_table = _created_table_name(expr)
            if new_table:
                tables_in_scope.add(new_table)
        else:
            refs = _referenced_tables(expr)
            unknown = refs - tables_in_scope - SYSTEM_TABLES
            if unknown:
                raise ValueError(
                    f"Statement {i} references unknown table(s) {sorted(unknown)}: {sql}"
                )

        validated.append((sql.strip().rstrip(";").strip(), expr, stmt_type))

    return validated


def build_preview_sql(expr, stmt_type: str):
    """
    For UPDATE/DELETE, build a SELECT that shows which rows would be affected.
    Returns (count_sql, sample_sql) or (None, None) for statement types we
    can't (or don't need to) safely preview (INSERT, CREATE).
    """
    if stmt_type not in ("update", "delete"):
        return None, None

    table = expr.this
    where = expr.args.get("where")
    if table is None or where is None:
        return None, None

    try:
        table_sql = table.sql(dialect=DIALECT)
        where_sql = where.this.sql(dialect=DIALECT)
    except Exception:
        return None, None

    count_sql = f"SELECT COUNT(*) AS affected_count FROM {table_sql} WHERE {where_sql}"
    sample_sql = f"SELECT * FROM {table_sql} WHERE {where_sql} LIMIT 5"
    return count_sql, sample_sql


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def run_select(path: str, sql: str, limit: int = 200) -> dict:
    limit = min(max(int(limit), 1), int(os.getenv("MAX_SQL_RESULT_ROWS", "5000")))
    conn = get_connection(path)
    try:
        timeout_ms = int(os.getenv("SQL_TIMEOUT_MS", "5000"))
        started = time.monotonic()
        conn.set_progress_handler(lambda: 1 if (time.monotonic() - started) * 1000 > timeout_ms else 0, 10000)
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(limit)
        return {"columns": cols, "rows": [dict(r) for r in rows]}
    finally:
        conn.close()


def run_write(path: str, sql: str) -> int:
    """Execute a single write inside its own transaction (used for one-off writes)."""
    conn = get_connection(path)
    try:
        cur = conn.execute(sql)
        conn.commit()
        return cur.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_statements(path: str, sql_list: list[str]) -> list[dict]:
    """
    Executes a batch of statements in ONE transaction. All succeed or all
    roll back. Returns per-statement results (rowcount, or rows for a
    SELECT mixed into the batch).
    """
    conn = get_connection(path)
    results = []
    try:
        timeout_ms = int(os.getenv("SQL_TIMEOUT_MS", "5000"))
        started = time.monotonic()
        conn.set_progress_handler(lambda: 1 if (time.monotonic() - started) * 1000 > timeout_ms else 0, 10000)
        for sql in sql_list:
            cur = conn.execute(sql)
            if sql.strip().upper().startswith("SELECT"):
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchall()
                results.append({"sql": sql, "columns": cols, "rows": [dict(r) for r in rows]})
            else:
                results.append({"sql": sql, "rowcount": cur.rowcount})
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise RuntimeError(f"Batch execution failed, all statements rolled back: {e}")
    finally:
        conn.close()

    return results


def backup_db(path: str, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"{os.path.basename(path)}.{ts}.bak")
    shutil.copy2(path, backup_path)
    return backup_path


def restore_db(path: str, backup_path: str) -> None:
    shutil.copy2(backup_path, path)
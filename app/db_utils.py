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
from sqlglot.tokens import TokenType

from app.config import MAX_BACKUPS_PER_DB, MAX_SQL_RESULT_ROWS, SQL_TIMEOUT_MS

DIALECT = "sqlite"

# Rows returned to the UI by default; MAX_SQL_RESULT_ROWS is the hard ceiling.
DEFAULT_RESULT_ROWS = 200

# SQLite system tables — exempt from the "unknown table" check since they
# aren't part of the user's schema but are legitimate to query (e.g. for
# "show me the tables" -> SELECT name FROM sqlite_master WHERE type='table').
SYSTEM_TABLES = {"sqlite_master", "sqlite_schema", "sqlite_sequence", "sqlite_temp_master"}

# Statement types we ever allow. A UNION/INTERSECT/EXCEPT of SELECTs is
# still a pure read, so it is classified as "select".
ALLOWED_TYPES = {
    "select": (exp.Select, exp.SetOperation),
    "insert": exp.Insert,
    "update": exp.Update,
    "delete": exp.Delete,
    "create": exp.Create,
}

# CREATE TRIGGER could smuggle arbitrary writes past the WHERE-clause guard.
ALLOWED_CREATE_KINDS = {"TABLE", "VIEW", "INDEX"}

# Leading keywords rejected outright. The type whitelist above already
# blocks these; this list only exists to give a clear error message.
# NOTE: CREATE is intentionally NOT in this list — CREATE TABLE is allowed.
BLOCKED_KEYWORDS = {
    "DROP", "TRUNCATE", "ALTER", "ATTACH", "DETACH", "PRAGMA",
    "VACUUM", "GRANT", "REVOKE", "REPLACE",
}


def quote_ident(name: str) -> str:
    """Quote an identifier for SQLite, escaping embedded double quotes."""
    return '"' + str(name).replace('"', '""') + '"'


# --------------------------------------------------------------------------
# Connection helpers
# --------------------------------------------------------------------------

def get_connection(path: str, read_only: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Uploaded files are untrusted: don't let their views/triggers call
    # side-effecting SQL functions.
    conn.execute("PRAGMA trusted_schema = OFF")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    return conn


def _arm_timeout(conn: sqlite3.Connection) -> None:
    """Abort any statement on this connection that runs past SQL_TIMEOUT_MS."""
    started = time.monotonic()
    conn.set_progress_handler(
        lambda: 1 if (time.monotonic() - started) * 1000 > SQL_TIMEOUT_MS else 0,
        10000,
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    """BLOBs can't be serialised to JSON; show a placeholder instead."""
    return {
        key: f"<BLOB {len(value)} bytes>" if isinstance(value, (bytes, memoryview)) else value
        for key, value in zip(row.keys(), row)
    }


# --------------------------------------------------------------------------
# Schema extraction
# --------------------------------------------------------------------------

def extract_schema(path: str, sample_rows: int = 3) -> dict:
    """Introspect a SQLite file and return tables, columns, FKs, and sample rows."""
    conn = get_connection(path, read_only=True)
    schema = {}
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
        ).fetchall()

        for t in tables:
            table_name = t["name"]
            quoted = quote_ident(table_name)
            cols = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
            fks = conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
            try:
                samples = conn.execute(
                    f"SELECT * FROM {quoted} LIMIT ?", (int(sample_rows),)
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
                "sample_rows": [_row_to_dict(r) for r in samples],
            }
    finally:
        conn.close()
    return schema


# --------------------------------------------------------------------------
# Statement splitting
# --------------------------------------------------------------------------

def split_statements(sql_text: str) -> list[str]:
    """
    Split SQL text into individual statements on ';' boundaries, ignoring
    semicolons inside string literals, quoted identifiers and comments.
    Falls back to a plain split if the text can't even be tokenised —
    validate_sql() will then report the real syntax error.
    """
    sql_text = sql_text or ""
    try:
        tokens = sqlglot.tokenize(sql_text, read=DIALECT)
    except Exception:
        return [part.strip() for part in sql_text.split(";") if part.strip()]

    statements = []
    start = 0
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            statements.append(sql_text[start:token.start])
            start = token.end + 1
    statements.append(sql_text[start:])

    return [s.strip() for s in statements if s.strip()]


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

    try:
        tokens = sqlglot.tokenize(sql_stripped, read=DIALECT)
    except Exception as e:
        raise ValueError(f"Generated SQL failed to parse: {e}")

    leading = tokens[0].text.upper() if tokens else ""
    if leading in BLOCKED_KEYWORDS:
        raise ValueError(f"Statement type '{leading}' is not permitted for safety reasons.")

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

    if stmt_type == "create":
        kind = str(expr.args.get("kind") or "").upper()
        if kind not in ALLOWED_CREATE_KINDS:
            raise ValueError(f"CREATE {kind or 'statement'} is not permitted.")

    if stmt_type in ("update", "delete"):
        where = expr.args.get("where")
        if where is None:
            raise ValueError(
                f"{stmt_type.upper()} without a WHERE clause is blocked for safety. "
                "Be specific about which rows to change."
            )

    return expr, stmt_type


def _referenced_tables(expr) -> set:
    """Real tables a statement touches — names defined by its own CTEs excluded."""
    cte_names = {cte.alias for cte in expr.find_all(exp.CTE)}
    return {t.name for t in expr.find_all(exp.Table) if t.name} - cte_names


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
            unknown = _referenced_tables(expr) - tables_in_scope - SYSTEM_TABLES
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


def build_previews(path: str, validated: list[tuple]) -> list[dict]:
    """
    Dry-run preview for each statement of a write batch: how many rows an
    UPDATE/DELETE would touch plus a small sample. Preview failures are
    never fatal — the statement is simply shown without an impact figure.
    """
    previews = []
    for sql, expr, stmt_type in validated:
        preview = {"sql": sql, "type": stmt_type, "affected_count": None, "sample_rows": None}
        try:
            count_sql, sample_sql = build_preview_sql(expr, stmt_type)
            if count_sql:
                count_data = run_select(path, count_sql)
                sample_data = run_select(path, sample_sql)
                if count_data["rows"]:
                    preview["affected_count"] = count_data["rows"][0].get("affected_count")
                preview["sample_rows"] = sample_data["rows"]
        except Exception:
            pass
        previews.append(preview)
    return previews


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def run_select(
    path: str,
    sql: str,
    limit: int = DEFAULT_RESULT_ROWS,
    max_rows: int = MAX_SQL_RESULT_ROWS,
) -> dict:
    """
    Run a read on a query-only connection. At most `limit` rows are
    returned; "truncated" says whether the query had more.
    """
    limit = min(max(int(limit), 1), max_rows)
    conn = get_connection(path, read_only=True)
    try:
        _arm_timeout(conn)
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(limit + 1)
        return {
            "columns": cols,
            "rows": [_row_to_dict(r) for r in rows[:limit]],
            "truncated": len(rows) > limit,
        }
    finally:
        conn.close()


def fetch_table_page(path: str, table_name: str, limit: int, offset: int) -> dict:
    """One page of a table plus its total row count, for the table preview."""
    quoted = quote_ident(table_name)
    conn = get_connection(path, read_only=True)
    try:
        cur = conn.execute(f"SELECT * FROM {quoted} LIMIT ? OFFSET ?", (limit, offset))
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = [_row_to_dict(r) for r in cur.fetchall()]
        total = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        return {"columns": columns, "rows": rows, "total_rows": total}
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
        _arm_timeout(conn)
        for sql in sql_list:
            cur = conn.execute(sql)
            if cur.description:
                cols = [d[0] for d in cur.description]
                rows = cur.fetchmany(MAX_SQL_RESULT_ROWS)
                results.append({"sql": sql, "columns": cols, "rows": [_row_to_dict(r) for r in rows]})
            else:
                results.append({"sql": sql, "rowcount": cur.rowcount})
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise RuntimeError(f"Batch execution failed, all statements rolled back: {e}")
    finally:
        conn.close()

    return results


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def format_sql(sql_text: str) -> str:
    """Pretty-print one or more statements. Raises ValueError if unparseable."""
    statements = split_statements(sql_text)
    if not statements:
        raise ValueError("Nothing to format.")
    try:
        return ";\n\n".join(
            sqlglot.parse_one(stmt, read=DIALECT).sql(dialect=DIALECT, pretty=True)
            for stmt in statements
        ) + ";"
    except Exception as e:
        raise ValueError(f"Could not format this SQL: {e}")


# --------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------
#
# A backup is a copy of the database file named
# "<db file name>.<YYYYmmdd_HHMMSS_mmm>.bak" inside backup_dir.

def _backup_prefix(path: str) -> str:
    return f"{os.path.basename(path)}."


def _backup_names(path: str, backup_dir: str) -> list[str]:
    """This database's backups, newest first (the timestamp sorts by name)."""
    prefix = _backup_prefix(path)
    try:
        names = os.listdir(backup_dir)
    except FileNotFoundError:
        return []
    return sorted(
        (n for n in names if n.startswith(prefix) and n.endswith(".bak")), reverse=True
    )


def backup_db(path: str, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    now = time.time()
    stamp = f"{time.strftime('%Y%m%d_%H%M%S', time.localtime(now))}_{int(now * 1000) % 1000:03d}"
    backup_path = os.path.join(backup_dir, f"{_backup_prefix(path)}{stamp}.bak")
    shutil.copy2(path, backup_path)

    if MAX_BACKUPS_PER_DB > 0:
        for old in _backup_names(path, backup_dir)[MAX_BACKUPS_PER_DB:]:
            try:
                os.remove(os.path.join(backup_dir, old))
            except OSError:
                pass

    return backup_path


def list_backups(path: str, backup_dir: str) -> list[dict]:
    prefix = _backup_prefix(path)
    backups = []
    for name in _backup_names(path, backup_dir):
        stamp = name[len(prefix):-len(".bak")]
        try:
            created = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.strptime(stamp[:15], "%Y%m%d_%H%M%S")
            )
        except ValueError:
            created = None
        backups.append({
            "name": name,
            "created": created,
            "size": os.path.getsize(os.path.join(backup_dir, name)),
        })
    return backups


def restore_backup(path: str, backup_dir: str, name: str) -> str:
    """
    Put one of this database's own backups back in place. The current
    state is backed up first (so a restore can be undone); returns that
    safety backup's path.
    """
    # Only an exact name from this database's backup list is accepted —
    # never a caller-supplied path.
    if name not in _backup_names(path, backup_dir):
        raise ValueError("Unknown backup for this database.")

    # Stage the chosen backup before taking the safety backup: taking a
    # backup prunes the oldest one, which may be the one being restored.
    staged = f"{path}.restore"
    shutil.copy2(os.path.join(backup_dir, name), staged)
    try:
        safety_backup = backup_db(path, backup_dir)
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.remove(staged)
    return safety_backup


def delete_backups(path: str, backup_dir: str) -> None:
    for name in _backup_names(path, backup_dir):
        try:
            os.remove(os.path.join(backup_dir, name))
        except OSError:
            pass

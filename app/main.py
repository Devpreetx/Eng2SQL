"""
Eng2SQL Agent — FastAPI backend.

Flow (via LangGraph agent):
  1. POST /auth/signup | /auth/signin -> start a session (app/auth.py)
  2. POST /upload        -> store a SQLite/CSV/XLSX file, extract schema
  3. POST /query         -> runs the LangGraph agent. Pure reads execute
                             immediately. Any batch containing a
                             write/create returns a preview and a
                             query_id, and waits for confirmation.
                             Accepts an optional session_id for
                             conversation memory.
  4. POST /confirm       -> execute (or cancel) a pending batch of
                             statements as ONE transaction, with an
                             automatic backup taken first.
  5. POST /sql/execute   -> SQL WORKSPACE. Manual SQL, same validation/
                             execution/confirmation pipeline as /query.
  6. GET  /history/{db_id}, /history/detail/{id} -> query history.
  7. /export/*, /import/table, /download/{id}    -> data in and out.
  8. /backups/{db_id}    -> list backups, restore one (undo).

Every route except /health and /auth/* requires a signed-in user, and a
database is only ever visible to the user who owns it.

Every query — AI-generated or manual — is persisted by app/history.py.
A confirmation-based write records ONE history row when the preview is
shown (status="confirmation_required") and that SAME row is updated in
place when /confirm resolves it — never a duplicate second row.

This file only ORCHESTRATES auth.py/rag.py/llm.py/db_utils.py/memory.py/
history.py — no SQL generation, validation, execution or storage logic
lives here.

Endpoints are plain `def` on purpose: they do blocking work (SQLite,
pandas, LLM calls), so FastAPI runs them on its worker thread pool
instead of stalling the event loop for every other request.
"""

import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders

from app import auth, config, data_tools, history, memory, rag, registry
from app.agent import run_agent
from app.auth import current_user
from app.db_utils import (
    backup_db,
    build_previews,
    delete_backups,
    extract_schema,
    fetch_table_page,
    format_sql,
    list_backups,
    quote_ident,
    restore_backup,
    run_select,
    run_statements,
    split_statements,
    validate_statements,
)

config.configure_logging()
logger = logging.getLogger("main")

DB_UPLOAD_EXTENSIONS = (".db", ".sqlite", ".sqlite3")
TABULAR_EXTENSIONS = (".csv", ".xlsx")
SQLITE_MAGIC = b"SQLite format 3\x00"
COLUMN_TYPES = {"TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"}
MAX_DB_NAME_LENGTH = 80

# Unconfirmed writes kept per database; the oldest is cancelled beyond this.
MAX_PENDING_PER_DB = 50

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)

# db_id -> {"path", "schema", "name", "owner", "pending"}. Rebuilt from the
# registry on startup; "pending" (unconfirmed writes) is in-memory only.
DBS: dict = {}


def _db_path(db_id: str) -> str:
    return os.path.join(config.UPLOAD_DIR, f"{db_id}.sqlite")


def _load_registered_databases() -> None:
    for row in registry.list_registered():
        db_id = row["db_id"]
        path = _db_path(db_id)
        if not os.path.isfile(path):
            registry.unregister_db(db_id)
            continue
        try:
            schema = extract_schema(path)
        except Exception as e:
            logger.warning(f"Skipping unreadable database {db_id}: {e}")
            continue
        DBS[db_id] = {
            "path": path,
            "schema": schema,
            "name": row["name"],
            "owner": row.get("owner_id"),
            "pending": {},
        }


def _claim_unowned_databases(user: dict) -> None:
    """Databases from before sign-in existed go to the first account."""
    registry.claim_unowned(user["id"])
    for db in list(DBS.values()):
        if db.get("owner") is None:
            db["owner"] = user["id"]


auth.on_first_account.append(_claim_unowned_databases)


@asynccontextmanager
async def lifespan(_: FastAPI):
    auth.init_db()
    history.init_db()
    registry.init_registry()
    _load_registered_databases()
    yield


app = FastAPI(title="Eng2SQL Agent", lifespan=lifespan)


class SecurityHeadersMiddleware:
    """Adds hardening headers; tells browsers to revalidate cached assets."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # The interactive API docs load their own inline scripts.
        is_docs = scope["path"] in ("/docs", "/redoc", "/openapi.json")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Cache-Control", "no-cache")
                if not is_docs:
                    headers.setdefault("X-Frame-Options", "DENY")
                    headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)

if config.ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.ALLOWED_ORIGINS,
        # Browsers refuse credentials with a wildcard origin.
        allow_credentials="*" not in config.ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth.router)


class QueryRequest(BaseModel):
    db_id: str
    question: str
    session_id: str | None = None


class ConfirmRequest(BaseModel):
    db_id: str
    query_id: str
    confirm: bool


class RagSearchRequest(BaseModel):
    db_id: str
    question: str


class SqlExecuteRequest(BaseModel):
    db_id: str
    sql: str


class SqlFormatRequest(BaseModel):
    sql: str


class QueryExportRequest(BaseModel):
    db_id: str
    sql: str
    format: str = "csv"


class ColumnSpec(BaseModel):
    name: str
    type: str = "TEXT"


class CreateTableRequest(BaseModel):
    db_id: str
    table_name: str
    columns: list[ColumnSpec]


class RenameRequest(BaseModel):
    name: str


class RestoreRequest(BaseModel):
    name: str


class StarRequest(BaseModel):
    starred: bool


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _get_db_or_404(db_id: str, user: dict) -> dict:
    """
    The database, if it exists AND belongs to this user. Someone else's
    database answers exactly like a missing one, so ids can't be probed.
    """
    db = DBS.get(db_id)
    if not db or db.get("owner") != user["id"]:
        raise HTTPException(404, "Unknown db_id. Upload a database first.")
    return db


def _get_table_or_404(db: dict, table_name: str) -> None:
    if table_name not in db["schema"]:
        raise HTTPException(404, "Table not found.")


def _public_db_summary(db_id: str, db: dict) -> dict:
    return {
        "db_id": db_id,
        "name": db.get("name", "untitled"),
        "tables": list(db.get("schema", {}).keys()),
    }


def _safe_index_schema(db_id: str, schema: dict) -> None:
    try:
        rag.index_schema(db_id, schema)
    except Exception as e:
        logger.error(f"RAG indexing failed for db_id={db_id}: {e}")


def _add_database(db_id: str, name: str, path: str, schema: dict, source_type: str,
                  user: dict, background_tasks: BackgroundTasks) -> None:
    DBS[db_id] = {"path": path, "schema": schema, "name": name, "owner": user["id"], "pending": {}}
    registry.register_db(db_id, name, path, source_type, owner_id=user["id"])
    background_tasks.add_task(_safe_index_schema, db_id, schema)


def _refresh_schema(db_id: str, db: dict, background_tasks: BackgroundTasks) -> dict:
    """Re-read the schema after a change and re-index it off the request path."""
    db["schema"] = extract_schema(db["path"])
    background_tasks.add_task(_safe_index_schema, db_id, db["schema"])
    return db["schema"]


def _remove_file(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        logger.warning(f"Could not remove {path}: {e}")


def _save_upload(file: UploadFile, dest: str) -> None:
    """Stream an upload to disk, refusing anything over MAX_UPLOAD_MB."""
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    with open(dest, "wb") as out:
        while chunk := file.file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise HTTPException(
                    413, f"File is larger than the {config.MAX_UPLOAD_MB} MB upload limit."
                )
            out.write(chunk)


def _backup(db: dict) -> str:
    """Take a backup; returns its project-relative label (never an absolute path)."""
    return f"backups/{os.path.basename(backup_db(db['path'], config.BACKUP_DIR))}"


def _record(db_id: str, db: dict, status: str, **fields) -> str | None:
    """Write one history row. History is best-effort: it never fails a query."""
    try:
        return history.insert_history(
            db_id=db_id, database_name=db.get("name"), status=status, **fields
        )
    except Exception as e:
        logger.error(f"Could not record history for db_id={db_id}: {e}")
        return None


def _update_record(history_id: str | None, **fields) -> None:
    if not history_id:
        return
    try:
        # Same row created when the preview was first shown — updated in
        # place, not duplicated.
        history.update_history(history_id, **fields)
    except Exception as e:
        logger.error(f"Could not update history {history_id}: {e}")


def _add_pending(db: dict, statements: list[str], history_id: str | None) -> str:
    pending = db["pending"]
    while len(pending) >= MAX_PENDING_PER_DB:
        stale = pending.pop(next(iter(pending)))
        _update_record(stale.get("history_id"), status="cancelled")

    query_id = str(uuid.uuid4())
    pending[query_id] = {"statements": statements, "history_id": history_id}
    return query_id


def _cancel_all_pending(db: dict) -> None:
    """Previews were computed against data that is about to be replaced."""
    while db["pending"]:
        _, stale = db["pending"].popitem()
        _update_record(stale.get("history_id"), status="cancelled")


def _owned_history_record(history_id: str, user: dict) -> dict:
    record = history.get_history_detail(history_id)
    if record is None:
        raise HTTPException(404, "Unknown history_id.")
    try:
        _get_db_or_404(record["db_id"], user)
    except HTTPException:
        raise HTTPException(404, "Unknown history_id.")
    return record


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _row_total(results: list[dict]) -> int:
    return sum(len(r.get("rows", [])) for r in results)


def _clean_db_name(name: str) -> str:
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    if not name:
        raise HTTPException(400, "Database name is required.")
    if len(name) > MAX_DB_NAME_LENGTH:
        raise HTTPException(400, f"Database name must be at most {MAX_DB_NAME_LENGTH} characters.")
    return name


# --------------------------------------------------------------------------
# Databases
# --------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/databases")
def list_databases(user: dict = Depends(current_user)):
    return {
        "databases": [
            _public_db_summary(db_id, db)
            for db_id, db in list(DBS.items())
            if db.get("owner") == user["id"]
        ]
    }


@app.post("/upload")
def upload_db(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    name = os.path.basename(file.filename or "")
    lower = name.lower()

    if not lower.endswith(DB_UPLOAD_EXTENSIONS + TABULAR_EXTENSIONS):
        raise HTTPException(400, "Supported files: .db, .sqlite, .sqlite3, .csv, .xlsx")

    db_id = str(uuid.uuid4())
    path = _db_path(db_id)
    staged = f"{path}.upload"

    try:
        _save_upload(file, staged)

        if lower.endswith(TABULAR_EXTENSIONS):
            data_tools.load_file_as_database(staged, name, path)
        else:
            with open(staged, "rb") as f:
                if f.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
                    raise HTTPException(400, "That file is not a SQLite database.")
            os.replace(staged, path)

        schema = extract_schema(path)
        if not schema:
            raise HTTPException(400, "No tables found in the uploaded dataset.")

    except HTTPException:
        _remove_file(path)
        raise
    except Exception as e:
        _remove_file(path)
        raise HTTPException(400, f"Could not process uploaded file: {e}")
    finally:
        _remove_file(staged)

    source_type = "sqlite" if lower.endswith(DB_UPLOAD_EXTENSIONS) else lower.rsplit(".", 1)[-1]
    name = name[:MAX_DB_NAME_LENGTH]
    _add_database(db_id, name, path, schema, source_type, user, background_tasks)

    return {
        "db_id": db_id,
        "filename": name,
        "name": name,
        "tables": list(schema.keys()),
        "schema": schema,
    }


@app.post("/database/create")
def create_database(name: str, background_tasks: BackgroundTasks, user: dict = Depends(current_user)):
    safe_name = "".join(c for c in name.strip() if c.isalnum() or c in ("_", "-"))

    if not name.strip():
        raise HTTPException(400, "Database name is required.")
    if not safe_name:
        raise HTTPException(400, "Invalid database name.")

    safe_name = safe_name[:MAX_DB_NAME_LENGTH - 3]
    if not safe_name.endswith(".db"):
        safe_name += ".db"

    db_id = str(uuid.uuid4())
    path = _db_path(db_id)

    try:
        sqlite3.connect(path).close()
    except Exception as e:
        _remove_file(path)
        raise HTTPException(500, f"Could not create database: {e}")

    _add_database(db_id, safe_name, path, {}, "created", user, background_tasks)

    return {"db_id": db_id, "name": safe_name, "tables": [], "schema": {}}


@app.patch("/database/{db_id}")
def rename_database(db_id: str, req: RenameRequest, user: dict = Depends(current_user)):
    db = _get_db_or_404(db_id, user)
    db["name"] = _clean_db_name(req.name)
    registry.rename_db(db_id, db["name"])
    return _public_db_summary(db_id, db)


@app.delete("/database/{db_id}")
def delete_database(db_id: str, user: dict = Depends(current_user)):
    """
    Permanently remove a database with everything derived from it: the
    file, its backups, its query history, conversation memory and index.
    """
    db = _get_db_or_404(db_id, user)

    DBS.pop(db_id, None)
    registry.unregister_db(db_id)
    _remove_file(db["path"])
    delete_backups(db["path"], config.BACKUP_DIR)

    for cleanup in (
        lambda: history.delete_for_db(db_id),
        lambda: memory.delete_for_db(db_id),
        lambda: rag.delete_index(db_id, db["schema"]),
    ):
        try:
            cleanup()
        except Exception as e:
            logger.warning(f"Cleanup after deleting {db_id} was incomplete: {e}")

    return {"status": "deleted", "db_id": db_id}


@app.post("/table/create")
def create_table(req: CreateTableRequest, background_tasks: BackgroundTasks,
                 user: dict = Depends(current_user)):
    db = _get_db_or_404(req.db_id, user)

    table_name = req.table_name.strip()

    if not table_name:
        raise HTTPException(400, "Table name is required.")
    if not req.columns:
        raise HTTPException(400, "At least one column is required.")
    if not table_name.replace("_", "").isalnum():
        raise HTTPException(400, "Invalid table name.")

    column_sql = []
    for column in req.columns:
        name = column.name.strip()
        data_type = column.type.upper()

        if not name:
            raise HTTPException(400, "Column name cannot be empty.")
        if not name.replace("_", "").isalnum():
            raise HTTPException(400, f"Invalid column name: {name}")
        if data_type not in COLUMN_TYPES:
            raise HTTPException(400, f"Invalid data type: {data_type}")

        column_sql.append(f"{quote_ident(name)} {data_type}")

    try:
        run_statements(
            db["path"],
            [f"CREATE TABLE {quote_ident(table_name)} ({', '.join(column_sql)})"],
        )
    except RuntimeError as e:
        raise HTTPException(400, str(e).split(": ", 1)[-1])

    schema = _refresh_schema(req.db_id, db, background_tasks)

    return {"status": "created", "table": table_name, "schema": schema}


@app.get("/schema/{db_id}")
def get_schema(db_id: str, user: dict = Depends(current_user)):
    return _get_db_or_404(db_id, user)["schema"]


@app.get("/table/{db_id}/{table_name}")
def get_table_data(db_id: str, table_name: str, page: int = 1, limit: int = 50,
                   user: dict = Depends(current_user)):
    db = _get_db_or_404(db_id, user)
    _get_table_or_404(db, table_name)

    if page < 1:
        raise HTTPException(400, "Page must be >= 1.")
    if limit < 1 or limit > 500:
        raise HTTPException(400, "Limit must be between 1 and 500.")

    data = fetch_table_page(db["path"], table_name, limit, (page - 1) * limit)

    return {"table": table_name, "page": page, "limit": limit, **data}


@app.get("/download/{db_id}")
def download_current_db(db_id: str, user: dict = Depends(current_user)):
    db = _get_db_or_404(db_id, user)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(db.get("name") or "")[0]).strip("_")
    return FileResponse(db["path"], filename=f"{stem or 'database'}.sqlite")


# --------------------------------------------------------------------------
# AI CHAT
# --------------------------------------------------------------------------

@app.post("/query")
def query(req: QueryRequest, user: dict = Depends(current_user)):
    db = _get_db_or_404(req.db_id, user)

    # Generate a session_id if the caller didn't provide one.
    session_id = req.session_id or str(uuid.uuid4())

    start = time.perf_counter()
    try:
        result = run_agent(
            db_id=req.db_id,
            question=req.question,
            schema=db["schema"],
            db_path=db["path"],
            session_id=session_id,
        )
    except Exception as e:
        logger.exception("Agent execution failed.")
        raise HTTPException(502, f"Agent execution failed: {e}")
    # NOTE: this measures the whole agent run (RAG retrieval + LLM SQL
    # generation/correction + execution + analysis), not isolated SQL
    # execution time. The SQL Workspace path below has true,
    # execution-only timing since it doesn't call an LLM.
    elapsed_ms = _elapsed_ms(start)

    # Always echo session_id back, even on early-exit paths, so the
    # frontend can persist it starting from the very first query.
    result.setdefault("session_id", session_id)

    status = result.get("status")

    if status == "needs_clarification":
        # Nothing concrete happened yet (no SQL, no outcome) — don't save
        # an incomplete entry.
        return result

    if status not in ("rejected", "error", "executed", "confirmation_required"):
        # Unknown/unexpected status — surface as an error rather than crashing.
        return {"status": "error", "error": f"Unexpected agent status: {status}", "session_id": session_id}

    statements = result.get("statements", [])
    fields = {
        "session_id": session_id,
        "question": req.question,
        "standalone_question": result.get("standalone_question"),
        "sql": statements,
        "execution_time_ms": elapsed_ms,
    }

    if status == "error":
        fields["explanation"] = result.get("error")
    elif status == "rejected":
        fields["explanation"] = result.get("reason")
    else:
        fields["explanation"] = result.get("explanation")

    if status == "executed":
        chart = result.get("chart") or {}
        fields["row_count"] = _row_total(result.get("results", []))
        fields["chart_type"] = chart.get("chart_type") if chart.get("chart_required") else None

    history_id = _record(req.db_id, db, status, **fields)

    if status == "confirmation_required":
        result = {**result, "query_id": _add_pending(db, statements, history_id)}

    return result


# --------------------------------------------------------------------------
# SQL WORKSPACE
# --------------------------------------------------------------------------

@app.post("/sql/format")
def sql_format(req: SqlFormatRequest, _: dict = Depends(current_user)):
    try:
        return {"sql": format_sql(req.sql)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/sql/execute")
def sql_execute(req: SqlExecuteRequest, user: dict = Depends(current_user)):
    db = _get_db_or_404(req.db_id, user)

    # validate_statements() (SQLGlot-backed) performs the real parsing and
    # validation of each piece, exactly as it does for AI-generated SQL.
    statements = split_statements(req.sql)
    if not statements:
        return {"status": "error", "error": "SQL statement cannot be empty."}

    try:
        validated = validate_statements(statements, set(db["schema"].keys()))
    except ValueError as e:
        _record(req.db_id, db, "rejected", sql=statements, explanation=str(e))
        return {"status": "rejected", "reason": str(e), "statements": statements}
    except Exception as e:
        logger.error(f"SQL Workspace validation crashed for db_id={req.db_id}: {e}")
        message = "SQL syntax error: could not validate the statement."
        _record(req.db_id, db, "error", sql=statements, explanation=message)
        return {"status": "error", "error": message}

    statements = [sql for sql, _, _ in validated]
    stmt_types = {stmt_type for (_, _, stmt_type) in validated}

    # ---- Pure SELECT batch: execute immediately, same as /query ----
    if stmt_types == {"select"}:
        start = time.perf_counter()
        try:
            results = [run_select(db["path"], sql) for sql in statements]
        except Exception as e:
            _record(req.db_id, db, "error", sql=statements, explanation=str(e))
            return {
                "status": "error",
                "error": f"SQL execution failed: {e}",
                "statements": statements,
            }
        elapsed_ms = _elapsed_ms(start)

        _record(
            req.db_id, db, "executed",
            sql=statements, execution_time_ms=elapsed_ms, row_count=_row_total(results),
        )

        return {
            "status": "executed",
            "statements": statements,
            "results": results,
            "execution_time_ms": elapsed_ms,
        }

    # ---- Write/create batch: build previews, defer to /confirm ----
    previews = build_previews(db["path"], validated)
    history_id = _record(req.db_id, db, "confirmation_required", sql=statements)

    return {
        "status": "confirmation_required",
        "query_id": _add_pending(db, statements, history_id),
        "statements": statements,
        "previews": previews,
    }


@app.post("/confirm")
def confirm(req: ConfirmRequest, background_tasks: BackgroundTasks,
            user: dict = Depends(current_user)):
    db = _get_db_or_404(req.db_id, user)

    pending = db["pending"].pop(req.query_id, None)
    if not pending:
        raise HTTPException(404, "Unknown or already-resolved query_id.")

    history_id = pending.get("history_id")

    if not req.confirm:
        _update_record(history_id, status="cancelled")
        return {"status": "cancelled"}

    backup = _backup(db)

    start = time.perf_counter()
    try:
        results = run_statements(db["path"], pending["statements"])
    except RuntimeError as e:
        _update_record(
            history_id, status="error", execution_time_ms=_elapsed_ms(start), explanation=str(e)
        )
        return {"status": "error", "error": str(e), "backup": backup}

    elapsed_ms = _elapsed_ms(start)

    _refresh_schema(req.db_id, db, background_tasks)

    _update_record(
        history_id,
        status="executed",
        execution_time_ms=elapsed_ms,
        row_count=sum(max(r.get("rowcount", 0), 0) for r in results),
    )

    return {"status": "executed", "results": results, "backup": backup}


# --------------------------------------------------------------------------
# BACKUPS
# --------------------------------------------------------------------------

@app.get("/backups/{db_id}")
def get_backups(db_id: str, user: dict = Depends(current_user)):
    db = _get_db_or_404(db_id, user)
    return {"backups": list_backups(db["path"], config.BACKUP_DIR)}


@app.post("/backups/{db_id}/restore")
def restore_from_backup(db_id: str, req: RestoreRequest, background_tasks: BackgroundTasks,
                        user: dict = Depends(current_user)):
    """
    Put the database back to how it was when a backup was taken. The
    current state is backed up first, so a restore can itself be undone.
    """
    db = _get_db_or_404(db_id, user)

    try:
        safety_backup = restore_backup(db["path"], config.BACKUP_DIR, req.name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except OSError as e:
        raise HTTPException(500, f"Could not restore backup: {e}")
    safety_backup = f"backups/{os.path.basename(safety_backup)}"

    _cancel_all_pending(db)
    schema = _refresh_schema(db_id, db, background_tasks)

    return {"status": "restored", "restored": req.name, "backup": safety_backup, "schema": schema}


# --------------------------------------------------------------------------
# ADVANCED QUERY HISTORY
# --------------------------------------------------------------------------

@app.get("/history/{db_id}")
def get_history(
    db_id: str,
    limit: int = 20,
    offset: int = 0,
    search: str | None = None,
    status: str | None = None,
    starred: bool = False,
    user: dict = Depends(current_user),
):
    """
    Searchable, paginated, database-aware query history. History is
    always scoped to exactly one database the caller owns — there is no
    cross-database default.
    """
    _get_db_or_404(db_id, user)

    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(400, "offset must be >= 0.")

    return history.list_history(
        db_id=db_id,
        limit=limit,
        offset=offset,
        search=search,
        status=status,
        starred=starred,
    )


@app.get("/history/detail/{history_id}")
def get_history_detail(history_id: str, user: dict = Depends(current_user)):
    return _owned_history_record(history_id, user)


@app.post("/history/star/{history_id}")
def star_history(history_id: str, req: StarRequest, user: dict = Depends(current_user)):
    _owned_history_record(history_id, user)
    history.set_starred(history_id, req.starred)
    return {"history_id": history_id, "starred": req.starred}


# --------------------------------------------------------------------------
# DATASET EXPORT / IMPORT
# --------------------------------------------------------------------------

def _export_response(result: dict, filename_stem: str, export_format: str) -> Response:
    """Turn a run_select() result into a browser-downloadable CSV or XLSX."""
    export_format = (export_format or "csv").lower().strip()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", filename_stem).strip("_") or "dataset"

    if export_format == "csv":
        content = data_tools.to_csv_bytes(result["columns"], result["rows"])
        media_type, extension = "text/csv; charset=utf-8", "csv"
    elif export_format in {"xlsx", "excel"}:
        content = data_tools.to_xlsx_bytes(result["columns"], result["rows"])
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        extension = "xlsx"
    else:
        raise HTTPException(400, "Unsupported export format. Use csv or xlsx.")

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{stem}.{extension}"'},
    )


def _select_for_export(db: dict, sql: str) -> dict:
    return run_select(
        db["path"], sql, limit=config.MAX_EXPORT_ROWS, max_rows=config.MAX_EXPORT_ROWS
    )


@app.get("/export/table/{db_id}/{table_name}")
def export_table(db_id: str, table_name: str, format: str = "csv",
                 user: dict = Depends(current_user)):
    """
    Export the selected table as CSV or XLSX — the whole table (up to
    MAX_EXPORT_ROWS), not only the current preview page.
    """
    db = _get_db_or_404(db_id, user)
    _get_table_or_404(db, table_name)

    try:
        result = _select_for_export(db, f"SELECT * FROM {quote_ident(table_name)}")
    except Exception as e:
        logger.error(f"TABLE EXPORT ERROR db_id={db_id}, table={table_name}: {e}")
        raise HTTPException(500, f"Could not export table: {e}")

    return _export_response(result, table_name, format)


@app.post("/export/query")
def export_query(req: QueryExportRequest, user: dict = Depends(current_user)):
    """
    Export the result of a SELECT query as CSV or XLSX. Exactly one
    SELECT is allowed — this endpoint never runs a write.
    """
    db = _get_db_or_404(req.db_id, user)

    statements = split_statements(req.sql)
    if not statements:
        raise HTTPException(400, "SQL statement cannot be empty.")
    if len(statements) != 1:
        raise HTTPException(400, "Export supports exactly one SELECT statement.")

    try:
        validated = validate_statements(statements, set(db["schema"].keys()))
    except ValueError as e:
        raise HTTPException(400, str(e))

    sql, _, stmt_type = validated[0]
    if stmt_type != "select":
        raise HTTPException(400, "Only SELECT queries can be exported.")

    try:
        result = _select_for_export(db, sql)
    except Exception as e:
        logger.error(f"QUERY EXPORT ERROR db_id={req.db_id}: {e}")
        raise HTTPException(500, f"Could not export query result: {e}")

    return _export_response(result, "query_result", req.format)


@app.post("/import/table")
def import_table(
    background_tasks: BackgroundTasks,
    db_id: str = Form(...),
    table_name: str = Form(...),
    mode: str = Form("new"),
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    """
    Load a CSV/XLSX file into one table of an existing database.
    mode: "new" (table must not exist), "append", or "replace".
    A backup of the database is taken before anything is written.
    """
    db = _get_db_or_404(db_id, user)

    name = os.path.basename(file.filename or "")
    mode = mode.strip().lower()

    if not name.lower().endswith(TABULAR_EXTENSIONS):
        raise HTTPException(400, "Only CSV and XLSX files can be imported.")
    if mode not in data_tools.IMPORT_MODES:
        raise HTTPException(400, "Invalid import mode. Use new, append or replace.")
    try:
        table_name = data_tools.validate_identifier(table_name)
    except ValueError as e:
        raise HTTPException(400, str(e))

    staged = os.path.join(config.UPLOAD_DIR, f"{uuid.uuid4()}.upload")

    try:
        _save_upload(file, staged)
        backup = _backup(db)
        rows = data_tools.import_into_table(db["path"], staged, name, table_name, mode)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Import failed: {e}")
    finally:
        _remove_file(staged)

    schema = _refresh_schema(db_id, db, background_tasks)

    return {
        "status": "imported",
        "table": table_name,
        "rows": rows,
        "backup": backup,
        "schema": schema,
    }


# --------------------------------------------------------------------------
# RAG
# --------------------------------------------------------------------------

@app.post("/rag/search")
def rag_search(req: RagSearchRequest, user: dict = Depends(current_user)):
    db = _get_db_or_404(req.db_id, user)

    if not req.question or not req.question.strip():
        raise HTTPException(400, "question is required.")

    try:
        result = rag.retrieve_schema(req.db_id, req.question, schema=db["schema"])
    except Exception as e:
        logger.error(f"RAG /rag/search failed for db_id={req.db_id}: {e}")
        raise HTTPException(500, f"RAG retrieval failed: {e}")

    response = {
        "question": req.question,
        "tables": result.get("tables", []),
        "documents": result.get("documents", []),
    }
    if not response["tables"]:
        response["note"] = "No relevant schema found."

    return response


app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")

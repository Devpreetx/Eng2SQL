"""
Eng2SQL Agent — FastAPI backend.

Flow (via LangGraph agent):
  1. POST /upload        -> store a SQLite file, extract schema
  2. POST /query         -> runs the LangGraph agent. Pure reads execute
                             immediately. Any batch containing a
                             write/create returns a preview and a
                             query_id, and waits for confirmation.
                             Accepts an optional session_id for
                             conversation memory.
  3. POST /confirm       -> execute (or cancel) a pending batch of
                             statements as ONE transaction, with an
                             automatic backup taken first.
  4. GET  /schema/{id}   -> inspect the extracted schema
  5. GET  /databases     -> list all currently loaded/created databases
  6. POST /sql/execute   -> SQL WORKSPACE. Manual SQL, same validation/
                             execution/confirmation pipeline as /query.
  7. GET  /history/{db_id}          -> searchable, paginated query
                                        history for one database.
  8. GET  /history/detail/{id}      -> one full history record.

ADVANCED QUERY HISTORY (this phase):
  Every query — AI-generated or manual — is persisted to a dedicated
  SQLite store (app/history.py -> data/history.sqlite), not just an
  in-memory list. A confirmation-based write records ONE history row
  when the preview is shown (status="confirmation_required") and that
  SAME row is updated in place when /confirm resolves it — never a
  duplicate second row. history.py owns storage/retrieval only; this
  file decides *when* to call it, reusing data it already computes for
  /query, /sql/execute and /confirm.

The agent only ORCHESTRATES existing rag.py/llm.py/db_utils.py/memory.py
logic — no SQL generation, validation, execution, or memory-storage logic
is duplicated here. The SQL Workspace and History endpoints follow the
same rule: they orchestrate db_utils/history, they do not reimplement it.
"""

import logging
import os
import shutil
import time
import uuid
import pandas as pd
from pydantic import BaseModel
import sqlite3

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.db_utils import (
    backup_db,
    build_preview_sql,
    extract_schema,
    run_select,
    run_statements,
    validate_statements,
)
from app import rag
from app import history
from app.agent import run_agent

logger = logging.getLogger("main")
logger.setLevel(logging.INFO)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
STATIC_DIR = os.path.join(BASE_DIR, "static")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

app = FastAPI(title="Eng2SQL Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DBS: dict = {}


@app.on_event("startup")
async def _startup():
    # Creates data/history.sqlite + query_history table/indexes if they
    # don't exist yet. Safe to call on every startup — CREATE TABLE/INDEX
    # IF NOT EXISTS.
    history.init_db()


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


def _get_db_or_404(db_id: str) -> dict:
    db = DBS.get(db_id)
    if not db:
        raise HTTPException(404, "Unknown db_id. Upload a database first.")
    return db


def _public_db_summary(db_id: str, db: dict) -> dict:
    return {
        "db_id": db_id,
        "name": db.get("name", "untitled"),
        "tables": list(db.get("schema", {}).keys()),
    }


def _safe_index_schema(db_id: str, schema: dict) -> None:
    try:
        rag.refresh_schema_index(db_id, schema)
    except Exception as e:
        logger.error(f"RAG indexing failed for db_id={db_id}: {e}")


@app.get("/databases")
async def list_databases():
    return {
        "databases": [
            _public_db_summary(db_id, db) for db_id, db in DBS.items()
        ]
    }


@app.post("/upload")
async def upload_db(background_tasks: BackgroundTasks, file: UploadFile = File(...)):

    filename = file.filename.lower()

    allowed = (
        ".db",
        ".sqlite",
        ".sqlite3",
        ".csv",
        ".xlsx",
    )

    if not filename.endswith(allowed):
        raise HTTPException(
            400,
            "Supported files: .db, .sqlite, .sqlite3, .csv, .xlsx"
        )

    db_id = str(uuid.uuid4())

    path = os.path.join(
        UPLOAD_DIR,
        f"{db_id}.sqlite"
    )

    try:

        if filename.endswith((".db", ".sqlite", ".sqlite3")):

            with open(path, "wb") as f:
                shutil.copyfileobj(file.file, f)

        elif filename.endswith(".csv"):

            df = pd.read_csv(file.file)

            table_name = os.path.splitext(
                os.path.basename(file.filename)
            )[0]

            table_name = table_name.replace(" ", "_")

            conn = sqlite3.connect(path)

            df.to_sql(
                table_name,
                conn,
                if_exists="replace",
                index=False
            )

            conn.close()

        elif filename.endswith(".xlsx"):

            excel = pd.ExcelFile(file.file)

            conn = sqlite3.connect(path)

            for sheet in excel.sheet_names:

                df = pd.read_excel(
                    excel,
                    sheet_name=sheet
                )

                table_name = sheet.strip().replace(
                    " ",
                    "_"
                )

                df.to_sql(
                    table_name,
                    conn,
                    if_exists="replace",
                    index=False
                )

            conn.close()

        schema = extract_schema(path)

        if not schema:
            raise HTTPException(
                400,
                "No tables found in the uploaded dataset."
            )

        DBS[db_id] = {
        "path": path,
        "schema": schema,
        "pending": {},
        "history": [],
        "name": file.filename
            }

        background_tasks.add_task(_safe_index_schema, db_id, schema)

        return {
            "db_id": db_id,
            "filename": file.filename,
            "name": file.filename,
            "tables": list(schema.keys()),
            "schema": schema
        }

    except HTTPException:
        if os.path.exists(path):
            os.remove(path)
        raise

    except Exception as e:

        if os.path.exists(path):
            os.remove(path)

        raise HTTPException(
            400,
            f"Could not process uploaded file: {e}"
        )

    
@app.post("/database/create")
async def create_database(name: str, background_tasks: BackgroundTasks):
    name = name.strip()

    if not name:
        raise HTTPException(400, "Database name is required.")

    safe_name = "".join(
        c for c in name
        if c.isalnum() or c in ("_", "-")
    )

    if not safe_name:
        raise HTTPException(400, "Invalid database name.")

    if not safe_name.endswith(".db"):
        safe_name += ".db"

    db_id = str(uuid.uuid4())

    path = os.path.join(
        UPLOAD_DIR,
        f"{db_id}.sqlite"
    )

    try:
        conn = sqlite3.connect(path)
        conn.close()

        schema = {}

        DBS[db_id] = {
            "path": path,
            "schema": schema,
            "pending": {},
            "history": [],
            "name": safe_name
        }

        background_tasks.add_task(_safe_index_schema, db_id, schema)

        return {
            "db_id": db_id,
            "name": safe_name,
            "tables": [],
            "schema": {}
        }

    except Exception as e:

        if os.path.exists(path):
            os.remove(path)

        raise HTTPException(
            500,
            f"Could not create database: {e}"
        )

class CreateTableRequest(BaseModel):
    db_id: str
    table_name: str
    columns: list[dict]

@app.post("/table/create")
async def create_table(req: CreateTableRequest, background_tasks: BackgroundTasks):

    db = _get_db_or_404(req.db_id)

    table_name = req.table_name.strip()

    if not table_name:
        raise HTTPException(400, "Table name is required.")

    if not req.columns:
        raise HTTPException(400, "At least one column is required.")

    if not table_name.replace("_", "").isalnum():
        raise HTTPException(400, "Invalid table name.")

    for column in req.columns:

        name = column.get("name", "").strip()
        data_type = column.get("type", "TEXT").upper()

        if not name:
            raise HTTPException(400, "Column name cannot be empty.")

        if not name.replace("_", "").isalnum():
            raise HTTPException(
                400,
                f"Invalid column name: {name}"
            )

        allowed_types = {
            "TEXT",
            "INTEGER",
            "REAL",
            "BLOB",
            "NUMERIC"
        }

        if data_type not in allowed_types:
            raise HTTPException(
                400,
                f"Invalid data type: {data_type}"
            )

    column_sql = []

    for column in req.columns:

        name = column["name"].strip()
        data_type = column.get("type", "TEXT").upper()

        column_sql.append(
            f'"{name}" {data_type}'
        )

    sql = f'''
        CREATE TABLE "{table_name}" (
            {", ".join(column_sql)}
        )
    '''

    try:

        conn = sqlite3.connect(db["path"])

        conn.execute(sql)

        conn.commit()
        conn.close()

        db["schema"] = extract_schema(db["path"])

        background_tasks.add_task(_safe_index_schema, req.db_id, db["schema"])

        return {
            "status": "created",
            "table": table_name,
            "schema": db["schema"]
        }

    except sqlite3.OperationalError as e:

        raise HTTPException(
            400,
            str(e)
        )

    
@app.get("/schema/{db_id}")
async def get_schema(db_id: str):
    db = _get_db_or_404(db_id)
    return db["schema"]


@app.post("/query")
async def query(req: QueryRequest):
    db = _get_db_or_404(req.db_id)

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
        raise HTTPException(502, f"Agent execution failed: {e}")
    # NOTE: this measures the whole agent run (RAG retrieval + LLM SQL
    # generation/correction + execution + analysis), not isolated SQL
    # execution time — the agent doesn't expose a narrower figure, and
    # agent.py isn't modified for this phase. The SQL Workspace path
    # below has true, execution-only timing since it doesn't call an LLM.
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

    # Always echo session_id back, even on early-exit paths, so the
    # frontend can persist it starting from the very first query.
    result.setdefault("session_id", session_id)

    status = result.get("status")
    database_name = db.get("name")
    standalone_question = result.get("standalone_question")

    if status == "needs_clarification":
        # Nothing concrete happened yet (no SQL, no outcome) — per spec
        # section 3, don't save an incomplete entry.
        return result

    if status == "rejected":
        history.insert_history(
            db_id=req.db_id,
            database_name=database_name,
            session_id=session_id,
            question=req.question,
            standalone_question=standalone_question,
            sql=result.get("statements", []),
            status="rejected",
            execution_time_ms=elapsed_ms,
        )
        return result

    if status == "error":
        history.insert_history(
            db_id=req.db_id,
            database_name=database_name,
            session_id=session_id,
            question=req.question,
            standalone_question=standalone_question,
            sql=result.get("statements", []),
            status="error",
            execution_time_ms=elapsed_ms,
            explanation=result.get("error"),
        )
        return result

    if status == "executed":
        db["history"].append(
            {
                "question": req.question,
                "statements": result.get("statements", []),
                "type": "read",
            }
        )

        row_count = sum(
            len(r.get("rows", [])) for r in result.get("results", [])
        )
        chart = result.get("chart") or {}

        history.insert_history(
            db_id=req.db_id,
            database_name=database_name,
            session_id=session_id,
            question=req.question,
            standalone_question=standalone_question,
            sql=result.get("statements", []),
            status="executed",
            execution_time_ms=elapsed_ms,
            row_count=row_count,
            chart_type=chart.get("chart_type") if chart.get("chart_required") else None,
            explanation=result.get("explanation"),
        )
        return result

    if status == "confirmation_required":
        query_id = str(uuid.uuid4())

        history_id = history.insert_history(
            db_id=req.db_id,
            database_name=database_name,
            session_id=session_id,
            question=req.question,
            standalone_question=standalone_question,
            sql=result.get("statements", []),
            status="confirmation_required",
            execution_time_ms=elapsed_ms,
            explanation=result.get("explanation"),
        )

        db["pending"][query_id] = {
            "statements": result.get("statements", []),
            "stmt_types": [p.get("type") for p in result.get("previews", [])],
            "question": req.question,
            "history_id": history_id,
        }
        response = dict(result)
        response["query_id"] = query_id
        return response

    # Unknown/unexpected status — surface as an error rather than crashing.
    return {"status": "error", "error": f"Unexpected agent status: {status}", "session_id": session_id}


@app.get("/table/{db_id}/{table_name}")
async def get_table_data(
    db_id: str,
    table_name: str,
    page: int = 1,
    limit: int = 50
):
    db = _get_db_or_404(db_id)

    if table_name not in db["schema"]:
        raise HTTPException(404, "Table not found.")

    if page < 1:
        raise HTTPException(400, "Page must be >= 1.")

    if limit < 1 or limit > 500:
        raise HTTPException(400, "Limit must be between 1 and 500.")

    offset = (page - 1) * limit

    conn = sqlite3.connect(db["path"])
    conn.row_factory = sqlite3.Row

    try:
        cursor = conn.execute(
            f'SELECT * FROM "{table_name}" LIMIT ? OFFSET ?',
            (limit, offset)
        )

        rows = [dict(row) for row in cursor.fetchall()]

        count = conn.execute(
            f'SELECT COUNT(*) FROM "{table_name}"'
        ).fetchone()[0]

        return {
            "table": table_name,
            "page": page,
            "limit": limit,
            "total_rows": count,
            "rows": rows
        }

    finally:
        conn.close()


# --------------------------------------------------------------------------
# SQL WORKSPACE
# --------------------------------------------------------------------------

def _split_sql_statements(sql_text: str) -> list[str]:
    """
    Splits the SQL Workspace textarea content into individual statements
    on ';' boundaries, dropping empty fragments. validate_statements()
    (SQLGlot-backed) still performs the real parsing/validation of each
    piece, exactly as it does for AI-generated SQL.
    """
    raw_parts = sql_text.split(";")
    statements = [p.strip() for p in raw_parts if p.strip()]
    return statements


@app.post("/sql/execute")
async def sql_execute(req: SqlExecuteRequest):
    db = _get_db_or_404(req.db_id)

    logger.info("SQL WORKSPACE REQUEST")
    logger.info(f"DB ID: {req.db_id}")

    sql_text = (req.sql or "").strip()
    if not sql_text:
        logger.info("VALIDATION: rejected (empty SQL)")
        return {
            "status": "error",
            "error": "SQL statement cannot be empty.",
        }

    statements = _split_sql_statements(sql_text)
    if not statements:
        logger.info("VALIDATION: rejected (empty SQL)")
        return {
            "status": "error",
            "error": "SQL statement cannot be empty.",
        }

    known_tables = set(db["schema"].keys())
    database_name = db.get("name")

    try:
        validated = validate_statements(statements, known_tables)
    except ValueError as e:
        logger.info(f"VALIDATION: rejected ({e})")
        history.insert_history(
            db_id=req.db_id,
            database_name=database_name,
            question=None,
            sql=statements,
            status="rejected",
            explanation=str(e),
        )
        return {
            "status": "rejected",
            "reason": str(e),
            "statements": statements,
        }
    except Exception as e:
        logger.error(f"SQL Workspace validation crashed for db_id={req.db_id}: {e}")
        history.insert_history(
            db_id=req.db_id,
            database_name=database_name,
            question=None,
            sql=statements,
            status="error",
            explanation="SQL syntax error: could not validate the statement.",
        )
        return {
            "status": "error",
            "error": "SQL syntax error: could not validate the statement.",
        }

    logger.info("VALIDATION: valid")

    stmt_types = {stmt_type for (_, _, stmt_type) in validated}

    # ---- Pure SELECT batch: execute immediately, same as /query ----
    if stmt_types == {"select"}:
        logger.info("SQL TYPE: select")
        start = time.perf_counter()
        try:
            results = [run_select(db["path"], sql) for sql, _, _ in validated]
        except Exception as e:
            logger.info(f"EXECUTION: error ({e})")
            history.insert_history(
                db_id=req.db_id,
                database_name=database_name,
                question=None,
                sql=[sql for sql, _, _ in validated],
                status="error",
                explanation=str(e),
            )
            return {
                "status": "error",
                "error": f"SQL execution failed: {e}",
                "statements": [sql for sql, _, _ in validated],
            }
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        logger.info("EXECUTION: executed")

        db["history"].append(
            {
                "statements": [sql for sql, _, _ in validated],
                "type": "read",
                "source": "sql_workspace",
            }
        )

        row_count = sum(len(r.get("rows", [])) for r in results)

        history.insert_history(
            db_id=req.db_id,
            database_name=database_name,
            question=None,
            sql=[sql for sql, _, _ in validated],
            status="executed",
            execution_time_ms=elapsed_ms,
            row_count=row_count,
        )

        return {
            "status": "executed",
            "statements": [sql for sql, _, _ in validated],
            "results": results,
            "execution_time_ms": elapsed_ms,
        }

    # ---- Write/create batch: build previews, defer to /confirm ----
    logger.info(f"SQL TYPE: {'/'.join(sorted(stmt_types))}")

    previews = []
    for sql, expr, stmt_type in validated:
        preview = {"sql": sql, "type": stmt_type, "affected_count": None, "sample_rows": None}
        try:
            count_sql, sample_sql = build_preview_sql(expr, stmt_type)
            if count_sql:
                count_data = run_select(db["path"], count_sql)
                sample_data = run_select(db["path"], sample_sql)
                if count_data["rows"]:
                    preview["affected_count"] = count_data["rows"][0].get("affected_count")
                preview["sample_rows"] = sample_data["rows"]
        except Exception:
            pass
        previews.append(preview)

    query_id = str(uuid.uuid4())

    history_id = history.insert_history(
        db_id=req.db_id,
        database_name=database_name,
        question=None,
        sql=[sql for sql, _, _ in validated],
        status="confirmation_required",
    )

    db["pending"][query_id] = {
        "statements": [sql for sql, _, _ in validated],
        "stmt_types": [t for (_, _, t) in validated],
        "question": None,
        "source": "sql_workspace",
        "history_id": history_id,
    }

    logger.info("EXECUTION: confirmation_required")

    return {
        "status": "confirmation_required",
        "query_id": query_id,
        "statements": [sql for sql, _, _ in validated],
        "previews": previews,
    }


@app.post("/confirm")
async def confirm(req: ConfirmRequest, background_tasks: BackgroundTasks):
    db = _get_db_or_404(req.db_id)

    pending = db["pending"].pop(req.query_id, None)
    if not pending:
        raise HTTPException(404, "Unknown or already-resolved query_id.")

    history_id = pending.get("history_id")

    if not req.confirm:
        db["history"].append(
            {
                "statements": pending["statements"],
                "types": pending["stmt_types"],
                "status": "cancelled",
            }
        )
        if history_id:
            # Same row created when the preview was first shown — updated
            # in place, not duplicated.
            history.update_history(history_id, status="cancelled")
        return {"status": "cancelled"}

    backup_path = backup_db(db["path"], BACKUP_DIR)

    start = time.perf_counter()
    try:
        results = run_statements(db["path"], pending["statements"])
    except RuntimeError as e:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        db["history"].append(
            {
                "statements": pending["statements"],
                "types": pending["stmt_types"],
                "status": "error",
                "error": str(e),
                "backup": backup_path,
            }
        )
        if history_id:
            history.update_history(
                history_id,
                status="error",
                execution_time_ms=elapsed_ms,
                explanation=str(e),
            )
        return {"status": "error", "error": str(e), "backup": backup_path}

    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

    db["schema"] = extract_schema(db["path"])

    background_tasks.add_task(_safe_index_schema, req.db_id, db["schema"])

    db["history"].append(
        {
            "statements": pending["statements"],
            "types": pending["stmt_types"],
            "status": "executed",
            "results": results,
            "backup": backup_path,
        }
    )

    if history_id:
        total_rowcount = sum(r.get("rowcount", 0) for r in results)
        history.update_history(
            history_id,
            status="executed",
            execution_time_ms=elapsed_ms,
            row_count=total_rowcount,
        )

    return {"status": "executed", "results": results, "backup": backup_path}


# --------------------------------------------------------------------------
# ADVANCED QUERY HISTORY
# --------------------------------------------------------------------------

@app.get("/history/{db_id}")
async def get_history(
    db_id: str,
    limit: int = 20,
    offset: int = 0,
    search: str | None = None,
    status: str | None = None,
):
    """
    Searchable, paginated, database-aware query history. db_id must be a
    known database — per spec section 12 ("Never mix databases
    unintentionally"), history is always scoped to exactly one db_id;
    there is no cross-database default.
    """
    _get_db_or_404(db_id)

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
    )


@app.get("/history/detail/{history_id}")
async def get_history_detail(history_id: str):
    record = history.get_history_detail(history_id)
    if record is None:
        raise HTTPException(404, "Unknown history_id.")
    return record


@app.get("/download/{db_id}")
async def download_current_db(db_id: str):
    from fastapi.responses import FileResponse

    db = _get_db_or_404(db_id)
    return FileResponse(db["path"], filename="database.sqlite")


@app.post("/rag/search")
async def rag_search(req: RagSearchRequest):
    if not req.db_id:
        raise HTTPException(400, "db_id is required.")

    if req.db_id not in DBS:
        raise HTTPException(404, "Unknown db_id. Upload a database first.")

    if not req.question or not req.question.strip():
        raise HTTPException(400, "question is required.")

    try:
        result = rag.retrieve_schema(req.db_id, req.question, k=5)
    except Exception as e:
        logger.error(f"RAG /rag/search failed for db_id={req.db_id}: {e}")
        raise HTTPException(500, f"RAG retrieval failed: {e}")

    if not result.get("tables"):
        return {
            "question": req.question,
            "tables": [],
            "documents": [],
            "note": "No relevant schema found.",
        }

    return {
        "question": req.question,
        "tables": result["tables"],
        "documents": result["documents"],
    }


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
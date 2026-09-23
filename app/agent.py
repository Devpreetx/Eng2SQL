"""
LangGraph agent orchestrating the existing Eng2SQL pipeline.

PHASE 1: analyze_result asks Mistral to interpret the actual SQL result.
PHASE 2: decide_visualization inspects SQL result shape and picks a chart.
PHASE 3: conversation memory — load_memory / resolve_follow_up / save_memory
         wrap the existing flow so follow-up questions ("them", "those",
         "only above X") resolve against prior turns for the SAME
         (session_id, db_id) pair only.

No duplication of rag.py / llm.py / db_utils.py / memory.py logic — this
module only orchestrates them.
"""

import logging
import re
from typing import TypedDict, Optional

from langgraph.graph import StateGraph, END

from app import rag
from app import llm as llm_module
from app import memory as memory_module
from app.db_utils import (
    build_preview_sql,
    run_select,
    validate_statements,
)

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)

MAX_CORRECTION_ATTEMPTS = 3
MAX_ROWS_FOR_LLM = 20        # cap rows sent to Mistral for analysis
MAX_ROWS_FOR_CHART = 100     # cap rows used to build chart_data


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    db_id: str
    question: str
    schema: dict
    db_path: str

    # Phase 3 — memory state
    session_id: str
    conversation_history: list
    standalone_question: str

    intent: str
    needs_clarification: bool
    clarification_question: str

    retrieved_schema: Optional[dict]

    generated_sql: dict
    statements: list
    explanation: str
    target_table: str

    validated: list
    validation_result: str
    validation_error: str

    correction_attempts: int

    execution_status: str
    execution_result: dict

    final_answer: str

    # Phase 2 — chart state
    chart_required: bool
    chart_type: str
    chart_title: str
    chart_x_column: Optional[str]
    chart_y_column: Optional[str]
    chart_data: list


# --------------------------------------------------------------------------
# PHASE 3 — load_memory / resolve_follow_up / save_memory
# --------------------------------------------------------------------------

def load_memory_node(state: AgentState) -> AgentState:
    """
    Loads recent conversation history for this exact (session_id, db_id)
    pair. Strictly isolated: a different db_id (even same session) never
    sees this history, per the isolation requirement.
    """
    logger.info("NODE: load_memory")

    session_id = state.get("session_id") or ""
    db_id = state.get("db_id") or ""

    logger.info(f"SESSION ID: {session_id}")
    logger.info(f"DATABASE ID: {db_id}")

    try:
        history = memory_module.get_recent_history(session_id, db_id)
    except Exception as e:
        logger.error(f"load_memory failed for session_id={session_id}, db_id={db_id}: {e}")
        history = []

    logger.info(f"CONVERSATION HISTORY LENGTH: {len(history)}")

    state["conversation_history"] = history
    return state


def resolve_follow_up_node(state: AgentState) -> AgentState:
    """
    Rewrites ambiguous follow-up questions ("them", "those", "only above
    X") into standalone questions using conversation_history + Mistral.
    Leaves standalone questions unchanged. Sets needs_clarification when
    the reference can't be resolved.
    """
    logger.info("NODE: resolve_follow_up")

    original_question = state.get("question", "")
    logger.info(f"ORIGINAL QUESTION: {original_question}")

    if state.get("needs_clarification"):
        # understand_question already flagged this (e.g. empty question) —
        # don't attempt resolution.
        state["standalone_question"] = original_question
        logger.info(f"STANDALONE QUESTION: {original_question}")
        return state

    session_id = state.get("session_id") or ""
    db_id = state.get("db_id") or ""

    try:
        result = memory_module.resolve_follow_up(
            session_id=session_id,
            db_id=db_id,
            question=original_question,
            llm_invoke_fn=llm_module.llm.invoke,
        )
    except Exception as e:
        logger.error(f"resolve_follow_up failed, using original question: {e}")
        result = {
            "standalone_question": original_question,
            "needs_clarification": False,
            "clarification_question": None,
        }

    standalone_question = result.get("standalone_question") or original_question
    state["standalone_question"] = standalone_question

    logger.info(f"STANDALONE QUESTION: {standalone_question}")

    if result.get("needs_clarification"):
        state["needs_clarification"] = True
        state["clarification_question"] = result.get(
            "clarification_question", "Could you clarify which records you mean?"
        )

    return state


def save_memory_node(state: AgentState) -> AgentState:
    """
    Persists this turn (user question, resolved standalone question,
    assistant answer, and generated SQL if useful) for this exact
    (session_id, db_id) pair. Runs on every path through final_response,
    including clarification/rejection/error, so the conversation stays
    coherent — but only stores conversational content, never secrets.
    """
    logger.info("NODE: save_memory")

    session_id = state.get("session_id") or ""
    db_id = state.get("db_id") or ""

    if not session_id or not db_id:
        logger.info("save_memory skipped: missing session_id or db_id")
        return state

    result = state.get("execution_result", {}) or {}
    status = result.get("status")

    # Don't pollute memory with unresolved clarification turns — nothing
    # concrete happened yet for future follow-ups to reference.
    if status == "needs_clarification":
        return state

    user_question = state.get("question", "")
    standalone_question = state.get("standalone_question") or user_question

    if status == "executed":
        assistant_answer = result.get("explanation") or "Query executed."
        statements = result.get("statements") or []
        sql_text = "; ".join(statements) if statements else None
    elif status == "rejected":
        assistant_answer = f"Rejected: {result.get('reason', '')}"
        sql_text = None
    elif status == "error":
        assistant_answer = f"Error: {result.get('error', '')}"
        sql_text = None
    elif status == "confirmation_required":
        assistant_answer = "Awaiting confirmation for a write operation."
        statements = result.get("statements") or []
        sql_text = "; ".join(statements) if statements else None
    else:
        assistant_answer = ""
        sql_text = None

    try:
        memory_module.save_exchange(
            session_id=session_id,
            db_id=db_id,
            user_question=user_question,
            standalone_question=standalone_question,
            assistant_answer=assistant_answer,
            sql=sql_text,
        )
    except Exception as e:
        logger.error(f"save_memory failed for session_id={session_id}, db_id={db_id}: {e}")

    return state


# --------------------------------------------------------------------------
# Nodes: understand / retrieve / generate / validate / correct
# --------------------------------------------------------------------------

def understand_question(state: AgentState) -> AgentState:
    logger.info("NODE: understand_question")
    question = (state.get("question") or "").strip()

    if not question:
        state["needs_clarification"] = True
        state["clarification_question"] = "Could you provide a question?"
        state["intent"] = "other"
        return state

    state["needs_clarification"] = False
    state["clarification_question"] = ""
    state["intent"] = "unknown"
    return state


def retrieve_schema_node(state: AgentState) -> AgentState:
    logger.info("NODE: retrieve_schema")
    db_id = state["db_id"]
    # RAG receives the RESOLVED standalone question, not the raw follow-up
    # fragment, per spec section 4/13. Falls back to `question` if no
    # standalone_question was set (keeps old callers/tests working).
    question = state.get("standalone_question") or state["question"]

    try:
        result = rag.retrieve_schema(db_id, question, k=5)
    except Exception as e:
        logger.error(f"Agent RAG retrieval failed for db_id={db_id}: {e}")
        result = {"tables": [], "documents": []}

    logger.info(f"Retrieved tables: {result.get('tables', [])}")
    state["retrieved_schema"] = result
    return state


def generate_sql_node(state: AgentState) -> AgentState:
    logger.info("NODE: generate_sql")
    db_id = state["db_id"]
    # Same rule as retrieve_schema_node: SQL generation uses the resolved
    # standalone question so follow-ups compile to complete SQL.
    question = state.get("standalone_question") or state["question"]
    full_schema = state.get("schema") or {}

    try:
        result = llm_module.generate_sql(
            db_id=db_id,
            question=question,
            schema=full_schema,
        )
    except Exception as e:
        logger.error(f"Agent SQL generation failed for db_id={db_id}: {e}")
        state["needs_clarification"] = True
        state["clarification_question"] = f"SQL generation failed: {e}"
        state["generated_sql"] = {}
        state["statements"] = []
        return state

    state["generated_sql"] = result
    state["statements"] = result.get("statements", [])
    state["explanation"] = result.get("explanation", "")
    state["target_table"] = result.get("target_table", "")
    state["intent"] = result.get("intent", "other")

    if result.get("needs_clarification"):
        state["needs_clarification"] = True
        state["clarification_question"] = result.get(
            "clarification_question", "Could you clarify your request?"
        )

    logger.info(f"Generated SQL: {state['statements']}")
    return state


def validate_sql_node(state: AgentState) -> AgentState:
    logger.info("NODE: validate_sql")

    statements = state.get("statements", [])
    if not statements:
        state["validation_result"] = "none"
        state["validation_error"] = ""
        return state

    known_tables = set((state.get("schema") or {}).keys())

    try:
        validated = validate_statements(statements, known_tables)
        state["validated"] = validated
        state["validation_result"] = "valid"
        state["validation_error"] = ""
        logger.info("Validation result: valid")
    except ValueError as e:
        state["validated"] = []
        state["validation_result"] = "invalid"
        state["validation_error"] = str(e)
        logger.info(f"Validation result: invalid ({e})")

    return state


def correct_sql_node(state: AgentState) -> AgentState:
    logger.info("NODE: correct_sql")

    attempts = state.get("correction_attempts", 0) + 1
    state["correction_attempts"] = attempts
    logger.info(f"Correction attempt count: {attempts}")

    if attempts > MAX_CORRECTION_ATTEMPTS:
        return state

    db_id = state["db_id"]
    question = state.get("standalone_question") or state["question"]
    full_schema = state.get("schema") or {}
    bad_statements = state.get("statements", [])
    error = state.get("validation_error", "")

    correction_question = (
        f"{question}\n\n"
        f"NOTE: A previous attempt generated this SQL, which failed validation:\n"
        f"{bad_statements}\n\n"
        f"Validation error: {error}\n"
        f"Please generate corrected SQL that fixes this error, using only "
        f"real tables/columns from the schema."
    )

    try:
        result = llm_module.generate_sql(
            db_id=db_id,
            question=correction_question,
            schema=full_schema,
        )
    except Exception as e:
        logger.error(f"Agent SQL correction failed for db_id={db_id}: {e}")
        state["statements"] = []
        state["validation_error"] = f"Correction failed: {e}"
        return state

    state["generated_sql"] = result
    state["statements"] = result.get("statements", [])
    state["explanation"] = result.get("explanation", "")
    state["target_table"] = result.get("target_table", "")

    if result.get("needs_clarification"):
        state["needs_clarification"] = True
        state["clarification_question"] = result.get(
            "clarification_question", "Could you clarify your request?"
        )

    logger.info(f"Corrected SQL: {state['statements']}")
    return state


def execute_sql_node(state: AgentState) -> AgentState:
    logger.info("NODE: execute_sql")

    db_path = state["db_path"]
    validated = state.get("validated", [])

    if not validated:
        state["execution_status"] = "error"
        state["execution_result"] = {
            "status": "error",
            "error": "No SQL statements were generated.",
        }
        return state

    stmt_types = {stmt_type for (_, _, stmt_type) in validated}

    if stmt_types == {"select"}:
        results = []
        try:
            for sql, _, _ in validated:
                results.append(run_select(db_path, sql))
        except Exception as e:
            logger.info("Execution status: error")
            state["execution_status"] = "error"
            state["execution_result"] = {
                "status": "error",
                "statements": [sql for sql, _, _ in validated],
                "error": str(e),
            }
            return state

        logger.info("Execution status: executed")
        state["execution_status"] = "executed"
        state["execution_result"] = {
            "status": "executed",
            "statements": [sql for sql, _, _ in validated],
            "explanation": state.get("explanation", ""),
            "results": results,
        }
        return state

    previews = []
    for sql, expr, stmt_type in validated:
        preview = {"sql": sql, "type": stmt_type, "affected_count": None, "sample_rows": None}
        try:
            count_sql, sample_sql = build_preview_sql(expr, stmt_type)
            if count_sql:
                count_data = run_select(db_path, count_sql)
                sample_data = run_select(db_path, sample_sql)
                if count_data["rows"]:
                    preview["affected_count"] = count_data["rows"][0].get("affected_count")
                preview["sample_rows"] = sample_data["rows"]
        except Exception:
            pass
        previews.append(preview)

    logger.info("Execution status: confirmation_required")
    state["execution_status"] = "confirmation_required"
    state["execution_result"] = {
        "status": "confirmation_required",
        "statements": [sql for sql, _, _ in validated],
        "explanation": state.get("explanation", ""),
        "previews": previews,
    }
    return state


# --------------------------------------------------------------------------
# PHASE 1 — analyze_result
# --------------------------------------------------------------------------

def analyze_result_node(state: AgentState) -> AgentState:
    """
    Interprets the SQL result via Mistral. NEVER performs computation the
    database already did — only explains/summarizes returned rows.
    On any failure, falls back to a generic but accurate message and does
    NOT fail the query.
    """
    logger.info("NODE: analyze_result")

    if state.get("execution_status") != "executed":
        state["final_answer"] = ""
        return state

    results = state.get("execution_result", {}).get("results", [])
    if not results:
        state["final_answer"] = ""
        return state

    first = results[0]
    columns = first.get("columns", [])
    all_rows = first.get("rows", [])
    row_count = len(all_rows)

    if row_count == 0:
        state["final_answer"] = "No matching records were found."
        return state

    # Never send huge result sets to the LLM — cap what's sent.
    sample_rows = all_rows[:MAX_ROWS_FOR_LLM]

    try:
        # Use the resolved standalone question so the analysis reads
        # naturally for follow-ups (e.g. "how much did he spend?").
        question = state.get("standalone_question") or state["question"]

        analysis_prompt = (
            "You are analyzing the result of a SQL query that has ALREADY "
            "been executed. The database performed all filtering, grouping, "
            "aggregation, sorting, and joins. Your job is ONLY to explain "
            "and summarize the data below in plain language.\n\n"
            "STRICT RULES:\n"
            "- Use ONLY the data given below. Never invent values, rows, or trends.\n"
            "- Do not recompute or guess numbers beyond what's shown.\n"
            "- Keep the answer concise (1-3 sentences).\n"
            "- Mention the specific important number(s) if relevant.\n"
            "- If this is a plain lookup/listing with many rows, briefly state "
            "what was found and the row count rather than listing every row.\n\n"
            f"Question: {question}\n"
            f"Columns: {columns}\n"
            f"Total row count: {row_count}\n"
            f"Rows (showing up to {MAX_ROWS_FOR_LLM}): {sample_rows}\n\n"
            "Give the direct answer now, in plain text, no markdown."
        )

        response = llm_module.llm.invoke(analysis_prompt)
        answer = (response.content or "").strip()
        state["final_answer"] = answer if answer else (
            f"Query executed successfully and returned {row_count} row(s)."
        )
    except Exception as e:
        logger.error(f"Agent result analysis failed: {e}")
        # Do NOT fail the query — fall back to a safe generic message.
        state["final_answer"] = (
            f"Query executed successfully and returned {row_count} row(s)."
        )

    return state


# --------------------------------------------------------------------------
# PHASE 2 — decide_visualization  (UNCHANGED from previous phase)
# --------------------------------------------------------------------------

_DATE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(:\d{2})?)?$"
)

_DATE_NAME_HINTS = ("date", "month", "year", "day", "time", "created_at", "updated_at", "timestamp")


def is_numeric_column(rows: list, col: str) -> bool:
    """Inspects actual values (skipping Nones) to decide if a column is numeric."""
    seen_any = False
    for r in rows[:10]:
        v = r.get(col)
        if v is None:
            continue
        seen_any = True
        if isinstance(v, bool):
            return False
        if not isinstance(v, (int, float)):
            if isinstance(v, str):
                try:
                    float(v)
                    continue
                except ValueError:
                    return False
            return False
    return seen_any


def is_datetime_column(col_name: str, rows: list, col: str) -> bool:
    """Inspects actual values first; falls back to column-name hints."""
    for r in rows[:10]:
        v = r.get(col)
        if isinstance(v, str) and _DATE_PATTERN.match(v.strip()):
            return True

    lowered = (col_name or "").lower()
    return any(hint in lowered for hint in _DATE_NAME_HINTS)


def is_categorical_column(rows: list, col: str) -> bool:
    """A column is categorical/text if it's neither numeric nor datetime-like."""
    for r in rows[:10]:
        v = r.get(col)
        if v is None:
            continue
        if isinstance(v, str) and not _DATE_PATTERN.match(v.strip()):
            return True
    return not is_numeric_column(rows, col)


def decide_visualization_node(state: AgentState) -> AgentState:
    """
    Chooses a chart type based purely on the shape of the ALREADY-EXECUTED
    SQL result. Never fabricates data — chart_data is always a direct
    (capped) slice of the real result rows. Follow-up queries generate a
    brand-new result and therefore a brand-new chart decision here; no
    stale chart data is ever reused.
    """
    logger.info("NODE: decide_visualization")

    state["chart_required"] = False
    state["chart_type"] = "none"
    state["chart_title"] = ""
    state["chart_x_column"] = None
    state["chart_y_column"] = None
    state["chart_data"] = []

    if state.get("execution_status") != "executed":
        return state

    results = state.get("execution_result", {}).get("results", [])
    if not results:
        return state

    first = results[0]
    columns = first.get("columns", [])
    rows = first.get("rows", [])

    logger.info(f"CHART COLUMNS: {columns}")

    if not rows or len(columns) < 1:
        logger.info("CHART DECISION: none (empty result / no columns)")
        return state

    question = state.get("standalone_question") or state.get("question", "")

    col_types = {}
    for c in columns:
        if is_datetime_column(c, rows, c):
            col_types[c] = "datetime"
        elif is_numeric_column(rows, c):
            col_types[c] = "numeric"
        elif is_categorical_column(rows, c):
            col_types[c] = "categorical"
        else:
            col_types[c] = "unknown"

    logger.info(f"COLUMN TYPES: {col_types}")

    if len(rows) == 1 and len(columns) == 1 and col_types[columns[0]] == "numeric":
        state["chart_required"] = True
        state["chart_type"] = "kpi"
        state["chart_title"] = question.strip().capitalize() or columns[0]
        state["chart_y_column"] = columns[0]
        state["chart_data"] = rows[:1]
        logger.info("CHART DECISION: kpi")
        logger.info("CHART TYPE: kpi")
        return state

    if len(columns) < 2:
        logger.info("CHART DECISION: none (insufficient columns)")
        return state

    if len(rows) > MAX_ROWS_FOR_CHART:
        logger.info("CHART DECISION: none (result too large)")
        return state

    chart_data = rows[:MAX_ROWS_FOR_CHART]

    datetime_cols = [c for c in columns if col_types[c] == "datetime"]
    numeric_cols = [c for c in columns if col_types[c] == "numeric"]
    categorical_cols = [c for c in columns if col_types[c] == "categorical"]

    if datetime_cols and numeric_cols:
        time_col = datetime_cols[0]
        value_col = numeric_cols[0]
        logger.info(f"DETECTED TIME COLUMN: {time_col}")
        logger.info(f"DETECTED NUMERIC COLUMN: {value_col}")

        state["chart_required"] = True
        state["chart_type"] = "line"
        state["chart_title"] = question.strip().capitalize() or f"{value_col} over {time_col}"
        state["chart_x_column"] = time_col
        state["chart_y_column"] = value_col
        state["chart_data"] = chart_data
        logger.info("CHART DECISION: time series")
        logger.info("CHART TYPE: line")
        return state

    if len(numeric_cols) >= 2 and not categorical_cols:
        col_a, col_b = numeric_cols[0], numeric_cols[1]
        state["chart_required"] = True
        state["chart_type"] = "scatter"
        state["chart_title"] = question.strip().capitalize() or f"{col_a} vs {col_b}"
        state["chart_x_column"] = col_a
        state["chart_y_column"] = col_b
        state["chart_data"] = chart_data
        logger.info("CHART DECISION: two numeric columns")
        logger.info("CHART TYPE: scatter")
        return state

    if categorical_cols and numeric_cols:
        cat_col = categorical_cols[0]
        num_col = numeric_cols[0]

        lowered_q = question.lower()
        part_to_whole_hint = any(
            k in lowered_q
            for k in ("share", "percentage", "percent", "proportion", "distribution", "composition")
        )

        if part_to_whole_hint and len(rows) <= 8:
            state["chart_required"] = True
            state["chart_type"] = "pie"
            state["chart_title"] = question.strip().capitalize() or f"{num_col} share by {cat_col}"
            state["chart_x_column"] = cat_col
            state["chart_y_column"] = num_col
            state["chart_data"] = chart_data
            logger.info("CHART DECISION: category + number (part-to-whole)")
            logger.info("CHART TYPE: pie")
            return state

        state["chart_required"] = True
        state["chart_type"] = "bar"
        state["chart_title"] = question.strip().capitalize() or f"{num_col} by {cat_col}"
        state["chart_x_column"] = cat_col
        state["chart_y_column"] = num_col
        state["chart_data"] = chart_data
        logger.info("CHART DECISION: category + number")
        logger.info("CHART TYPE: bar")
        return state

    logger.info("CHART DECISION: none (unsuitable shape)")
    logger.info("CHART TYPE: none")
    return state


# --------------------------------------------------------------------------
# final_response
# --------------------------------------------------------------------------

def final_response_node(state: AgentState) -> AgentState:
    logger.info("NODE: final_response")

    if state.get("needs_clarification"):
        state["execution_status"] = "needs_clarification"
        state["execution_result"] = {
            "status": "needs_clarification",
            "question": state.get(
                "clarification_question", "Could you clarify your request?"
            ),
        }
        return state

    if state.get("validation_result") == "invalid":
        state["execution_status"] = "rejected"
        state["execution_result"] = {
            "status": "rejected",
            "reason": state.get("validation_error", "SQL failed validation."),
            "statements": state.get("statements", []),
        }
        return state

    result = dict(state.get("execution_result", {}))

    if state.get("execution_status") == "executed":
        if state.get("final_answer"):
            result["explanation"] = state["final_answer"]

        if state.get("chart_required"):
            result["chart"] = {
                "chart_required": True,
                "chart_type": state.get("chart_type"),
                "chart_title": state.get("chart_title"),
                "chart_x_column": state.get("chart_x_column"),
                "chart_y_column": state.get("chart_y_column"),
                "chart_data": state.get("chart_data", []),
            }
        else:
            result["chart"] = {"chart_required": False, "chart_type": "none"}

    # Echo session_id back so the frontend can persist a server-generated
    # id on the first turn of a session.
    if state.get("session_id"):
        result["session_id"] = state["session_id"]

    state["execution_result"] = result
    return state


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

def _route_after_understand(state: AgentState) -> str:
    if state.get("needs_clarification"):
        return "final_response"
    return "retrieve_schema"


def _route_after_resolve_follow_up(state: AgentState) -> str:
    if state.get("needs_clarification"):
        return "final_response"
    return "retrieve_schema"


def _route_after_generate(state: AgentState) -> str:
    if state.get("needs_clarification") or not state.get("statements"):
        return "final_response"
    return "validate_sql"


def _route_after_validate(state: AgentState) -> str:
    if state.get("validation_result") == "valid":
        return "execute_sql"
    if state.get("validation_result") == "none":
        return "final_response"
    if state.get("correction_attempts", 0) >= MAX_CORRECTION_ATTEMPTS:
        return "final_response"
    return "correct_sql"


def _route_after_execute(state: AgentState) -> str:
    if state.get("execution_status") == "executed":
        return "analyze_result"
    return "final_response"


# --------------------------------------------------------------------------
# Graph construction
# --------------------------------------------------------------------------

def build_agent():
    graph = StateGraph(AgentState)

    graph.add_node("load_memory", load_memory_node)
    graph.add_node("understand_question", understand_question)
    graph.add_node("resolve_follow_up", resolve_follow_up_node)
    graph.add_node("retrieve_schema", retrieve_schema_node)
    graph.add_node("generate_sql", generate_sql_node)
    graph.add_node("validate_sql", validate_sql_node)
    graph.add_node("correct_sql", correct_sql_node)
    graph.add_node("execute_sql", execute_sql_node)
    graph.add_node("analyze_result", analyze_result_node)
    graph.add_node("decide_visualization", decide_visualization_node)
    graph.add_node("final_response", final_response_node)
    graph.add_node("save_memory", save_memory_node)

    graph.set_entry_point("load_memory")

    graph.add_edge("load_memory", "understand_question")

    graph.add_conditional_edges(
        "understand_question",
        _route_after_understand,
        {"retrieve_schema": "resolve_follow_up", "final_response": "final_response"},
    )

    graph.add_conditional_edges(
        "resolve_follow_up",
        _route_after_resolve_follow_up,
        {"retrieve_schema": "retrieve_schema", "final_response": "final_response"},
    )

    graph.add_edge("retrieve_schema", "generate_sql")

    graph.add_conditional_edges(
        "generate_sql",
        _route_after_generate,
        {"validate_sql": "validate_sql", "final_response": "final_response"},
    )

    graph.add_conditional_edges(
        "validate_sql",
        _route_after_validate,
        {
            "execute_sql": "execute_sql",
            "correct_sql": "correct_sql",
            "final_response": "final_response",
        },
    )

    graph.add_edge("correct_sql", "validate_sql")

    graph.add_conditional_edges(
        "execute_sql",
        _route_after_execute,
        {"analyze_result": "analyze_result", "final_response": "final_response"},
    )

    graph.add_edge("analyze_result", "decide_visualization")
    graph.add_edge("decide_visualization", "final_response")
    graph.add_edge("final_response", "save_memory")
    graph.add_edge("save_memory", END)

    return graph.compile()


_compiled_agent = None


def get_agent():
    global _compiled_agent
    if _compiled_agent is None:
        _compiled_agent = build_agent()
    return _compiled_agent


def run_agent(db_id: str, question: str, schema: dict, db_path: str,
              session_id: str = None) -> dict:
    logger.info("AGENT START")
    logger.info(f"DB ID: {db_id}")
    logger.info(f"QUESTION: {question}")

    initial_state: AgentState = {
        "db_id": db_id,
        "question": question,
        "schema": schema,
        "db_path": db_path,
        "session_id": session_id or "",
        "correction_attempts": 0,
        "needs_clarification": False,
    }

    agent = get_agent()
    final_state = agent.invoke(initial_state)

    return final_state.get("execution_result", {
        "status": "error",
        "error": "Agent did not produce a result.",
    })
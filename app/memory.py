"""
Conversation memory for Eng2SQL.

Isolated strictly by (session_id, db_id). Never mixes context across
databases. Persisted to a local SQLite file so memory survives normal
process/page refreshes (per spec: not a plain Python global).

Public functions:
    init_memory_db()
    save_message(session_id, db_id, role, content, timestamp=None)
    save_exchange(session_id, db_id, user_question, standalone_question,
                  assistant_answer, sql=None)
    get_recent_history(session_id, db_id, limit=20)
    resolve_follow_up(session_id, db_id, question, llm_invoke_fn)
"""

import logging
import os
import sqlite3
import json
from datetime import datetime, timezone

logger = logging.getLogger("memory")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR = os.path.join(BASE_DIR, "data", "memory")
os.makedirs(MEMORY_DIR, exist_ok=True)

MEMORY_DB_PATH = os.path.join(MEMORY_DIR, "conversation_memory.sqlite")

DEFAULT_WINDOW = 20  # last N messages sent to Mistral for resolution


# --------------------------------------------------------------------------
# Storage init
# --------------------------------------------------------------------------

def init_memory_db() -> None:
    """Creates the memory table if it doesn't already exist. Idempotent."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                db_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                standalone_question TEXT,
                sql TEXT,
                timestamp TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_session_db
            ON conversation_memory (session_id, db_id, id)
            """
        )
        conn.commit()
    finally:
        conn.close()


init_memory_db()


# --------------------------------------------------------------------------
# Save
# --------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_message(session_id: str, db_id: str, role: str, content: str,
                  standalone_question: str = None, sql: str = None,
                  timestamp: str = None) -> None:
    """
    Persists a single memory item. Never stores secrets/API keys — callers
    must only pass conversational content (questions, answers, SQL text).
    """
    if not session_id or not db_id:
        logger.warning("save_message skipped: missing session_id or db_id")
        return

    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            """
            INSERT INTO conversation_memory
                (session_id, db_id, role, content, standalone_question, sql, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                db_id,
                role,
                content or "",
                standalone_question,
                sql,
                timestamp or _now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def save_exchange(session_id: str, db_id: str, user_question: str,
                   standalone_question: str, assistant_answer: str,
                   sql: str = None) -> None:
    """
    Convenience wrapper: saves both sides of one turn (user question +
    assistant answer), tagging the user message with the resolved
    standalone question and (if useful) the generated SQL.
    """
    save_message(
        session_id, db_id, role="user", content=user_question,
        standalone_question=standalone_question, sql=sql,
    )
    save_message(
        session_id, db_id, role="assistant", content=assistant_answer or "",
    )


# --------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------

def get_recent_history(session_id: str, db_id: str, limit: int = DEFAULT_WINDOW) -> list:
    """
    Returns the most recent `limit` messages for this exact
    (session_id, db_id) pair, oldest-first, as a list of
    {"role": ..., "content": ...} dicts. Strictly isolated by db_id —
    switching databases changes the active context entirely.
    """
    if not session_id or not db_id:
        return []

    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            """
            SELECT role, content, standalone_question, sql, timestamp
            FROM conversation_memory
            WHERE session_id = ? AND db_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, db_id, limit),
        )
        rows = [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

    rows.reverse()  # oldest-first for prompt construction
    return rows


# --------------------------------------------------------------------------
# Follow-up resolution
# --------------------------------------------------------------------------

def _format_history_for_prompt(history: list) -> str:
    """Build a compact but useful conversation context for follow-up resolution.

    User turns also carry the resolved standalone question and generated SQL
    when available. This is important for follow-ups such as "sort them",
    "only keep the previous ones", or "make that monthly" because the raw
    assistant summary may not contain the exact entities/filters from the
    previous turn.
    """
    if not history:
        return "(no previous conversation)"

    lines = []
    for item in history:
        role = "User" if item["role"] == "user" else "Assistant"
        lines.append(f"{role}: {item.get('content', '')}")

        if role == "User":
            standalone = item.get("standalone_question")
            sql = item.get("sql")
            if standalone and standalone != item.get("content"):
                lines.append(f"Resolved question: {standalone}")
            if sql:
                lines.append(f"SQL used: {sql}")

    return "\n".join(lines)


def _looks_like_follow_up(question: str) -> bool:
    """
    Cheap heuristic used only to decide whether it's worth calling Mistral
    at all — not the final say. Even if this misses, an unnecessary
    rewrite call is harmless since the prompt instructs 'return unchanged
    if already standalone'.
    """
    q = (question or "").strip().lower()
    if not q:
        return False
    follow_up_markers = (
        "them", "those", "these", "it", "he", "she", "his", "her", "their",
        "that one", "same", "only", "also", "and also", "what about",
        "sort them", "sort those", "filter them", "how many of",
    )
    return any(marker in q for marker in follow_up_markers)


def resolve_follow_up(session_id: str, db_id: str, question: str, llm_invoke_fn) -> dict:
    """
    Converts an ambiguous follow-up question into a standalone question
    using recent conversation history + Mistral (via llm_invoke_fn, a
    callable taking a prompt string and returning an object with
    `.content`). Reuses the EXISTING Mistral client from llm.py — this
    module does not construct its own LLM client.

    Returns:
        {
            "standalone_question": str,
            "needs_clarification": bool,
            "clarification_question": str | None,
        }

    If the question is already standalone, or history is empty, the
    original question is returned unchanged without invoking the LLM.
    """
    question = (question or "").strip()

    history = get_recent_history(session_id, db_id, limit=DEFAULT_WINDOW)

    logger.info(f"CONVERSATION HISTORY LENGTH: {len(history)}")

    if not history:
        # No prior context. If the question is clearly a bare follow-up
        # ("show those ones") with nothing to resolve against, ask for
        # clarification rather than guessing.
        if _looks_like_follow_up(question) and _is_ambiguous_without_context(question):
            return {
                "standalone_question": question,
                "needs_clarification": True,
                "clarification_question": "Could you clarify which records you mean?",
            }
        return {
            "standalone_question": question,
            "needs_clarification": False,
            "clarification_question": None,
        }

    # IMPORTANT: Do not rely on a small keyword heuristic here.
    # A real conversational follow-up does not always contain words like
    # "them", "those", or "what about". Examples such as:
    #   "Show the top 10 customers"
    #   "Now only Delhi"
    #   "Make it monthly"
    #   "And sort by revenue"
    # depend on the previous turn even though the heuristic may miss them.
    # Once history exists, always give the resolver the conversation so the
    # LLM can decide whether the new question is standalone or contextual.
    # This is what makes the chat genuinely multi-turn rather than merely
    # pronoun-aware.

    history_text = _format_history_for_prompt(history)

    prompt = (
        "You are resolving a follow-up question in a database-querying "
        "conversation into a standalone question.\n\n"
        "RULES:\n"
        "- Use the conversation context below to resolve pronouns and "
        "implicit references (them, those, it, he/she, same, previous result).\n"
        "- Preserve the user's intent exactly.\n"
        "- Do NOT invent conditions, filters, or values not present in the "
        "conversation or the new question.\n"
        "- Preserve the current database context — do not reference any "
        "other database.\n"
        "- If the new question is already standalone (does not depend on "
        "prior context), return it UNCHANGED.\n"
        "- If the new question is too ambiguous to resolve even with this "
        "context, respond with exactly: CLARIFY\n"
        "- Return ONLY the standalone question text (or CLARIFY). No extra "
        "commentary, no quotes, no markdown.\n\n"
        f"Conversation so far:\n{history_text}\n\n"
        f"New question: {question}\n\n"
        "Standalone question:"
    )

    try:
        response = llm_invoke_fn(prompt)
        rewritten = (getattr(response, "content", "") or "").strip()
    except Exception as e:
        logger.error(f"Follow-up resolution failed, using original question: {e}")
        rewritten = question

    if not rewritten:
        rewritten = question

    if rewritten.strip().upper() == "CLARIFY":
        return {
            "standalone_question": question,
            "needs_clarification": True,
            "clarification_question": "Could you clarify which records you mean?",
        }

    return {
        "standalone_question": rewritten,
        "needs_clarification": False,
        "clarification_question": None,
    }


def _is_ambiguous_without_context(question: str) -> bool:
    """
    True only for genuinely bare references with no other content to act
    on (e.g. 'show those ones', 'only them') — not for questions that
    happen to contain a pronoun-ish word but stand fine on their own.
    """
    q = question.strip().lower().rstrip(".!? ")
    bare_patterns = (
        "show those ones", "show those", "show them", "only those",
        "only them", "those ones", "same ones", "sort them", "filter them",
    )
    return q in bare_patterns
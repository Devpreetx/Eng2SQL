"""
Conversation memory for Eng2SQL.

Isolated strictly by (session_id, db_id). Never mixes context across
databases. Persisted to a local SQLite file so memory survives normal
process/page refreshes (per spec: not a plain Python global).

Public functions:
    init_memory_db()
    save_exchange(session_id, db_id, user_question, standalone_question,
                  assistant_answer, sql=None)
    get_recent_history(session_id, db_id, limit=20)
    delete_for_db(db_id)
    resolve_follow_up(question, history, llm_invoke_fn)
"""

import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from app.config import MEMORY_DIR

logger = logging.getLogger("memory")

MEMORY_DB_PATH = os.path.join(MEMORY_DIR, "conversation_memory.sqlite")

DEFAULT_WINDOW = 20  # last N messages sent to the LLM for resolution

# Only the last DEFAULT_WINDOW messages are ever read back, so older rows
# of a conversation are trimmed once it grows past this many.
MAX_STORED_MESSAGES = 200

CLARIFY_MESSAGE = "Could you clarify which records you mean?"


# --------------------------------------------------------------------------
# Storage init
# --------------------------------------------------------------------------

def init_memory_db() -> None:
    """Creates the memory table if it doesn't already exist. Idempotent."""
    with closing(sqlite3.connect(MEMORY_DB_PATH)) as conn, conn:
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


init_memory_db()


# --------------------------------------------------------------------------
# Save
# --------------------------------------------------------------------------

def save_exchange(session_id: str, db_id: str, user_question: str,
                   standalone_question: str, assistant_answer: str,
                   sql: str = None) -> None:
    """
    Saves both sides of one turn (user question + assistant answer) in a
    single transaction, tagging the user message with the resolved
    standalone question and (if useful) the generated SQL. Never stores
    secrets/API keys — callers only pass conversational content.
    """
    if not session_id or not db_id:
        logger.warning("save_exchange skipped: missing session_id or db_id")
        return

    now = datetime.now(timezone.utc).isoformat()

    with closing(sqlite3.connect(MEMORY_DB_PATH)) as conn, conn:
        conn.executemany(
            """
            INSERT INTO conversation_memory
                (session_id, db_id, role, content, standalone_question, sql, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (session_id, db_id, "user", user_question or "", standalone_question, sql, now),
                (session_id, db_id, "assistant", assistant_answer or "", None, None, now),
            ],
        )
        conn.execute(
            """
            DELETE FROM conversation_memory
            WHERE session_id = ? AND db_id = ? AND id NOT IN (
                SELECT id FROM conversation_memory
                WHERE session_id = ? AND db_id = ?
                ORDER BY id DESC LIMIT ?
            )
            """,
            (session_id, db_id, session_id, db_id, MAX_STORED_MESSAGES),
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

    with closing(sqlite3.connect(MEMORY_DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT role, content, standalone_question, sql, timestamp
            FROM conversation_memory
            WHERE session_id = ? AND db_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, db_id, limit),
        ).fetchall()

    return [dict(r) for r in reversed(rows)]  # oldest-first for prompt construction


def delete_for_db(db_id: str) -> None:
    """Forget every conversation about a database that is being deleted."""
    with closing(sqlite3.connect(MEMORY_DB_PATH)) as conn, conn:
        conn.execute("DELETE FROM conversation_memory WHERE db_id = ?", (db_id,))


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


def resolve_follow_up(question: str, history: list, llm_invoke_fn) -> dict:
    """
    Converts an ambiguous follow-up question into a standalone question
    using the recent conversation `history` (from get_recent_history) and
    the LLM (via llm_invoke_fn, a callable taking a prompt string and
    returning the reply text). Reuses the EXISTING client from llm.py —
    this module does not construct its own LLM client.

    Returns:
        {
            "standalone_question": str,
            "needs_clarification": bool,
            "clarification_question": str | None,
        }

    If history is empty, the original question is returned unchanged
    without invoking the LLM.
    """
    question = (question or "").strip()

    def result(standalone: str, clarify: bool = False) -> dict:
        return {
            "standalone_question": standalone,
            "needs_clarification": clarify,
            "clarification_question": CLARIFY_MESSAGE if clarify else None,
        }

    if not history:
        # No prior context. If the question is clearly a bare follow-up
        # ("show those ones") with nothing to resolve against, ask for
        # clarification rather than guessing.
        return result(question, clarify=_is_ambiguous_without_context(question))

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
        f"Conversation so far:\n{_format_history_for_prompt(history)}\n\n"
        f"New question: {question}\n\n"
        "Standalone question:"
    )

    try:
        rewritten = (llm_invoke_fn(prompt) or "").strip()
    except Exception as e:
        logger.error(f"Follow-up resolution failed, using original question: {e}")
        rewritten = ""

    if not rewritten:
        return result(question)

    if rewritten.upper() == "CLARIFY":
        return result(question, clarify=True)

    return result(rewritten)

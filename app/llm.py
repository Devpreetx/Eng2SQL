"""
English -> SQL generation via Groq (LangChain).

This module only GENERATES SQL — it never executes anything.
Validation, previews, execution, and backups live in db_utils.py.

generate_sql() sends the model only the schema context relevant to the
question (retrieved via app.rag), never the whole database.

Features:
- RAG-based schema retrieval
- SQLite-safe SQL generation
- Multi-statement support
- JSON-only output
- Groq retry handling
- Friendly rate-limit/API error handling
- Explicit full-schema fallback
"""

import json
import logging
import re
import threading
import time

from langchain_core.prompts import ChatPromptTemplate

from app import rag
from app.config import GROQ_MODEL
from app.db_utils import split_statements

logger = logging.getLogger("llm")

# Give up on a single Groq call after this many seconds.
REQUEST_TIMEOUT_S = 60

_llm = None
_llm_lock = threading.Lock()


def get_llm():
    """
    Lazily construct the Groq client, so the server can start (and serve
    the UI, table previews and the SQL Workspace) without a GROQ_API_KEY.
    """
    global _llm

    with _llm_lock:
        if _llm is None:
            from langchain_groq import ChatGroq

            _llm = ChatGroq(
                model=GROQ_MODEL,
                temperature=0,
                # Retries are handled by _invoke_llm below; letting the SDK
                # retry as well would multiply the attempts.
                max_retries=0,
                timeout=REQUEST_TIMEOUT_S,
            )

    return _llm


# ============================================================
# PROMPT
# ============================================================

prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """
You are a SQL generation assistant for a SQLite database.

The schema below is RETRIEVED CONTEXT — a relevant subset of the
full database schema selected for this specific question, NOT the
entire database.

Only these tables and columns are known to exist for this request,
UNLESS the user is asking you to CREATE a new table.

Never invent tables, columns, or relationships that are not listed
in the retrieved schema below and were not just created earlier in
the SAME request.

If the retrieved schema does not contain what the question needs,
for example a referenced column or table is missing, do NOT guess
or invent the structure.

Instead set:

"needs_clarification": true

and explain what is missing.

Relevant database schema:

{retrieved_schema}


SQL RULES:

- Use valid SQLite syntax.

- CREATE TABLE is allowed.

- DROP, ALTER, TRUNCATE, PRAGMA, ATTACH, DETACH, VACUUM,
  GRANT, and REVOKE are NEVER allowed.

- Never generate those forbidden statements.

- For UPDATE or DELETE, ALWAYS include a WHERE clause that is
  as specific as possible.

- Never generate UPDATE or DELETE without WHERE.

- If the request requires MULTIPLE steps, return EACH step as
  its own entry in the "statements" list.

- Statements must be returned in the exact order they must run.

- Each entry in "statements" must contain exactly ONE SQL statement.

- Do NOT put multiple SQL statements inside one string.

- If a later statement uses a table created by an earlier
  statement in the same request, that is allowed.

- If the request is ambiguous, references something that does
  not exist, or is missing required information, set:

  "needs_clarification": true

- In that case explain what is missing in
  "clarification_question".

- When clarification is required, "statements" must be an
  empty list.

- If the user asks to see/list the tables themselves, generate:

  SELECT name FROM sqlite_master WHERE type='table';


IMPORTANT:

Return ONLY valid JSON.

Do NOT use markdown code fences.

Do NOT add any explanation outside the JSON.

Return exactly this structure:

{{
    "statements": ["SQL statement 1", "SQL statement 2"],
    "intent": "read/insert/update/delete/create/other",
    "target_table": "table name(s) involved, comma separated",
    "explanation": "short explanation",
    "needs_clarification": false,
    "clarification_question": ""
}}
""",
        ),
        (
            "human",
            """
Question:

{question}
""",
        ),
    ]
)


def _extract_json(raw_text: str) -> str:
    """
    Removes markdown code fences and extracts the JSON object
    even if Groq adds text around it.
    """
    if not raw_text:
        return ""

    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```$", "", text).strip()

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end > start:
        text = text[start:end + 1]

    return text.strip()


def _clarification_result(message: str) -> dict:
    """
    Returns a safe clarification response instead of exposing
    internal errors to the frontend.
    """
    return {
        "statements": [],
        "intent": "other",
        "target_table": "",
        "explanation": "",
        "needs_clarification": True,
        "clarification_question": message,
    }


# ============================================================
# GROQ INVOCATION WITH RETRY
# ============================================================

_RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limit", "too many requests")
_UNAVAILABLE_MARKERS = ("503", "service unavailable", "temporarily unavailable")
_AUTH_MARKERS = ("401", "api key", "api_key", "authentication", "unauthorized")
_QUOTA_MARKERS = ("quota", "insufficient", "billing")


def _matches(error: Exception, markers: tuple) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in markers)


def _invoke_llm(final_prompt, max_attempts: int = 3):
    """
    Calls Groq with retry handling for temporary rate-limit
    and service errors.

    Retry sequence:

        Attempt 1 -> immediate
        Attempt 2 -> wait 2 seconds
        Attempt 3 -> wait 4 seconds

    Other errors are raised immediately.
    """
    llm = get_llm()

    for attempt in range(1, max_attempts + 1):
        try:
            return llm.invoke(final_prompt)
        except Exception as e:
            retryable = _matches(e, _RATE_LIMIT_MARKERS + _UNAVAILABLE_MARKERS)

            if not retryable or attempt == max_attempts:
                raise

            wait_time = 2 ** attempt
            logger.warning(
                f"Groq temporary/rate-limit error (attempt {attempt}/{max_attempts}), "
                f"retrying in {wait_time}s."
            )
            time.sleep(wait_time)


def invoke_text(prompt_text: str) -> str:
    """One-shot completion for a plain-text prompt; returns the reply text."""
    response = _invoke_llm(prompt_text)
    return (response.content or "").strip()


def _friendly_api_error(error: Exception) -> str | None:
    """A user-facing message for known Groq failure modes, else None."""
    if _matches(error, _RATE_LIMIT_MARKERS):
        return (
            "The Groq AI service is temporarily rate-limited. "
            "Please wait a little and try again."
        )

    if _matches(error, _AUTH_MARKERS):
        return (
            "The Groq API key is missing, invalid or expired. "
            "Please check your GROQ_API_KEY."
        )

    if _matches(error, _QUOTA_MARKERS):
        return (
            "The Groq API usage limit has been reached. "
            "Please check your Groq API usage and limits."
        )

    return None


# ============================================================
# MAIN SQL GENERATION
# ============================================================

def _schema_context(
    db_id: str,
    question: str,
    schema: dict | None,
    retrieved: dict | None,
) -> str | None:
    """
    The schema text to ground the model with: the retrieved table
    documents, or the full schema when retrieval produced nothing.
    Returns None when there is no schema information at all.
    """
    if retrieved is None:
        try:
            retrieved = rag.retrieve_schema(db_id, question, schema=schema)
        except Exception as e:
            logger.error(f"RAG retrieval failed for db_id={db_id}: {e}")
            retrieved = {}

    contents = [
        doc["content"]
        for doc in retrieved.get("documents", [])
        if isinstance(doc, dict) and doc.get("content")
    ]

    if contents:
        return "\n\n---\n\n".join(contents)

    if schema:
        logger.info(f"Database ID: {db_id} — RAG empty, falling back to full schema.")
        return json.dumps(schema, indent=2, default=str)

    if schema is not None:
        # A brand-new database: nothing to read yet, but CREATE TABLE is valid.
        return "(This database has no tables yet.)"

    return None


def generate_sql(
    db_id: str,
    question: str,
    schema: dict | None = None,
    retrieved: dict | None = None,
) -> dict:
    """
    Generate SQL from an English question.

    Parameters
    ----------
    db_id:
        Active database ID.

    question:
        User's English question.

    schema:
        Optional full schema dictionary. Used as a safe fallback when
        RAG retrieval fails or returns no useful schema.

    retrieved:
        Optional result of rag.retrieve_schema() the caller already has,
        so the same retrieval isn't run twice for one question.

    Returns
    -------
    dict

    {
        "statements": [...],
        "intent": "...",
        "target_table": "...",
        "explanation": "...",
        "needs_clarification": false,
        "clarification_question": ""
    }
    """
    if not db_id:
        raise ValueError("db_id is required for SQL generation.")

    if not question or not question.strip():
        return _clarification_result("Please enter a question for the database.")

    schema_text = _schema_context(db_id, question, schema, retrieved)

    if schema_text is None:
        return _clarification_result(
            "I couldn't find any relevant tables for that question. "
            "Could you clarify which table or data you're asking about?"
        )

    final_prompt = prompt.invoke(
        {
            "retrieved_schema": schema_text,
            "question": question,
        }
    )

    try:
        response = _invoke_llm(final_prompt)
    except Exception as e:
        message = _friendly_api_error(e)
        if message:
            logger.error(f"Groq call failed: {message}")
            return _clarification_result(message)

        logger.exception("Groq SQL generation failed.")
        raise

    try:
        result = json.loads(_extract_json(response.content))
    except json.JSONDecodeError:
        logger.error(f"Groq returned invalid JSON. Raw response: {response.content}")
        raise RuntimeError(f"LLM did not return valid JSON:\n{response.content}")

    if not isinstance(result, dict):
        raise RuntimeError(f"LLM did not return a JSON object:\n{response.content}")

    result.setdefault("statements", [])
    result.setdefault("intent", "other")
    result.setdefault("target_table", "")
    result.setdefault("explanation", "")
    result.setdefault("needs_clarification", False)
    result.setdefault("clarification_question", "")

    statements = result["statements"]
    if not isinstance(statements, list):
        statements = [str(statements)]

    # Defensive cleanup: drop trailing semicolons, and if the model put
    # several statements in one string, split them.
    result["statements"] = [
        part
        for stmt in statements
        if isinstance(stmt, str)
        for part in split_statements(stmt)
    ]

    logger.info(f"Generated SQL: {result['statements']} (intent={result['intent']})")

    return result

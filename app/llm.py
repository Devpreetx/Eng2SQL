"""
English -> SQL generation via Groq (LangChain).

This module only GENERATES SQL — it never executes anything.
Validation, previews, execution, and backups live in db_utils.py.

PHASE 2:
generate_sql() takes db_id + question, retrieves relevant schema
via app.rag.retrieve_schema(), and sends only the retrieved schema
context to Groq.

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
import time

from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate

from app import rag


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger("llm")
logger.setLevel(logging.INFO)

if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter(
            "[%(levelname)s] %(name)s: %(message)s"
        )
    )
    logger.addHandler(_handler)


# ============================================================
# GROQ LLM
# ============================================================

llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0,
    max_retries=2,
)

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


# ============================================================
# JSON EXTRACTION
# ============================================================

def _extract_json(raw_text: str) -> str:
    """
    Removes markdown code fences and extracts the JSON object
    even if Groq adds text around it.
    """

    if not raw_text:
        return ""

    text = raw_text.strip()

    # Remove markdown code fences
    text = re.sub(
        r"^```(?:json)?",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"```$",
        "",
        text,
    )

    text = text.strip()

    # Extract JSON object
    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    return text.strip()


# ============================================================
# CLARIFICATION RESULT
# ============================================================

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

    last_error = None

    for attempt in range(max_attempts):

        try:

            logger.info(
                f"Calling Groq "
                f"(attempt {attempt + 1}/{max_attempts})"
            )

            response = llm.invoke(final_prompt)

            logger.info(
                "Groq response received successfully."
            )

            return response

        except Exception as e:

            last_error = e
            error_text = str(e).lower()

            is_retryable = (
                "429" in error_text
                or "rate limit" in error_text
                or "rate_limit" in error_text
                or "too many requests" in error_text
                or "503" in error_text
                or "service unavailable" in error_text
                or "temporarily unavailable" in error_text
            )

            # Don't retry unrelated errors
            if not is_retryable:
                raise

            logger.warning(
                f"Groq temporary/rate-limit error "
                f"(attempt {attempt + 1}/{max_attempts})."
            )

            if attempt < max_attempts - 1:

                wait_time = 2 ** (attempt + 1)

                logger.warning(
                    f"Waiting {wait_time} seconds "
                    f"before retry..."
                )

                time.sleep(wait_time)

    raise last_error


# ============================================================
# MAIN SQL GENERATION
# ============================================================

def generate_sql(
    db_id: str,
    question: str,
    schema: dict | None = None,
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
        Optional full schema dictionary.

        It is only used as a safe fallback when RAG retrieval
        fails or returns no useful schema.

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

    # ========================================================
    # VALIDATION
    # ========================================================

    if not db_id:
        raise ValueError(
            "db_id is required for SQL generation."
        )

    if not question or not question.strip():
        return _clarification_result(
            "Please enter a question for the database."
        )

    logger.info("==========================================")
    logger.info("RAG → GROQ SQL GENERATION")
    logger.info("==========================================")

    logger.info(
        f"Database ID: {db_id}"
    )

    logger.info(
        f"Question: {question}"
    )


    # ========================================================
    # RAG SCHEMA RETRIEVAL
    # ========================================================

    retrieved_tables = []
    retrieved_schema_text = None

    try:

        rag_result = rag.retrieve_schema(
            db_id,
            question,
            k=5,
        )

        retrieved_tables = rag_result.get(
            "tables",
            [],
        )

        documents = rag_result.get(
            "documents",
            [],
        )

        if documents:

            valid_documents = [
                doc
                for doc in documents
                if isinstance(doc, dict)
                and doc.get("content")
            ]

            if valid_documents:

                retrieved_schema_text = (
                    "\n\n---\n\n".join(
                        doc["content"]
                        for doc in valid_documents
                    )
                )

    except Exception as e:

        logger.error(
            f"RAG retrieval failed for "
            f"db_id={db_id}: {e}"
        )

        retrieved_tables = []
        retrieved_schema_text = None


    logger.info(
        f"Retrieved tables: {retrieved_tables}"
    )


    # ========================================================
    # SCHEMA FALLBACK
    # ========================================================

    if not retrieved_schema_text:

        if schema:

            logger.info(
                f"Database ID: {db_id} — "
                f"RAG empty, falling back to full schema."
            )

            retrieved_schema_text = json.dumps(
                schema,
                indent=2,
                default=str,
            )

        else:

            logger.info(
                f"Database ID: {db_id} — "
                f"no schema available."
            )

            return _clarification_result(
                "I couldn't find any relevant tables for "
                "that question. Could you clarify which "
                "table or data you're asking about?"
            )


    # ========================================================
    # BUILD PROMPT
    # ========================================================

    final_prompt = prompt.invoke(
        {
            "retrieved_schema": retrieved_schema_text,
            "question": question,
        }
    )


    # ========================================================
    # CALL GROQ
    # ========================================================

    try:

        response = _invoke_llm(
            final_prompt,
            max_attempts=3,
        )

    except Exception as e:

        error_text = str(e)
        error_lower = error_text.lower()

        # ----------------------------------------------------
        # RATE LIMIT
        # ----------------------------------------------------

        if (
            "429" in error_lower
            or "rate limit" in error_lower
            or "rate_limit" in error_lower
            or "too many requests" in error_lower
        ):

            logger.error(
                "Groq rate limit persisted after retries."
            )

            return _clarification_result(
                "The Groq AI service is temporarily "
                "rate-limited. Please wait a little "
                "and try again."
            )

        # ----------------------------------------------------
        # INVALID API KEY
        # ----------------------------------------------------

        if (
            "401" in error_lower
            or "api key" in error_lower
            or "authentication" in error_lower
            or "unauthorized" in error_lower
            or "invalid_api_key" in error_lower
        ):

            logger.error(
                "Groq API authentication failed."
            )

            return _clarification_result(
                "The Groq API key is invalid or expired. "
                "Please check your GROQ_API_KEY."
            )

        # ----------------------------------------------------
        # QUOTA
        # ----------------------------------------------------

        if (
            "quota" in error_lower
            or "limit" in error_lower
        ):

            logger.error(
                "Groq API quota/usage limit reached."
            )

            return _clarification_result(
                "The Groq API usage limit has been reached. "
                "Please check your Groq API usage and limits."
            )

        # ----------------------------------------------------
        # OTHER ERROR
        # ----------------------------------------------------

        logger.exception(
            "Groq SQL generation failed."
        )

        raise


    # ========================================================
    # EXTRACT MODEL RESPONSE
    # ========================================================

    content = _extract_json(
        response.content
    )


    # ========================================================
    # PARSE JSON
    # ========================================================

    try:

        result = json.loads(
            content
        )

    except json.JSONDecodeError:

        logger.error(
            "Groq returned invalid JSON."
        )

        logger.error(
            f"Raw response: {response.content}"
        )

        raise RuntimeError(
            "LLM did not return valid JSON:\n"
            f"{response.content}"
        )


    # ========================================================
    # ENSURE REQUIRED FIELDS
    # ========================================================

    result.setdefault(
        "statements",
        [],
    )

    result.setdefault(
        "intent",
        "other",
    )

    result.setdefault(
        "target_table",
        "",
    )

    result.setdefault(
        "explanation",
        "",
    )

    result.setdefault(
        "needs_clarification",
        False,
    )

    result.setdefault(
        "clarification_question",
        "",
    )


    # ========================================================
    # VALIDATE STATEMENTS
    # ========================================================

    if not isinstance(
        result["statements"],
        list,
    ):

        result["statements"] = [
            str(result["statements"])
        ]


    # ========================================================
    # CLEAN SQL STATEMENTS
    # ========================================================

    fixed_statements = []

    for stmt in result["statements"]:

        if not isinstance(
            stmt,
            str,
        ):
            continue

        stmt = stmt.strip()

        if not stmt:
            continue

        # Remove trailing semicolon
        stmt = stmt.rstrip(";").strip()

        if not stmt:
            continue

        # Defensive cleanup:
        # If Groq accidentally places multiple SQL statements
        # in one string, split them.
        parts = [
            part.strip()
            for part in stmt.split(";")
            if part.strip()
        ]

        fixed_statements.extend(
            parts
        )


    result["statements"] = fixed_statements


    # ========================================================
    # FINAL LOGGING
    # ========================================================

    logger.info(
        f"Generated SQL: "
        f"{result['statements']}"
    )

    logger.info(
        f"Intent: {result['intent']}"
    )

    logger.info(
        f"Target table: "
        f"{result['target_table']}"
    )

    logger.info(
        "=========================================="
    )


    return result
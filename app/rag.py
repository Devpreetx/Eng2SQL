"""
RAG (schema retrieval) module for Eng2SQL.

Scope: retrieve only the RELEVANT schema documents (per-table) for a given
question, restricted to a single db_id, before SQL generation.

This module does NOT execute SQL and does NOT call the LLM for SQL
generation. It only builds/queries a persistent local vector store of
schema documents.

Public functions:
    build_schema_documents(db_id, schema)
    index_schema(db_id, schema)
    retrieve_schema(db_id, question, k=5)
    refresh_schema_index(db_id, schema)
"""

import logging
import os

from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma

logger = logging.getLogger("rag")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)

# --------------------------------------------------------------------------
# Config (isolated here, per instructions)
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VECTORSTORE_DIR = os.path.join(BASE_DIR, "data", "vectorstore")
os.makedirs(VECTORSTORE_DIR, exist_ok=True)

EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

COLLECTION_NAME = "schema_docs"

SAMPLE_ROWS_IN_DOC = 3  # cap on how many sample rows go into a document

_embeddings = None
_vectorstore = None


def _get_embeddings():
    """Lazily construct the local embedding model (no OpenAI required)."""
    global _embeddings
    if _embeddings is None:
        _embeddings = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL_NAME)
    return _embeddings


def _get_vectorstore():
    """Lazily construct the persistent Chroma vector store."""
    global _vectorstore
    if _vectorstore is None:
        try:
            _vectorstore = Chroma(
                collection_name=COLLECTION_NAME,
                embedding_function=_get_embeddings(),
                persist_directory=VECTORSTORE_DIR,
            )
        except Exception as e:
            logger.error(f"RAG VECTORSTORE INIT FAILED: {e}")
            raise RuntimeError(f"Could not initialize vector store: {e}")
    return _vectorstore


# --------------------------------------------------------------------------
# 1. Schema documents
# --------------------------------------------------------------------------

def _format_column(col: dict) -> str:
    name = col.get("name", "")
    dtype = col.get("type", "")
    pk = " PRIMARY KEY" if col.get("primary_key") else ""
    return f"{name} {dtype}{pk}".strip()


def _format_foreign_keys(fks: list) -> str:
    if not fks:
        return "None"
    lines = []
    for fk in fks:
        col = fk.get("column", "")
        ref_table = fk.get("ref_table", "")
        ref_col = fk.get("ref_column", "")
        lines.append(f"{col} -> {ref_table}.{ref_col}")
    return "\n".join(lines)


def _format_sample_rows(rows: list) -> str:
    if not rows:
        return "None"
    limited = rows[:SAMPLE_ROWS_IN_DOC]
    lines = [str(r) for r in limited]
    return "\n".join(lines)


def build_schema_documents(db_id: str, schema: dict) -> list:
    """
    Build one Document per table from an extract_schema()-shaped dict:
        {table_name: {"columns": [...], "foreign_keys": [...], "sample_rows": [...]}}

    Only schema metadata + a small sample of rows is embedded — never the
    full dataset.
    """
    if not db_id:
        raise ValueError("db_id is required to build schema documents.")

    if not schema:
        return []

    documents = []

    for table_name, table_info in schema.items():
        columns = table_info.get("columns", [])
        fks = table_info.get("foreign_keys", [])
        samples = table_info.get("sample_rows", [])

        columns_text = "\n".join(_format_column(c) for c in columns) or "None"
        fks_text = _format_foreign_keys(fks)
        samples_text = _format_sample_rows(samples)

        content = (
            f"Database ID: {db_id}\n"
            f"Table: {table_name}\n\n"
            f"Columns:\n{columns_text}\n\n"
            f"Foreign Keys:\n{fks_text}\n\n"
            f"Sample rows:\n{samples_text}"
        )

        doc = Document(
            page_content=content,
            metadata={
                "db_id": db_id,
                "table_name": table_name,
            },
        )
        documents.append(doc)

    return documents


# --------------------------------------------------------------------------
# 2. Indexing
# --------------------------------------------------------------------------

def _delete_existing_docs(db_id: str) -> None:
    """Remove existing indexed documents for this db_id (dedup / rebuild)."""
    store = _get_vectorstore()
    try:
        existing = store.get(where={"db_id": db_id})
        ids = existing.get("ids", []) if existing else []
        if ids:
            store.delete(ids=ids)
    except Exception as e:
        # Non-fatal: proceed to add, but log it.
        logger.warning(f"RAG could not clear existing docs for db_id={db_id}: {e}")


def index_schema(db_id: str, schema: dict) -> dict:
    """
    Convert schema -> documents -> embeddings -> store in Chroma, tagged
    with db_id. Avoids duplicate documents by clearing any existing
    documents for this db_id first (upsert-by-rebuild).
    """
    if not db_id:
        raise ValueError("db_id is required for indexing.")

    logger.info(f"RAG INDEXING DATABASE: {db_id}")

    if not schema:
        logger.info(f"RAG INDEXING SKIPPED (empty schema): {db_id}")
        # Still clear out any stale docs for this db_id.
        try:
            _delete_existing_docs(db_id)
        except Exception:
            pass
        return {"db_id": db_id, "indexed_tables": []}

    try:
        documents = build_schema_documents(db_id, schema)

        store = _get_vectorstore()

        # Avoid duplicates: clear old docs for this db_id before adding new ones.
        _delete_existing_docs(db_id)

        if documents:
            ids = [f"{db_id}::{doc.metadata['table_name']}" for doc in documents]
            store.add_documents(documents, ids=ids)

        table_names = [doc.metadata["table_name"] for doc in documents]
        logger.info(f"RAG INDEXED TABLES: {table_names}")

        return {"db_id": db_id, "indexed_tables": table_names}

    except Exception as e:
        logger.error(f"RAG INDEXING FAILED for db_id={db_id}: {e}")
        raise RuntimeError(f"Schema indexing failed: {e}")


def refresh_schema_index(db_id: str, schema: dict) -> dict:
    """
    Replace/rebuild the schema index for a database when its schema
    changes. Currently equivalent to index_schema (which already clears
    stale docs first), kept as a distinct named entry point per spec.
    """
    logger.info(f"RAG REFRESHING INDEX: {db_id}")
    return index_schema(db_id, schema)


# --------------------------------------------------------------------------
# 3. Retrieval
# --------------------------------------------------------------------------

def retrieve_schema(db_id: str, question: str, k: int = 5) -> dict:
    """
    Retrieve the most relevant schema documents for `question`, restricted
    strictly to `db_id`.

    Returns:
        {
            "tables": [table_name, ...],
            "documents": [
                {"table_name": str, "content": str, "score": float | None},
                ...
            ]
        }
    """
    if not db_id:
        raise ValueError("db_id is required for retrieval.")

    if not question or not question.strip():
        raise ValueError("A non-empty question is required for retrieval.")

    logger.info(f"RAG QUERY: db_id={db_id} question={question!r}")

    try:
        store = _get_vectorstore()

        results = store.similarity_search_with_score(
            question,
            k=k,
            filter={"db_id": db_id},
        )
    except Exception as e:
        logger.error(f"RAG RETRIEVAL FAILED for db_id={db_id}: {e}")
        raise RuntimeError(f"Schema retrieval failed: {e}")

    documents = []
    tables = []

    for doc, score in results:
        table_name = doc.metadata.get("table_name")
        documents.append(
            {
                "table_name": table_name,
                "content": doc.page_content,
                "score": float(score) if score is not None else None,
            }
        )
        if table_name and table_name not in tables:
            tables.append(table_name)

    logger.info(f"RAG RETRIEVED TABLES: {tables}")

    return {"tables": tables, "documents": documents}


def get_relevant_schema_dict(db_id: str, full_schema: dict, question: str, k: int = 5) -> dict:
    """
    Convenience helper for the SQL-generation flow: given the full
    extract_schema() dict, return only the subset of it corresponding to
    tables retrieved by RAG. Falls back to the full schema if retrieval
    finds nothing or fails, so SQL generation never breaks.
    """
    if not full_schema:
        return {}

    try:
        result = retrieve_schema(db_id, question, k=k)
        tables = result.get("tables", [])
    except Exception as e:
        logger.warning(f"RAG fallback to full schema for db_id={db_id}: {e}")
        return full_schema

    if not tables:
        logger.warning(f"RAG NO RELEVANT SCHEMA FOUND for db_id={db_id}, falling back to full schema")
        return full_schema

    filtered = {t: full_schema[t] for t in tables if t in full_schema}

    if not filtered:
        return full_schema

    return filtered
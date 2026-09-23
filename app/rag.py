"""
RAG (schema retrieval) module for Eng2SQL.

Schema-only RAG:
- Builds one vector document per table.
- Restricts retrieval to a single db_id.
- Never embeds the full dataset.
- Skips rebuilding when the schema has not changed.
"""

import hashlib
import json
import logging
import os

from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma

logger = logging.getLogger("rag")
logger.setLevel(logging.INFO)

if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VECTORSTORE_DIR = os.path.join(BASE_DIR, "data", "vectorstore")
os.makedirs(VECTORSTORE_DIR, exist_ok=True)

EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
COLLECTION_NAME = "schema_docs"

SAMPLE_ROWS_IN_DOC = 3

_embeddings = None
_vectorstore = None

# In-process cache: prevents repeated re-embedding of an unchanged schema.
_INDEX_HASHES = {}


def _get_embeddings():
    """Lazily construct the local embedding model."""
    global _embeddings

    if _embeddings is None:
        logger.info(
            "Loading embedding model: %s",
            EMBEDDING_MODEL_NAME,
        )

        _embeddings = HuggingFaceEmbeddings(
            model_name=EMBEDDING_MODEL_NAME,
            model_kwargs={"device": "cpu"},
            encode_kwargs={
                "normalize_embeddings": True,
                "batch_size": 32,
            },
        )

        logger.info("Embedding model loaded.")

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
            logger.error("RAG VECTORSTORE INIT FAILED: %s", e)
            raise RuntimeError(
                f"Could not initialize vector store: {e}"
            )

    return _vectorstore


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _schema_hash(schema: dict) -> str:
    """Return a stable hash for the schema metadata."""
    payload = json.dumps(
        schema,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    return "\n".join(str(row) for row in limited)


# --------------------------------------------------------------------------
# Schema documents
# --------------------------------------------------------------------------

def build_schema_documents(db_id: str, schema: dict) -> list:
    """
    Build one Document per table.

    Only schema metadata plus a small sample of rows is embedded.
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

        columns_text = (
            "\n".join(_format_column(c) for c in columns)
            or "None"
        )

        fks_text = _format_foreign_keys(fks)
        samples_text = _format_sample_rows(samples)

        content = (
            f"Database ID: {db_id}\n"
            f"Table: {table_name}\n\n"
            f"Columns:\n{columns_text}\n\n"
            f"Foreign Keys:\n{fks_text}\n\n"
            f"Sample rows:\n{samples_text}"
        )

        documents.append(
            Document(
                page_content=content,
                metadata={
                    "db_id": db_id,
                    "table_name": table_name,
                },
            )
        )

    return documents


# --------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------

def _delete_existing_docs(db_id: str) -> None:
    """Remove existing indexed documents for one db_id."""
    store = _get_vectorstore()

    try:
        existing = store.get(where={"db_id": db_id})
        ids = existing.get("ids", []) if existing else []

        if ids:
            store.delete(ids=ids)

    except Exception as e:
        logger.warning(
            "RAG could not clear existing docs for db_id=%s: %s",
            db_id,
            e,
        )


def index_schema(db_id: str, schema: dict) -> dict:
    """
    Index schema documents only when the schema has changed.
    """
    if not db_id:
        raise ValueError("db_id is required for indexing.")

    logger.info("RAG INDEXING DATABASE: %s", db_id)

    if not schema:
        logger.info(
            "RAG INDEXING SKIPPED (empty schema): %s",
            db_id,
        )
        return {
            "db_id": db_id,
            "indexed_tables": [],
            "skipped": True,
        }

    current_hash = _schema_hash(schema)

    # Avoid reloading/re-embedding unchanged schemas.
    if _INDEX_HASHES.get(db_id) == current_hash:
        logger.info(
            "RAG INDEXING SKIPPED (schema unchanged): %s",
            db_id,
        )
        return {
            "db_id": db_id,
            "indexed_tables": list(schema.keys()),
            "skipped": True,
        }

    try:
        documents = build_schema_documents(db_id, schema)
        store = _get_vectorstore()

        _delete_existing_docs(db_id)

        if documents:
            ids = [
                f"{db_id}::{doc.metadata['table_name']}"
                for doc in documents
            ]

            store.add_documents(
                documents,
                ids=ids,
            )

        table_names = [
            doc.metadata["table_name"]
            for doc in documents
        ]

        _INDEX_HASHES[db_id] = current_hash

        logger.info(
            "RAG INDEXED TABLES: %s",
            table_names,
        )

        return {
            "db_id": db_id,
            "indexed_tables": table_names,
            "skipped": False,
        }

    except Exception as e:
        logger.error(
            "RAG INDEXING FAILED for db_id=%s: %s",
            db_id,
            e,
        )
        raise RuntimeError(
            f"Schema indexing failed: {e}"
        )


def refresh_schema_index(db_id: str, schema: dict) -> dict:
    """
    Refresh the schema index.

    If the schema is unchanged, index_schema() skips the rebuild.
    """
    logger.info("RAG REFRESHING INDEX: %s", db_id)
    return index_schema(db_id, schema)


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------

def retrieve_schema(db_id: str, question: str, k: int = 5) -> dict:
    """
    Retrieve relevant schema documents for a question, restricted to db_id.
    """
    if not db_id:
        raise ValueError("db_id is required for retrieval.")

    if not question or not question.strip():
        raise ValueError(
            "A non-empty question is required for retrieval."
        )

    logger.info(
        "RAG QUERY: db_id=%s question=%r",
        db_id,
        question,
    )

    try:
        store = _get_vectorstore()

        results = store.similarity_search_with_score(
            question,
            k=k,
            filter={"db_id": db_id},
        )

    except Exception as e:
        logger.error(
            "RAG RETRIEVAL FAILED for db_id=%s: %s",
            db_id,
            e,
        )
        raise RuntimeError(
            f"Schema retrieval failed: {e}"
        )

    documents = []
    tables = []

    for doc, score in results:
        table_name = doc.metadata.get("table_name")

        documents.append(
            {
                "table_name": table_name,
                "content": doc.page_content,
                "score": (
                    float(score)
                    if score is not None
                    else None
                ),
            }
        )

        if table_name and table_name not in tables:
            tables.append(table_name)

    logger.info(
        "RAG RETRIEVED TABLES: %s",
        tables,
    )

    return {
        "tables": tables,
        "documents": documents,
    }


def get_relevant_schema_dict(
    db_id: str,
    full_schema: dict,
    question: str,
    k: int = 5,
) -> dict:
    """
    Return only the schema tables retrieved by RAG.

    Falls back to the full schema if retrieval fails or finds nothing.
    """
    if not full_schema:
        return {}

    try:
        result = retrieve_schema(
            db_id,
            question,
            k=k,
        )
        tables = result.get("tables", [])

    except Exception as e:
        logger.warning(
            "RAG fallback to full schema for db_id=%s: %s",
            db_id,
            e,
        )
        return full_schema

    if not tables:
        logger.warning(
            "RAG NO RELEVANT SCHEMA FOUND for db_id=%s, "
            "falling back to full schema",
            db_id,
        )
        return full_schema

    filtered = {
        table: full_schema[table]
        for table in tables
        if table in full_schema
    }

    return filtered or full_schema

"""
RAG (schema retrieval) module for Eng2SQL.

Schema-only RAG:
- Builds one vector document per table.
- Restricts retrieval to a single db_id.
- Never embeds the full dataset.
- Re-embeds only the tables whose document actually changed.
- Skips the vector store entirely for databases small enough that every
  table would be retrieved anyway, so the embedding model is only loaded
  when a database is big enough to need it.
"""

import hashlib
import logging
import threading

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.config import VECTORSTORE_DIR

logger = logging.getLogger("rag")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
COLLECTION_NAME = "schema_docs"

SAMPLE_ROWS_IN_DOC = 3

# Tables returned per question. A schema with at most this many tables is
# always returned whole, so it never needs embedding or a vector search.
DEFAULT_K = 5

_vectorstore = None

# Guards lazy construction and indexing: requests and background indexing
# tasks run on different worker threads.
_lock = threading.RLock()

# In-process cache of what is currently indexed per db_id, so an unchanged
# schema costs a dict lookup instead of a vector-store round trip.
_INDEX_HASHES = {}


class _LazyEmbeddings(Embeddings):
    """
    Defers loading the sentence-transformers model (and importing torch)
    until something actually has to be embedded.
    """

    def __init__(self):
        self._model = None

    def _get_model(self):
        if self._model is None:
            from langchain_huggingface import HuggingFaceEmbeddings

            logger.info("Loading embedding model: %s", EMBEDDING_MODEL_NAME)
            self._model = HuggingFaceEmbeddings(
                model_name=EMBEDDING_MODEL_NAME,
                model_kwargs={"device": "cpu"},
                encode_kwargs={
                    "normalize_embeddings": True,
                    "batch_size": 32,
                },
            )
            logger.info("Embedding model loaded.")
        return self._model

    def embed_documents(self, texts):
        return self._get_model().embed_documents(texts)

    def embed_query(self, text):
        return self._get_model().embed_query(text)


def _get_vectorstore():
    """Lazily construct the persistent Chroma vector store."""
    global _vectorstore

    with _lock:
        if _vectorstore is None:
            try:
                from langchain_chroma import Chroma

                _vectorstore = Chroma(
                    collection_name=COLLECTION_NAME,
                    embedding_function=_LazyEmbeddings(),
                    persist_directory=VECTORSTORE_DIR,
                )
            except Exception as e:
                logger.error("RAG VECTORSTORE INIT FAILED: %s", e)
                raise RuntimeError(f"Could not initialize vector store: {e}")

        return _vectorstore


# --------------------------------------------------------------------------
# Schema documents
# --------------------------------------------------------------------------

def _format_column(col: dict) -> str:
    name = col.get("name", "")
    dtype = col.get("type", "")
    pk = " PRIMARY KEY" if col.get("primary_key") else ""
    return f"{name} {dtype}{pk}".strip()


def _format_foreign_keys(fks: list) -> str:
    if not fks:
        return "None"

    return "\n".join(
        f"{fk.get('column', '')} -> {fk.get('ref_table', '')}.{fk.get('ref_column', '')}"
        for fk in fks
    )


def _format_sample_rows(rows: list) -> str:
    if not rows:
        return "None"

    return "\n".join(str(row) for row in rows[:SAMPLE_ROWS_IN_DOC])


def build_schema_documents(db_id: str, schema: dict) -> list:
    """
    Build one Document per table.

    Only schema metadata plus a small sample of rows is embedded.
    """
    if not db_id:
        raise ValueError("db_id is required to build schema documents.")

    documents = []

    for table_name, table_info in (schema or {}).items():
        columns_text = (
            "\n".join(_format_column(c) for c in table_info.get("columns", []))
            or "None"
        )

        content = (
            f"Database ID: {db_id}\n"
            f"Table: {table_name}\n\n"
            f"Columns:\n{columns_text}\n\n"
            f"Foreign Keys:\n{_format_foreign_keys(table_info.get('foreign_keys', []))}\n\n"
            f"Sample rows:\n{_format_sample_rows(table_info.get('sample_rows', []))}"
        )

        documents.append(
            Document(
                page_content=content,
                metadata={
                    "db_id": db_id,
                    "table_name": table_name,
                    "doc_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                },
            )
        )

    return documents


def _doc_id(doc: Document) -> str:
    return f"{doc.metadata['db_id']}::{doc.metadata['table_name']}"


# --------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------

def index_schema(db_id: str, schema: dict) -> dict:
    """
    Bring the vector index for one database in line with its schema.

    Incremental: each table's document carries a content hash, so only
    new or changed tables are embedded and tables that no longer exist are
    removed. The hashes live in the vector store itself, which means an
    unchanged schema is not re-embedded after a server restart either.
    """
    if not db_id:
        raise ValueError("db_id is required for indexing.")

    if len(schema or {}) <= DEFAULT_K:
        logger.info("RAG INDEXING SKIPPED (schema fits in one prompt): %s", db_id)
        return {"db_id": db_id, "indexed_tables": [], "skipped": True}

    documents = build_schema_documents(db_id, schema)
    wanted = {_doc_id(doc): doc for doc in documents}
    current_hashes = {doc_id: doc.metadata["doc_hash"] for doc_id, doc in wanted.items()}

    with _lock:
        if _INDEX_HASHES.get(db_id) == current_hashes:
            return {"db_id": db_id, "indexed_tables": [], "skipped": True}

        try:
            store = _get_vectorstore()

            existing = store.get(where={"db_id": db_id}, include=["metadatas"])
            indexed_hashes = {
                doc_id: (meta or {}).get("doc_hash")
                for doc_id, meta in zip(existing.get("ids", []), existing.get("metadatas", []))
            }

            stale_ids = [doc_id for doc_id in indexed_hashes if doc_id not in wanted]
            changed = [
                doc for doc_id, doc in wanted.items()
                if indexed_hashes.get(doc_id) != doc.metadata["doc_hash"]
            ]

            if stale_ids:
                store.delete(ids=stale_ids)

            if changed:
                # Same ids -> Chroma upserts, replacing the old embedding.
                store.add_documents(changed, ids=[_doc_id(doc) for doc in changed])

        except Exception as e:
            logger.error("RAG INDEXING FAILED for db_id=%s: %s", db_id, e)
            raise RuntimeError(f"Schema indexing failed: {e}")

        _INDEX_HASHES[db_id] = current_hashes

    table_names = [doc.metadata["table_name"] for doc in changed]
    logger.info("RAG INDEXED db_id=%s tables=%s removed=%d", db_id, table_names, len(stale_ids))

    return {"db_id": db_id, "indexed_tables": table_names, "skipped": not changed}


def delete_index(db_id: str, schema: dict) -> None:
    """
    Drop a deleted database's documents from the vector index. Small
    schemas were never indexed, so the vector store isn't even opened.
    """
    with _lock:
        was_indexed = _INDEX_HASHES.pop(db_id, None) is not None
        if not was_indexed and len(schema or {}) <= DEFAULT_K:
            return
        try:
            store = _get_vectorstore()
            ids = store.get(where={"db_id": db_id}).get("ids", [])
            if ids:
                store.delete(ids=ids)
        except Exception as e:
            logger.warning("RAG could not remove index for db_id=%s: %s", db_id, e)


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------

def retrieve_schema(
    db_id: str,
    question: str,
    k: int = DEFAULT_K,
    schema: dict | None = None,
) -> dict:
    """
    Retrieve relevant schema documents for a question, restricted to db_id.

    When the caller passes the full `schema` and it has at most `k` tables,
    every table is returned directly — a vector search could only return
    the same set. For larger schemas the index is synced first (a no-op
    when nothing changed), so a query never races a background indexing
    task or misses an index that was lost.
    """
    if not db_id:
        raise ValueError("db_id is required for retrieval.")

    if not question or not question.strip():
        raise ValueError("A non-empty question is required for retrieval.")

    if schema is not None and len(schema) <= k:
        documents = [
            {
                "table_name": doc.metadata["table_name"],
                "content": doc.page_content,
                "score": None,
            }
            for doc in build_schema_documents(db_id, schema)
        ]
        return {"tables": [d["table_name"] for d in documents], "documents": documents}

    try:
        if schema is not None:
            index_schema(db_id, schema)

        results = _get_vectorstore().similarity_search_with_score(
            question,
            k=k,
            filter={"db_id": db_id},
        )

    except Exception as e:
        logger.error("RAG RETRIEVAL FAILED for db_id=%s: %s", db_id, e)
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

    logger.info("RAG RETRIEVED TABLES: %s", tables)

    return {"tables": tables, "documents": documents}

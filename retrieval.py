"""
retrieval.py — retrieval module for the adversarial code review orchestrator.

Provides:
- ``initialize_store()``: seeds ChromaDB with examples on first boot.
- ``retrieve_examples()``: queries ChromaDB for relevant past findings.
- ``format_examples_for_prompt()``: renders examples as numbered text
  for injection into the Reviewer's instruction template.

All embeddings are computed locally via sentence-transformers so that
query-time retrieval requires zero network calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import chromadb
from sentence_transformers import SentenceTransformer

from config import settings
from observability import get_logger

logger = get_logger(__name__)

# Module-level singletons (lazy-initialised)
_chroma_client: chromadb.ClientAPI | None = None
_collection: chromadb.Collection | None = None
_embedder: SentenceTransformer | None = None


def _get_embedder() -> SentenceTransformer:
    """Return (and cache) the sentence-transformer embedding model."""
    global _embedder  # noqa: PLW0603
    if _embedder is None:
        logger.info("loading_embedding_model", model=settings.EMBEDDING_MODEL)
        _embedder = SentenceTransformer(settings.EMBEDDING_MODEL)
    return _embedder


def _get_collection() -> chromadb.Collection:
    """Return (and cache) the ChromaDB collection handle."""
    global _chroma_client, _collection  # noqa: PLW0603
    if _collection is None:
        _chroma_client = chromadb.PersistentClient(path=settings.CHROMA_PERSIST_DIR)
        _collection = _chroma_client.get_or_create_collection(
            name=settings.CHROMA_COLLECTION,
        )
    return _collection


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def initialize_store() -> None:
    """Seed the ChromaDB collection with the JSONL seed file if it is empty.

    Intended to be called once at application startup.  If the collection
    already contains documents the call is a no-op.
    """
    collection = _get_collection()
    if collection.count() > 0:
        logger.info(
            "store_already_seeded",
            count=collection.count(),
        )
        return

    seed_path = Path(settings.SEED_DATA_PATH)
    if not seed_path.exists():
        logger.warning("seed_file_missing", path=str(seed_path))
        return

    logger.info("seeding_store", path=str(seed_path))

    # Import ingest lazily to avoid circular dependency at module level
    from retrieval_pipeline.ingest import ingest_file

    accepted, rejected = ingest_file(seed_path)
    logger.info(
        "seeding_complete",
        accepted=accepted,
        rejected=rejected,
    )


def retrieve_examples(
    current_code: str,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Query ChromaDB for the *top_k* examples most similar to *current_code*.

    Embeddings are computed locally — no network calls at query time.

    Returns:
        A list of dicts, each containing the stored metadata fields
        (``bug_pattern``, ``code_snippet``, ``review_comment``,
        ``fix_summary``) plus the ChromaDB ``id`` and ``distance``.
    """
    collection = _get_collection()
    if collection.count() == 0:
        logger.warning("retrieve_empty_collection")
        return []

    embedder = _get_embedder()
    query_embedding: list[float] = embedder.encode(
        current_code,
        show_progress_bar=False,
    ).tolist()

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(top_k, collection.count()),
        include=["metadatas", "distances", "documents"],
    )

    examples: list[dict[str, Any]] = []
    ids: list[str] = results.get("ids", [[]])[0]
    metadatas: list[dict[str, Any]] = results.get("metadatas", [[]])[0]
    distances: list[float] = results.get("distances", [[]])[0]

    for record_id, meta, dist in zip(ids, metadatas, distances):
        example: dict[str, Any] = {
            "id": record_id,
            "distance": dist,
            **meta,
        }
        examples.append(example)

    logger.info(
        "retrieve_examples",
        query_length=len(current_code),
        top_k=top_k,
        returned=len(examples),
    )
    return examples


def format_examples_for_prompt(examples: list[dict[str, Any]]) -> str:
    """Format retrieved examples as a numbered list for prompt injection.

    Each example block contains the bug pattern, the problematic code,
    the review comment, and the fix summary.
    """
    if not examples:
        return "No similar past findings available."

    parts: list[str] = []
    for idx, ex in enumerate(examples, start=1):
        block = (
            f"--- Example {idx} [{ex.get('bug_pattern', 'unknown')}] ---\n"
            f"Code:\n```\n{ex.get('code_snippet', '')}\n```\n"
            f"Review comment: {ex.get('review_comment', '')}\n"
            f"Fix: {ex.get('fix_summary', '')}"
        )
        parts.append(block)

    return "\n\n".join(parts)

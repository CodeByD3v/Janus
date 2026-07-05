"""
ingest.py — batch ingestion of 'real catch' examples into ChromaDB.

Usage:
    python -m retrieval_pipeline.ingest path/to/new_examples.jsonl

Each line in the JSONL file must be a valid JSON object conforming to
``retrieval_pipeline.schema.RealCatchExample``.  Malformed records are
logged and skipped; valid records are upserted into the ChromaDB
persistent collection (safe to run while the service is live).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import chromadb
from pydantic import ValidationError
from sentence_transformers import SentenceTransformer

# Resolve project-root imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import settings  # noqa: E402
from core.observability import get_logger  # noqa: E402
from retrieval_pipeline.schema import RealCatchExample, validate_record  # noqa: E402

logger = get_logger(__name__)


def _build_document(record: RealCatchExample) -> str:
    """Combine the fields that carry semantic signal for embedding."""
    return f"{record.code_snippet}\n\n{record.review_comment}"


def _record_metadata(record: RealCatchExample) -> dict[str, str]:
    """Return the metadata dict stored alongside each ChromaDB document."""
    return {
        "bug_pattern": record.bug_pattern,
        "code_snippet": record.code_snippet,
        "review_comment": record.review_comment,
        "fix_summary": record.fix_summary,
    }


def ingest_file(
    jsonl_path: str | Path,
    *,
    chroma_persist_dir: str | None = None,
    collection_name: str | None = None,
    embedding_model_name: str | None = None,
) -> tuple[int, int]:
    """Ingest a JSONL file of examples into ChromaDB.

    Returns:
        A ``(accepted, rejected)`` tuple with record counts.
    """
    jsonl_path = Path(jsonl_path)
    if not jsonl_path.exists():
        logger.error("ingest_file_not_found", path=str(jsonl_path))
        raise FileNotFoundError(f"JSONL file not found: {jsonl_path}")

    chroma_dir = chroma_persist_dir or settings.CHROMA_PERSIST_DIR
    col_name = collection_name or settings.CHROMA_COLLECTION
    model_name = embedding_model_name or settings.EMBEDDING_MODEL

    logger.info(
        "ingest_start",
        file=str(jsonl_path),
        chroma_dir=chroma_dir,
        collection=col_name,
        model=model_name,
    )

    # --- Load & validate ---
    valid_records: list[RealCatchExample] = []
    rejected = 0

    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                data: dict[str, Any] = json.loads(raw_line)
                record = validate_record(data)
                valid_records.append(record)
            except (json.JSONDecodeError, ValidationError) as exc:
                rejected += 1
                logger.warning(
                    "ingest_record_rejected",
                    line=line_no,
                    error=str(exc),
                )

    if not valid_records:
        logger.warning("ingest_no_valid_records", rejected=rejected)
        return 0, rejected

    # --- Embed ---
    model = SentenceTransformer(model_name)
    documents = [_build_document(r) for r in valid_records]
    embeddings_array = model.encode(documents, show_progress_bar=False)
    embeddings: list[list[float]] = [emb.tolist() for emb in embeddings_array]

    # --- Upsert into ChromaDB ---
    client = chromadb.PersistentClient(path=chroma_dir)
    collection = client.get_or_create_collection(name=col_name)

    ids = [r.id for r in valid_records]
    metadatas = [_record_metadata(r) for r in valid_records]

    collection.upsert(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )

    accepted = len(valid_records)
    logger.info(
        "ingest_complete",
        accepted=accepted,
        rejected=rejected,
        total_in_collection=collection.count(),
    )
    return accepted, rejected


def main() -> None:
    """CLI entry-point for batch ingestion."""
    parser = argparse.ArgumentParser(
        description="Ingest a JSONL file of real-catch examples into ChromaDB."
    )
    parser.add_argument(
        "jsonl_path",
        type=str,
        help="Path to the .jsonl file containing example records.",
    )
    args = parser.parse_args()

    accepted, rejected = ingest_file(args.jsonl_path)
    print(f"Ingestion complete: {accepted} accepted, {rejected} rejected.")


if __name__ == "__main__":
    main()

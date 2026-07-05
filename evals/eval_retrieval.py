"""
evals/eval_retrieval.py — retrieval store and pipeline tests.

Tests schema validation, ChromaDB seeding, retrieval ranking, persistence
across simulated restarts, and upsert idempotency. No API key needed.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retrieval_pipeline.schema import RealCatchExample, validate_record  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_valid_record() -> dict:
    return {
        "id": f"test-{uuid.uuid4().hex[:8]}",
        "bug_pattern": "off_by_one",
        "code_snippet": "for i in range(len(items) - 1):\n    process(items[i])",
        "review_comment": "Off-by-one: the last element is never processed.",
        "fix_summary": "Change to range(len(items)).",
    }


@pytest.fixture
def temp_chroma_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Create a temporary ChromaDB directory and patch settings to use it."""
    chroma_dir = str(tmp_path / "chroma_test")
    collection_name = f"test_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr("core.config.settings.CHROMA_PERSIST_DIR", chroma_dir)
    monkeypatch.setattr("core.config.settings.CHROMA_COLLECTION", collection_name)
    # Reset module-level singletons in retrieval.py so they pick up new settings
    import core.retrieval as retrieval
    monkeypatch.setattr(retrieval, "_chroma_client", None)
    monkeypatch.setattr(retrieval, "_collection", None)
    monkeypatch.setattr(retrieval, "_embedder", None)
    yield chroma_dir, collection_name


@pytest.fixture
def seed_jsonl(tmp_path: Path) -> Path:
    """Create a small seed JSONL file for testing."""
    records = [
        {
            "id": "test-001",
            "bug_pattern": "mutates_caller_list",
            "code_snippet": "def remove_dupes(items):\n    for x in items:\n        if items.count(x) > 1:\n            items.remove(x)\n    return items",
            "review_comment": "Mutates the caller's list while iterating.",
            "fix_summary": "Build a new list instead of modifying in-place.",
        },
        {
            "id": "test-002",
            "bug_pattern": "unhandled_none",
            "code_snippet": "def get_name(user):\n    return user.get('name').strip()",
            "review_comment": "user.get('name') can return None, causing AttributeError on .strip().",
            "fix_summary": "Use user.get('name', '') or add a None check.",
        },
        {
            "id": "test-003",
            "bug_pattern": "off_by_one",
            "code_snippet": "def last_char(s):\n    return s[len(s)]",
            "review_comment": "IndexError: string indices are 0-based, use len(s)-1.",
            "fix_summary": "Change to s[len(s) - 1] or s[-1].",
        },
    ]
    path = tmp_path / "test_seed.jsonl"
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_accepts_valid(self) -> None:
        record = _make_valid_record()
        result = validate_record(record)
        assert isinstance(result, RealCatchExample)
        assert result.bug_pattern == "off_by_one"

    def test_rejects_empty_field(self) -> None:
        record = _make_valid_record()
        record["code_snippet"] = ""
        with pytest.raises(ValidationError):
            validate_record(record)

    def test_rejects_whitespace_only_field(self) -> None:
        record = _make_valid_record()
        record["review_comment"] = "   "
        with pytest.raises(ValidationError):
            validate_record(record)

    def test_rejects_missing_field(self) -> None:
        record = _make_valid_record()
        del record["fix_summary"]
        with pytest.raises(ValidationError):
            validate_record(record)


# ---------------------------------------------------------------------------
# Retrieval store
# ---------------------------------------------------------------------------


class TestRetrievalStore:
    def test_retrieve_examples_empty_collection(
        self, temp_chroma_dir: tuple[str, str]
    ) -> None:
        from core.retrieval import retrieve_examples
        results = retrieve_examples("some code here", top_k=3)
        assert results == []

    def test_initialize_store_seeds_data(
        self,
        temp_chroma_dir: tuple[str, str],
        seed_jsonl: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("core.config.settings.SEED_DATA_PATH", str(seed_jsonl))
        from core.retrieval import initialize_store, _get_collection
        initialize_store()
        collection = _get_collection()
        assert collection.count() == 3

        # Second call is a no-op
        initialize_store()
        assert collection.count() == 3

    def test_retrieve_examples_returns_results(
        self,
        temp_chroma_dir: tuple[str, str],
        seed_jsonl: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("core.config.settings.SEED_DATA_PATH", str(seed_jsonl))
        from core.retrieval import initialize_store, retrieve_examples
        initialize_store()

        results = retrieve_examples("items.remove(x)", top_k=2)
        assert len(results) > 0
        assert len(results) <= 2
        # Each result should have required fields
        for r in results:
            assert "id" in r
            assert "distance" in r
            assert "bug_pattern" in r

    def test_persistence_across_restart(
        self,
        temp_chroma_dir: tuple[str, str],
        seed_jsonl: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Seed, reset singletons (simulating restart), retrieve again."""
        monkeypatch.setattr("core.config.settings.SEED_DATA_PATH", str(seed_jsonl))
        from core.retrieval import initialize_store, retrieve_examples
        import core.retrieval as retrieval

        initialize_store()

        # Simulate restart by clearing singletons
        retrieval._chroma_client = None
        retrieval._collection = None
        # Keep embedder cached (it's deterministic)

        results = retrieve_examples("mutate caller list", top_k=3)
        assert len(results) > 0  # Data persisted on disk


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------


class TestFormatExamples:
    def test_empty_list(self) -> None:
        from core.retrieval import format_examples_for_prompt
        result = format_examples_for_prompt([])
        assert "No similar" in result or "none" in result.lower()

    def test_formatting(self) -> None:
        from core.retrieval import format_examples_for_prompt
        examples = [
            {
                "id": "ex-1",
                "bug_pattern": "off_by_one",
                "code_snippet": "x[len(x)]",
                "review_comment": "Index out of range",
                "fix_summary": "Use len(x) - 1",
            }
        ]
        result = format_examples_for_prompt(examples)
        assert "Example 1" in result
        assert "off_by_one" in result
        assert "x[len(x)]" in result


# ---------------------------------------------------------------------------
# Ingest pipeline
# ---------------------------------------------------------------------------


class TestIngestPipeline:
    def test_ingest_upsert(
        self,
        temp_chroma_dir: tuple[str, str],
        seed_jsonl: Path,
    ) -> None:
        """Ingesting the same file twice should not create duplicates."""
        from retrieval_pipeline.ingest import ingest_file

        chroma_dir, col_name = temp_chroma_dir
        accepted1, rejected1 = ingest_file(
            seed_jsonl,
            chroma_persist_dir=chroma_dir,
            collection_name=col_name,
        )
        assert accepted1 == 3
        assert rejected1 == 0

        # Ingest again — upsert, count stays the same
        accepted2, rejected2 = ingest_file(
            seed_jsonl,
            chroma_persist_dir=chroma_dir,
            collection_name=col_name,
        )
        assert accepted2 == 3

        import chromadb
        client = chromadb.PersistentClient(path=chroma_dir)
        collection = client.get_or_create_collection(name=col_name)
        assert collection.count() == 3  # No duplicates

    def test_ingest_rejects_malformed(self, tmp_path: Path, temp_chroma_dir: tuple[str, str]) -> None:
        """Malformed records are rejected, valid ones accepted."""
        chroma_dir, col_name = temp_chroma_dir
        path = tmp_path / "mixed.jsonl"
        with open(path, "w") as f:
            f.write(json.dumps(_make_valid_record()) + "\n")
            f.write('{"id": "bad", "bug_pattern": ""}\n')  # missing fields + empty
            f.write("not valid json\n")
        accepted, rejected = __import__("retrieval_pipeline.ingest", fromlist=["ingest_file"]).ingest_file(
            path, chroma_persist_dir=chroma_dir, collection_name=col_name
        )
        assert accepted == 1
        assert rejected == 2

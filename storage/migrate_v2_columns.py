"""
migrate_v2_columns.py — Add Janus 2.0 columns to existing databases.

SQLAlchemy's create_all() only creates MISSING TABLES, not missing columns.
This script adds the new reviewer_verdict / needs_human_review columns to
existing debate_sessions and debate_rounds tables.

Safe to run multiple times — checks before altering.

Usage:
    python -m storage.migrate_v2_columns
"""

from __future__ import annotations

import sqlite3
import sys

from core.config import settings
from core.observability import get_logger

logger = get_logger(__name__)


def _column_exists(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
    """Check if a column already exists in a SQLite table."""
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def migrate_sqlite(db_path: str) -> None:
    """Add Janus 2.0 columns to a SQLite database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    migrations: list[tuple[str, str, str]] = [
        # (table, column, DDL)
        # Janus 2.0 — Reviewer-first verdict tracking
        (
            "debate_sessions",
            "reviewer_verdict",
            "ALTER TABLE debate_sessions ADD COLUMN reviewer_verdict VARCHAR(32)",
        ),
        (
            "debate_sessions",
            "needs_human_review",
            "ALTER TABLE debate_sessions ADD COLUMN needs_human_review BOOLEAN DEFAULT 0",
        ),
        (
            "debate_rounds",
            "reviewer_verdict",
            "ALTER TABLE debate_rounds ADD COLUMN reviewer_verdict VARCHAR(32)",
        ),
        # Phase 5 — BYOK model configuration
        (
            "debate_sessions",
            "model_provider",
            "ALTER TABLE debate_sessions ADD COLUMN model_provider VARCHAR(32)",
        ),
        (
            "debate_sessions",
            "model_name",
            "ALTER TABLE debate_sessions ADD COLUMN model_name VARCHAR(128)",
        ),
    ]

    applied = 0
    for table, column, ddl in migrations:
        if _column_exists(cursor, table, column):
            logger.info("column_already_exists", table=table, column=column)
        else:
            cursor.execute(ddl)
            applied += 1
            logger.info("column_added", table=table, column=column)

    conn.commit()
    conn.close()

    if applied:
        logger.info("migration_complete", columns_added=applied)
    else:
        logger.info("migration_no_op", detail="All columns already exist")


def main() -> None:
    db_url = settings.DATABASE_URL

    if db_url.startswith("sqlite"):
        # Extract path from sqlite:///./foo.db or sqlite:///foo.db
        db_path = db_url.split("///", 1)[1]
        print(f"Migrating SQLite database: {db_path}")
        migrate_sqlite(db_path)
        print("Done.")
    else:
        print(
            "This migration script currently supports SQLite only.\n"
            "For PostgreSQL/MySQL, run these ALTER TABLE statements manually:\n"
            "\n"
            "  -- Janus 2.0: Reviewer-first verdict tracking\n"
            "  ALTER TABLE debate_sessions ADD COLUMN reviewer_verdict VARCHAR(32);\n"
            "  ALTER TABLE debate_sessions ADD COLUMN needs_human_review BOOLEAN DEFAULT FALSE;\n"
            "  ALTER TABLE debate_rounds ADD COLUMN reviewer_verdict VARCHAR(32);\n"
            "\n"
            "  -- Phase 5: BYOK model configuration\n"
            "  ALTER TABLE debate_sessions ADD COLUMN model_provider VARCHAR(32);\n"
            "  ALTER TABLE debate_sessions ADD COLUMN model_name VARCHAR(128);\n"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
storage/models.py — ORM models for debate persistence.

Provides DebateSession and Round models that capture the full lifecycle
of an adversarial code review debate, including per-round retrieval metadata,
gate results, and cost tracking.

Uses SQLAlchemy 2.0-style declarative models. Supports both SQLite (dev)
and PostgreSQL (prod) via the DATABASE_URL config.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models.

    __allow_unmapped__ = True is SQLAlchemy's own documented escape hatch
    for exactly this file's style: legacy `Column()` assignments paired
    with plain type annotations (`str`, `Optional[str]`, `list[Round]`,
    etc.) instead of the newer `Mapped[...]` generic wrapper. Without
    this, SQLAlchemy 2.0's declarative scanner raises
    `MappedAnnotationError` on the `relationship()` fields below, since
    it expects `Mapped[]` to signal "this annotation is ORM-mapped."
    Setting this here means every field in this file keeps its current
    (legacy but valid) style rather than requiring every single
    annotation across both models to be rewritten as Mapped[...].
    """

    __allow_unmapped__ = True


class DebateSession(Base):
    """A single adversarial code review debate."""

    __tablename__ = "debate_sessions"

    id: str = Column(String(36), primary_key=True)  # type: ignore[assignment]
    repo_ref: str = Column(String(512), nullable=False)  # type: ignore[assignment]
    target_file: str = Column(String(512), nullable=False)  # type: ignore[assignment]
    ticket: str = Column(Text, nullable=False)  # type: ignore[assignment]
    status: str = Column(  # type: ignore[assignment]
        String(32), nullable=False, default="queued", index=True
    )
    tenant_id: Optional[str] = Column(String(128), nullable=True)  # type: ignore[assignment]
    merged: Optional[bool] = Column(Boolean, nullable=True)  # type: ignore[assignment]
    final_gate_json: Optional[str] = Column(Text, nullable=True)  # type: ignore[assignment]
    cost_json: Optional[str] = Column(Text, nullable=True)  # type: ignore[assignment]
    error_message: Optional[str] = Column(Text, nullable=True)  # type: ignore[assignment]
    sandbox_path: Optional[str] = Column(String(512), nullable=True)  # type: ignore[assignment]
    # GAP 17 / TASK 18 — optional, all independent of each other and of
    # everything else here. A session with none of these set behaves
    # exactly as it did before this feature existed (see notifications.py).
    pr_repo: Optional[str] = Column(String(256), nullable=True)  # type: ignore[assignment]
    pr_number: Optional[int] = Column(Integer, nullable=True)  # type: ignore[assignment]
    commit_sha: Optional[str] = Column(String(64), nullable=True)  # type: ignore[assignment]
    webhook_url: Optional[str] = Column(String(2048), nullable=True)  # type: ignore[assignment]
    created_at: datetime = Column(  # type: ignore[assignment]
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Column(  # type: ignore[assignment]
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # No type annotation here on purpose: with __allow_unmapped__ = True,
    # a plain `list[Round]` annotation confuses SQLAlchemy's collection-
    # type detection for relationship() specifically (it silently forces
    # uselist=False even when uselist=True is passed explicitly) — this
    # was hit and verified during testing, not a hypothetical. Leaving
    # this unannotated and relying on `uselist=True` below is the
    # reliable way to get a real one-to-many collection here.
    rounds = relationship(  # type: ignore[assignment]
        "Round",
        back_populates="session",
        order_by="Round.round_num",
        uselist=True,
    )

    __table_args__ = (Index("ix_debate_sessions_status_created", "status", "created_at"),)

    @property
    def final_gate(self) -> dict[str, Any] | None:
        if self.final_gate_json:
            return json.loads(self.final_gate_json)  # type: ignore[arg-type]
        return None

    @final_gate.setter
    def final_gate(self, value: dict[str, Any] | None) -> None:
        self.final_gate_json = json.dumps(value) if value else None

    @property
    def cost(self) -> dict[str, Any] | None:
        if self.cost_json:
            return json.loads(self.cost_json)  # type: ignore[arg-type]
        return None

    @cost.setter
    def cost(self, value: dict[str, Any] | None) -> None:
        self.cost_json = json.dumps(value) if value else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repo_ref": self.repo_ref,
            "target_file": self.target_file,
            "ticket": self.ticket,
            "status": self.status,
            "tenant_id": self.tenant_id,
            "merged": self.merged,
            "final_gate": self.final_gate,
            "cost": self.cost,
            "error_message": self.error_message,
            "pr_repo": self.pr_repo,
            "pr_number": self.pr_number,
            "commit_sha": self.commit_sha,
            "webhook_url": self.webhook_url,
            "rounds": [r.to_dict() for r in self.rounds],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Round(Base):
    """A single round within a debate session."""

    __tablename__ = "debate_rounds"

    id: int = Column(Integer, primary_key=True, autoincrement=True)  # type: ignore[assignment]
    session_id: str = Column(  # type: ignore[assignment]
        String(36), ForeignKey("debate_sessions.id"), nullable=False, index=True
    )
    round_num: int = Column(Integer, nullable=False)  # type: ignore[assignment]
    patch_text: str = Column(Text, nullable=False, default="")  # type: ignore[assignment]
    reviewer_text: str = Column(Text, nullable=False, default="")  # type: ignore[assignment]
    gate_result_json: Optional[str] = Column(Text, nullable=True)  # type: ignore[assignment]
    retrieved_example_ids_json: Optional[str] = Column(Text, nullable=True)  # type: ignore[assignment]
    repo_context_signals_json: Optional[str] = Column(Text, nullable=True)  # type: ignore[assignment]
    stop_reason: Optional[str] = Column(String(64), nullable=True)  # type: ignore[assignment]
    code_extraction_failed: bool = Column(  # type: ignore[assignment]
        Boolean, nullable=False, default=False
    )
    reviewer_skipped_counterexample: bool = Column(  # type: ignore[assignment]
        Boolean, nullable=False, default=False
    )
    created_at: datetime = Column(  # type: ignore[assignment]
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # See the matching note on DebateSession.rounds above — no annotation
    # here on purpose.
    session = relationship(  # type: ignore[assignment]
        "DebateSession", back_populates="rounds", uselist=False
    )

    @property
    def gate_result(self) -> dict[str, Any] | None:
        if self.gate_result_json:
            return json.loads(self.gate_result_json)  # type: ignore[arg-type]
        return None

    @gate_result.setter
    def gate_result(self, value: dict[str, Any] | None) -> None:
        self.gate_result_json = json.dumps(value) if value else None

    @property
    def retrieved_example_ids(self) -> list[str]:
        if self.retrieved_example_ids_json:
            return json.loads(self.retrieved_example_ids_json)  # type: ignore[arg-type]
        return []

    @retrieved_example_ids.setter
    def retrieved_example_ids(self, value: list[str]) -> None:
        self.retrieved_example_ids_json = json.dumps(value)

    @property
    def repo_context_signals(self) -> dict[str, Any]:
        if self.repo_context_signals_json:
            return json.loads(self.repo_context_signals_json)  # type: ignore[arg-type]
        return {}

    @repo_context_signals.setter
    def repo_context_signals(self, value: dict[str, Any]) -> None:
        self.repo_context_signals_json = json.dumps(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_num": self.round_num,
            "patch_text": self.patch_text,
            "reviewer_text": self.reviewer_text,
            "gate_result": self.gate_result,
            "retrieved_example_ids": self.retrieved_example_ids,
            "repo_context_signals": self.repo_context_signals,
            "stop_reason": self.stop_reason,
            "code_extraction_failed": self.code_extraction_failed,
            "reviewer_skipped_counterexample": self.reviewer_skipped_counterexample,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

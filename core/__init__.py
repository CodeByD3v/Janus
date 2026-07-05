"""
core/ — the adversarial code review engine.

This package contains the core business logic:
- config: centralized, environment-driven settings
- observability: structured logging, metrics, cost tracking
- gate: deterministic verification gate (lint/type/test/security)
- agents: Patcher and Reviewer LLM agent definitions
- orchestrator: the debate loop (retry, circuit breaker, persistence)
- retrieval: ChromaDB vector store for "real catch" few-shot examples
- worker: queue consumer that runs debates asynchronously
"""

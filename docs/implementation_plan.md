# Janus 2.0 — Implementation Plan

This document is the detailed implementation spec for migrating Janus from v1.0 (Patcher-first, Python-only) to v2.0 (Reviewer-first, language-agnostic, multi-provider). See ROADMAP.md for status tracking and ARCHITECTURE.md for the target design.

## Guiding Principles
- The moat is the debate, not the linter
- The Patcher is a responder, not an initiator
- Language-specific behavior is configuration, not code
- The user's model choice is invisible to the debate engine
- Fork PRs are untrusted by default

## Phase 1: Core Engine Restructure
[Detailed implementation spec with pseudocode for the new run_debate() flow, the three ReviewerVerdict outcomes, updated dataclasses, ORM changes]

## Phase 2: Generic Validation Interface
[Detailed implementation spec for ValidationRunner, janus.yaml schema, RepoConfig dataclass, backward compatibility]

## Phase 3: Language-Agnostic Prompts
[Detailed implementation spec for language detection, prompt templates, code extraction generalization]

## Phase 4: Repository Context Generalization
[Detailed spec for making repo_context.py language-aware]

## Phase 5: Multi-Provider LLM (BYOK)
[Detailed spec for LiteLLM integration, provider abstraction, tiered model routing]

## Phase 6: GitHub App & Product Layer
[Detailed spec for webhook handler, command parser, PR comment formatting, fork security]

## Phase 7: Auto-Merge (Enterprise)
[Detailed spec for merge API, janus.yaml auto_merge config, safety constraints]

## Files Changed Summary
[New files table, modified files table, unchanged files table]

## Business Model
[Free/Pro/Enterprise feature gating table]

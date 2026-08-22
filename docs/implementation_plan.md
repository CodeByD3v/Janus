# Janus 2.0 — Implementation Record

**Status: complete and regression-tested.** Janus 2.0 migrated the service from a Patcher-first, Python-specific prototype into a Reviewer-first, language-agnostic, multi-provider adversarial code-review system. The seven phases below are implemented; this document records the resulting architecture rather than an outstanding task list.

For live status and deliberate future work, see [`Roadmap.md`](Roadmap.md). For the runtime architecture and security model, see [`ARCHITECTURE.md`](ARCHITECTURE.md). For setup, deployment, and operations, see [`RUNBOOK.md`](RUNBOOK.md). The detailed visual lifecycle is in [`JANUS_WORKFLOW.mmd`](JANUS_WORKFLOW.mmd).

## Guiding principles

- The Patcher is a responder, not an initiator; it runs only after the Reviewer proves a concrete issue.
- The Reviewer must support `ISSUE_FOUND` with an exact executable counterexample; if pytest does not execute and fail that test, Janus normalizes the verdict to `INCONCLUSIVE` and requests human review. The Reviewer cannot edit source files or call the full gate.
- The deterministic gate has sole merge authority; no LLM verdict can override a failed required check.
- Language-specific validation is repository configuration through `janus.yaml`, not hard-coded debate logic.
- Tenant isolation, secret isolation, sandbox boundaries, and fail-closed behavior are enforced at multiple layers.
- External side effects are best-effort after the debate result is persisted and never retroactively change the gate decision.

## Completed migration phases

| Phase | Implemented result | Primary implementation areas |
|---|---|---|
| **1. Core engine restructure** | Reviewer-first flow with `PASS`, `ISSUE_FOUND`, and `INCONCLUSIVE` verdicts; Patcher runs only for `ISSUE_FOUND`; per-round and final persistence | `core/orchestrator.py`, `storage/models.py` |
| **2. Generic validation interface** | `janus.yaml` custom checks with backward-compatible default checks; configurable repository validation | `core/repo_config.py`, `core/validation.py`, `core/gate.py` |
| **3. Language-agnostic prompts** | Target-language detection, generic code extraction, and prompts that do not assume Python | `core/language.py`, `core/agents.py`, `core/orchestrator.py` |
| **4. Repository-context generalization** | Graceful non-Python behavior plus AST-derived Python callers/callees, conservative fallback matching, source-repository history fallback, and per-round test-convention retrieval | `core/repo_context.py`, `core/orchestrator.py` |
| **5. Multi-provider LLM and BYOK** | Gemini key pooling plus provider-specific model configuration for supported LiteLLM providers; BYOK encrypted at rest and decrypted only in worker memory | `core/llm_client.py`, `core/credentials.py`, `core/config.py` |
| **6. GitHub App and product layer** | HMAC-verified webhooks, installation registration/revocation, manual and opt-in automatic triggers, fork protection, exact-commit materialization, notifications, and tenant-scoped credentials | `api/github_app.py`, `core/github_credentials.py`, `core/github_materializer.py`, `core/notifications.py` |
| **7. Enterprise auto-merge** | Optional SHA-pinned merge only when repository opt-in, deterministic gate, trusted branch, trusted author, and no human-review flag all pass | `core/auto_merge.py`, `core/worker.py`, `core/repo_config.py` |

## Completed hardening and product additions

The post-migration hardening work is also complete and regression-tested:

- MCP-facing repository and target-file validation blocks path traversal and arbitrary host access.
- Containerized gate execution uses network isolation, resource limits, read-only protection, and wall-clock timeouts; Docker unavailability fails closed when container mode is required.
- Gate baseline diffing runs immutable baseline and candidate test suites, ignores only pre-existing parseable pytest failures, and fails closed for invalid or unparseable baselines.
- Webhook SSRF protection validates all resolved addresses and pins the validated public IP for the outbound request while preserving TLS hostname validation; redirects and proxies are disabled.
- GitHub App installation credentials are short-lived, tenant-aware, cached by installation scope, and never persisted as plaintext tokens.
- Blocking database, gate, sandbox, and worker operations are moved off async event loops; LLM deadlines and bounded shielded MCP teardown mitigate the persistence/event-loop freeze risk.
- Admin visibility provides a fail-closed `ADMIN_API_KEYS` role tier, cross-tenant non-sensitive summaries through `GET /admin/debates`, and the `/admin` operator dashboard. Tenant and admin credentials are disjoint.
- Queue claiming is atomic, zombie sessions are swept, per-round state is persisted, and structured logs and Prometheus metrics expose operational outcomes without logging secrets.
- Reviewer evidence is fail-closed, debate controls validate safe bounds, and telemetry exposes verdicts, evidence, repository-context availability, and max-round caps for calibration.
- Curated Python/TypeScript/Go regression fixtures broaden local coverage without being represented as a real-repository benchmark.
- `k8s/` provides an offline-rendered alternative with a dedicated Docker-socket worker, Docker CLI in the service image, explicit API gate isolation, immutable image wiring, and ordered `k8s/deploy.sh` migration rollout.
- `training/dataset.py` and `scripts/prepare_reviewer_dataset.py` validate provenance/evidence-gated records and export provider-neutral JSONL; they do not train or upload a model.

## Verification record

The complete evaluation command runs every `evals/eval_*.py` module. The final non-integration run completed with **254 passed, 3 skipped, 1 deselected integration test, and 4 warnings**. The previous baseline was 224 passed, 4 skipped, and 4 warnings. Skips and deselection are integration cases requiring real external credentials.
GitHub App coverage in `evals/eval_github_app.py` is unit/regression-level with mocked `_github_get` calls; a live installed-App webhook flow against a real repository and public endpoint has not yet been run. Kubernetes validation is offline rendering only. The Mermaid workflow in `JANUS_WORKFLOW.mmd` provides the corresponding end-to-end control-flow view.

## Deliberate future work

**Fine-tuning the Reviewer remains unstarted as a model-training operation.**
The provider-neutral `training/dataset.py` boundary now requires repository/PR/
review/source/fix commit provenance and executable before/after evidence, and
exports chat JSONL without contacting a provider. The 25-example retrieval seed
is intentionally rejected because it lacks this lineage. Revisit fine-tuning
after the retrieval store grows substantially, repository-context signals are
validated across more repositories, and a real fine-tuning-shaped failure is
observed rather than hypothesized.
Optional persistent-environment diagnosis of third-party MCP compatibility
behavior may provide additional root-cause detail, but it is not a prerequisite
for normal operation of the implemented Janus paths.

## Related documentation

- [`AGENTS.md`](../AGENTS.md) — authoritative operating and extension contract.
- [`README.md`](../README.md) — project overview, Quick Start, API usage, deployment, and environment variables.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — live architecture, security, persistence, and testing model.
- [`RUNBOOK.md`](RUNBOOK.md) — operational setup, deployment, lifecycle hardening, and diagnostics.
- [`Roadmap.md`](Roadmap.md) — current status, historical implementation order, and deliberate future work.
- [`JANUS_WORKFLOW.mmd`](JANUS_WORKFLOW.mmd) — detailed Mermaid workflow of the full service lifecycle.
- [`janus_audit_summary.md`](janus_audit_summary.md) — historical security audit with current remediation status.

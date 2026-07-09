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

## ⚠️ Known Unfixed Vulnerabilities (For Your Backlog)

1. **Server-Side Request Forgery (SSRF) in Webhooks**
   * **The Flaw**: In `core/notifications.py`, `requests.post(url)` fires a JSON payload to user-supplied `webhook_url`s.
   * **Impact**: A malicious tenant can supply an internal IP address (e.g., `169.254.169.254/latest/meta-data/` on AWS) as their webhook URL to port-scan your internal VPC or trigger actions on unauthenticated internal microservices.
   * **Recommendation**: Implement an SSRF protection layer (e.g., resolving the URL's IP address and blocking private/loopback/link-local CIDR ranges) before making requests.

2. **Zombie Debate Sessions**
   * **The Flaw**: If a worker process is killed abruptly (e.g., OOM kill, SIGKILL, hardware failure) while processing a debate, the debate's status will remain `running` in the database forever. 
   * **Recommendation**: Implement a heartbeat mechanism or a "zombie sweeper" cron job that resets any debate stuck in `running` for more than 30 minutes back to `queued`.

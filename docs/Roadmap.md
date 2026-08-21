# Janus — Roadmap

This is an honest status document and historical implementation manual.
The seven Janus 2.0 phases and the subsequent hardening work are complete;
the phase sections preserve the design sequence, file locations, signatures,
and data structures that produced the current system. They are not a list of
outstanding implementation tasks.

**Notation**: `L142` means a historical line reference from the v1.0-to-v2.0
migration. Line numbers drift as the repository evolves — use them for quick
navigation, not as stable anchors.

---

## 1. Status at a glance

| Area | Status |
|---|---|
| REST API (enqueue, poll, health, metrics) | Built, verified |
| Deterministic gate + container isolation | Built, verified |
| Gate baseline diffing | Built, verified; only new pytest failures reject a patch |
| Gate check scoping (lint/type/security → target_file) | Built, verified |
| Reviewer counterexample execution (`run_candidate_test`) | Built, verified |
| Behavioral retrieval | Built, verified |
| Repository-context retrieval | Built, verified |
| Multi-key LLM pooling | Built, verified |
| Deploy pipeline (build → push → migrate → roll out → health-check) | Built, verified |
| Notifications (PR comment, webhook) | Built, verified |
| Sandbox-escape fix (MCP-layer repo_dir validation) | Built, verified |
| SSRF protection (webhooks) | Built, verified with validated-IP pinning |
| Zombie-session sweeper | Built, verified |
| `_persist_session_start` upsert fix | Built, verified |
| **Phase 1: Core Engine Restructure** | Implemented and regression-tested |
| **Phase 2: Generic Validation Interface** | Implemented and regression-tested |
| **Phase 3: Language-Agnostic Prompts** | Implemented and regression-tested |
| **Phase 4: Repository Context Generalization** | Implemented; non-Python fallback regression-tested |
| **Phase 5: Multi-Provider LLM / BYOK** | Implemented; provider credential validation hardened |
| **Phase 6: GitHub App & Product Layer** | Implemented; installation-scoped GitHub App auth verified |
| **Phase 7: Auto-Merge** | Implemented; allowlists now fail closed when metadata is absent |

---

## 2. Janus 2.0 Migration Phases

### Phase 1: Core Engine Restructure

> **Goal**: Flip the debate from Patcher-first to Reviewer-first.
> The Patcher becomes a responder — it only runs if the Reviewer finds
> something concrete.

**Effort**: HIGH — this is the product change.
**Files touched**: `core/orchestrator.py`, `storage/models.py`
**Depends on**: nothing (all other phases depend on this)

#### Step 1.1 — Add `ReviewerVerdict` enum

Create in `core/orchestrator.py` near L142, right after the existing
`RoundLog` dataclass:

```python
from enum import Enum

class ReviewerVerdict(str, Enum):
    PASS = "PASS"               # Code is fine, no patcher needed
    ISSUE_FOUND = "ISSUE_FOUND" # Concrete bug, patcher must fix
    INCONCLUSIVE = "INCONCLUSIVE"  # Flag for human review
```

This is a `str` enum so it JSON-serializes directly into DB fields and
API responses without a custom encoder.

#### Step 1.2 — Update `RoundLog` dataclass

The existing `RoundLog` at `core/orchestrator.py` L142-152:

```python
@dataclass
class RoundLog:
    round_num: int
    patch_text: str
    reviewer_text: str
    gate_result: dict[str, Any]
    ...
```

Add:

```python
    reviewer_verdict: str = "ISSUE_FOUND"  # ReviewerVerdict value
```

`patch_text` becomes optional (empty string when verdict is `PASS` or
`INCONCLUSIVE` — the Patcher never ran).

#### Step 1.3 — Update `DebateResult` dataclass

The existing `DebateResult` at `core/orchestrator.py` L155-161:

```python
@dataclass
class DebateResult:
    merged: bool
    rounds: list[RoundLog] = field(default_factory=list)
    final_gate: dict[str, Any] | None = None
    sandbox_path: str | None = None
    cost: dict[str, Any] | None = None
```

Add:

```python
    needs_human_review: bool = False  # True when any round was INCONCLUSIVE
    reviewer_verdict: str = "ISSUE_FOUND"  # Final verdict from last round
```

#### Step 1.4 — Update ORM model `DebateSession`

In `storage/models.py` L50-101, add two columns to `DebateSession`:

```python
    reviewer_verdict: Optional[str] = Column(String(32), nullable=True)  # type: ignore[assignment]
    needs_human_review: Optional[bool] = Column(Boolean, nullable=True, default=False)  # type: ignore[assignment]
```

And add to `Round` (around L140) a column:

```python
    reviewer_verdict: Optional[str] = Column(String(32), nullable=True)  # type: ignore[assignment]
```

**Migration**: after adding columns, run:
```bash
python -c "from storage.db import run_migrations; run_migrations()"
```
Since we use `create_all()` (not Alembic), new columns on SQLite appear
automatically. On PostgreSQL, you'd need `ALTER TABLE ADD COLUMN` —
that's a Phase 6 problem (prod is SQLite until GitHub App).

#### Step 1.5 — Add verdict parsing helper

Add to `core/orchestrator.py`, after `_check_reviewer_wrote_test`:

```python
def _parse_reviewer_verdict(reviewer_text: str) -> ReviewerVerdict:
    """Parse the Reviewer's structured verdict from its response.

    The Reviewer's prompt (see agents.py REVIEWER_INSTRUCTION_TEMPLATE)
    instructs it to end with exactly one of:
      VERDICT: PASS
      VERDICT: ISSUE_FOUND
      VERDICT: INCONCLUSIVE

    Falls back to text heuristics for backward compatibility with the
    v1.0 Reviewer prompt that used 'No further issues found.'
    """
    text_upper = reviewer_text.upper()

    # Structured verdict (v2.0 prompt format)
    if "VERDICT: PASS" in text_upper:
        return ReviewerVerdict.PASS
    if "VERDICT: ISSUE_FOUND" in text_upper:
        return ReviewerVerdict.ISSUE_FOUND
    if "VERDICT: INCONCLUSIVE" in text_upper:
        return ReviewerVerdict.INCONCLUSIVE

    # Backward-compatible heuristic (v1.0 prompt format)
    if "no further issues found" in reviewer_text.lower():
        return ReviewerVerdict.PASS

    # If the Reviewer gave a substantive response but no verdict tag,
    # treat it as ISSUE_FOUND (the conservative choice)
    return ReviewerVerdict.ISSUE_FOUND
```

#### Step 1.6 — Rewrite `_run_debate_inner`

This is the big one. The current flow at L566-885:

```
Current v1.0 flow:
  1. Build Patcher → initial patch prompt → write code
  2. Loop:
     a. Build Reviewer → review prompt → get critique
     b. Run gate
     c. If "no further issues found" → break
     d. Build fix prompt → Patcher fixes → write code
  3. Final gate → merge/reject
```

New v2.0 flow:

```
New v2.0 flow:
  1. Read current code (DON'T build Patcher yet)
  2. Loop:
     a. Build Reviewer → review prompt → get verdict
     b. Parse verdict
     c. If PASS → break (no Patcher needed at all)
     d. If INCONCLUSIVE → set needs_human_review, break
     e. If ISSUE_FOUND:
        i.   Build Patcher (or reuse from prior round)
        ii.  Send fix prompt with the Reviewer's critique
        iii. Extract code, write to sandbox
     f. Run gate
     g. Persist round
  3. Final gate → merge/reject (skip gate if PASS on round 1)
```

The key structural change is: **the Patcher is built lazily, inside the
`ISSUE_FOUND` branch, not before the loop starts.** The initial patch
prompt (L622-627) moves into the `ISSUE_FOUND` branch too.

Here is the exact refactored `_run_debate_inner`. Replace everything from
L566 to L885:

```python
async def _run_debate_inner(
    repo_dir: str,
    target_file: str,
    ticket: str,
    debate_id: str,
    tenant_id: str | None,
    sandbox: Path,
    cost_tracker: CostTracker,
) -> DebateResult:
    from core.repo_context import format_repo_context_for_prompt, retrieve_repo_context
    from core.retrieval import format_examples_for_prompt, retrieve_examples

    sandbox_resolved = sandbox.resolve()
    target_path = (sandbox_resolved / target_file).resolve()

    if not target_path.is_relative_to(sandbox_resolved):
        error_msg = f"Path traversal denied: {target_file} is outside the sandbox"
        logger.error("debate_failed_path_traversal", debate_id=debate_id, error=error_msg)
        await _persist_with_timeout(_persist_session_end, debate_id, False, {}, cost_tracker.to_dict(), str(sandbox), error_msg)
        return DebateResult(merged=False, sandbox_path=str(sandbox))

    try:
        current_code = target_path.read_text()
    except Exception as e:
        error_msg = f"Failed to read target file: {e}"
        logger.error("debate_failed_read_target", debate_id=debate_id, error=error_msg)
        await _persist_with_timeout(_persist_session_end, debate_id, False, {}, cost_tracker.to_dict(), str(sandbox), error_msg)
        return DebateResult(merged=False, sandbox_path=str(sandbox))

    user_id = "service_account"
    result = DebateResult(merged=False, sandbox_path=str(sandbox))

    # Patcher is built LAZILY — only if the Reviewer finds something.
    patcher_runner = None
    patcher_session = None
    patcher_key_index = None

    async def _ensure_patcher():
        """Build or return the existing Patcher agent."""
        nonlocal patcher_runner, patcher_session, patcher_key_index
        if patcher_runner is not None:
            return patcher_runner, patcher_session, patcher_key_index
        agent, idx = build_patcher()
        patcher_runner = InMemoryRunner(agent=agent, app_name=settings.APP_NAME)
        patcher_session = str(uuid.uuid4())
        await patcher_runner.session_service.create_session(
            app_name=settings.APP_NAME, user_id=user_id, session_id=patcher_session
        )
        patcher_key_index = idx
        return patcher_runner, patcher_session, patcher_key_index

    async def _rebuild_patcher():
        nonlocal patcher_runner, patcher_session, patcher_key_index
        agent, idx = build_patcher()
        patcher_runner = InMemoryRunner(agent=agent, app_name=settings.APP_NAME)
        patcher_session = str(uuid.uuid4())
        await patcher_runner.session_service.create_session(
            app_name=settings.APP_NAME, user_id=user_id, session_id=patcher_session
        )
        patcher_key_index = idx
        return patcher_runner, patcher_session, patcher_key_index

    # Snapshot pre-existing test files
    tests_dir = sandbox / "tests"
    pre_existing_tests: set[str] = set()
    if tests_dir.exists():
        pre_existing_tests = {f.name for f in tests_dir.iterdir() if f.is_file()}

    patch_text = ""
    extraction_failed = False
    last_verdict = ReviewerVerdict.ISSUE_FOUND

    for round_num in range(1, settings.MAX_ROUNDS + 1):
        metrics.rounds_total.inc()
        logger.info("round_started", debate_id=debate_id, round_num=round_num)

        # --- REVIEWER PHASE (always runs) ---

        try:
            examples = retrieve_examples(current_code, top_k=3)
        except Exception as e:
            logger.warning("retrieval_failed", debate_id=debate_id, round_num=round_num, error=str(e))
            examples = []

        try:
            repo_context = retrieve_repo_context(str(sandbox), target_file, current_code)
        except Exception as e:
            logger.warning("repo_context_retrieval_failed", debate_id=debate_id, round_num=round_num, error=str(e))
            repo_context = {}

        reviewer_agent, reviewer_key_index = build_reviewer(
            format_examples_for_prompt(examples),
            format_repo_context_for_prompt(repo_context),
        )
        reviewer_runner = InMemoryRunner(agent=reviewer_agent, app_name=settings.APP_NAME)
        reviewer_session = str(uuid.uuid4())
        await reviewer_runner.session_service.create_session(
            app_name=settings.APP_NAME, user_id=user_id, session_id=reviewer_session
        )

        async def _rebuild_reviewer():
            agent, idx = build_reviewer(
                format_examples_for_prompt(examples),
                format_repo_context_for_prompt(repo_context),
            )
            r = InMemoryRunner(agent=agent, app_name=settings.APP_NAME)
            sid = str(uuid.uuid4())
            await r.session_service.create_session(
                app_name=settings.APP_NAME, user_id=user_id, session_id=sid
            )
            return r, sid, idx

        review_prompt = (
            f"Ticket:\n{ticket}\n\n"
            f"Current contents of {target_file} "
            f"(sandbox at {sandbox}):\n```python\n{current_code}\n```\n\n"
            f"The repo root for your tools is: {sandbox}\n"
            f"Review this code. If you find a real issue, write an "
            f"executable counterexample test and run it to confirm it "
            f"fails, then report the failure. End your response with "
            f"exactly one of: VERDICT: PASS, VERDICT: ISSUE_FOUND, "
            f"or VERDICT: INCONCLUSIVE.\n"
            f"If nothing clears the bar, say VERDICT: PASS."
        )

        try:
            reviewer_text, reviewer_runner, reviewer_session, reviewer_key_index = await _ask(
                reviewer_runner, reviewer_session, user_id, review_prompt,
                cost_tracker=cost_tracker,
                key_index=reviewer_key_index,
                rebuild_on_rate_limit=_rebuild_reviewer,
            )
        except RuntimeError as e:
            logger.error("debate_failed_reviewer", debate_id=debate_id, round_num=round_num, error=str(e))
            break

        verdict = _parse_reviewer_verdict(reviewer_text)
        last_verdict = verdict
        skipped_counterexample = _check_reviewer_wrote_test(sandbox, pre_existing_tests, reviewer_text)

        # --- PATCHER PHASE (only on ISSUE_FOUND) ---

        stop_reason = None

        if verdict == ReviewerVerdict.PASS:
            stop_reason = "reviewer_passed"
            logger.info("reviewer_verdict_pass", debate_id=debate_id, round_num=round_num)

        elif verdict == ReviewerVerdict.INCONCLUSIVE:
            stop_reason = "reviewer_inconclusive"
            result.needs_human_review = True
            logger.info("reviewer_verdict_inconclusive", debate_id=debate_id, round_num=round_num)

        elif verdict == ReviewerVerdict.ISSUE_FOUND:
            logger.info("reviewer_verdict_issue_found", debate_id=debate_id, round_num=round_num)

            runner, session, key_idx = await _ensure_patcher()

            fix_prompt = (
                f"Ticket:\n{ticket}\n\n"
                f"Reviewer's critique:\n{reviewer_text}\n\n"
                f"Current contents of {target_file}:\n```python\n{current_code}\n```\n\n"
                f"Fix the issue if it's real, or explain briefly why you're "
                f"pushing back (only once per critique). Propose your patch as "
                f"a full replacement file."
            )

            try:
                patch_text, patcher_runner, patcher_session, patcher_key_index = await _ask(
                    runner, session, user_id, fix_prompt,
                    cost_tracker=cost_tracker,
                    key_index=key_idx,
                    rebuild_on_rate_limit=_rebuild_patcher,
                )
            except RuntimeError as e:
                logger.error("debate_failed_patcher_fix", debate_id=debate_id, round_num=round_num, error=str(e))
                break

            current_code, extraction_failed = _extract_code(patch_text, current_code)
            target_path.write_text(current_code)

        if round_num == settings.MAX_ROUNDS and stop_reason is None:
            stop_reason = "max_rounds_reached"

        # Gate runs every round (gives the Patcher intermediate feedback)
        gate_result = run_full_gate(str(sandbox), target_file)
        for check in gate_result.get("checks", []):
            outcome = f"{check['check']}_{'pass' if check['passed'] else 'fail'}"
            metrics.gate_checks.inc(outcome)

        round_log = RoundLog(
            round_num=round_num,
            patch_text=patch_text,
            reviewer_text=reviewer_text,
            gate_result=gate_result,
            reviewer_verdict=verdict.value,
            retrieved_example_ids=[ex.get("id", "") for ex in examples],
            repo_context_signals={
                "callers": repo_context.get("call_graph", {}).get("callers", []),
                "prior_fix_shas": [f.get("sha", "") for f in repo_context.get("prior_fixes", [])],
                "test_convention_files": len(repo_context.get("test_conventions", [])),
            },
            stop_reason=stop_reason,
            code_extraction_failed=extraction_failed,
            reviewer_skipped_counterexample=skipped_counterexample,
        )
        result.rounds.append(round_log)
        await _persist_with_timeout(_persist_round, debate_id, round_log)

        if stop_reason:
            logger.info("debate_loop_stop", debate_id=debate_id, round_num=round_num, reason=stop_reason)
            break

        # Update pre-existing tests for next round
        if tests_dir.exists():
            pre_existing_tests = {f.name for f in tests_dir.iterdir() if f.is_file()}

    # --- FINAL GATE ---
    final_gate = run_full_gate(str(sandbox), target_file)
    result.final_gate = final_gate
    result.reviewer_verdict = last_verdict.value

    # PASS verdict + gate pass = merge. ISSUE_FOUND + gate pass = merge.
    # INCONCLUSIVE = never auto-merge (needs_human_review).
    if last_verdict == ReviewerVerdict.INCONCLUSIVE:
        result.merged = False
    else:
        result.merged = final_gate["passed"]

    result.cost = cost_tracker.to_dict()

    metrics.debates_completed.inc()
    metrics.rounds_per_debate.observe(len(result.rounds))
    if result.merged:
        metrics.debates_merged.inc()
    else:
        metrics.debates_rejected.inc()

    persisted = await _persist_with_timeout(
        _persist_session_end, debate_id, result.merged, final_gate,
        cost_tracker.to_dict(), str(sandbox),
    )
    if not persisted:
        logger.error(
            "debate_final_state_not_persisted", debate_id=debate_id,
            merged=result.merged,
        )

    logger.info(
        "debate_completed", debate_id=debate_id, merged=result.merged,
        rounds=len(result.rounds), verdict=last_verdict.value,
        cost=cost_tracker.to_dict(),
    )
    return result
```

#### Step 1.7 — Update `_persist_round` and `_persist_session_end`

In `_persist_round` (L445), add `reviewer_verdict` to the `Round`
constructor:

```python
    db_round = Round(
        ...
        reviewer_verdict=round_log.reviewer_verdict,  # NEW
    )
```

In `_persist_session_end` (L472), add `reviewer_verdict` and
`needs_human_review` parameters and persist them.

#### Step 1.8 — Update the Reviewer prompt

In `core/agents.py` L100-148, the `REVIEWER_INSTRUCTION_TEMPLATE` must
instruct the Reviewer to emit a structured verdict. Append to the end of
the template, before the closing `"""`:

```
- End your review with exactly one verdict line:
  VERDICT: PASS — if the code is correct and you found nothing real
  VERDICT: ISSUE_FOUND — if you found and proved a concrete defect
  VERDICT: INCONCLUSIVE — if you suspect a problem but cannot prove it
    with an executable test (e.g., a design concern, a performance issue
    you can't benchmark in-sandbox, or a concurrency issue that requires
    specific timing). This flags the PR for human review.
```

#### Step 1.9 — Verification

```bash
# 1. Import check
python -c "from core.orchestrator import ReviewerVerdict, _parse_reviewer_verdict; print('ok')"

# 2. Verdict parsing unit tests (add to evals/eval_orchestrator.py)
# Test all three explicit verdicts + the backward-compat heuristic

# 3. Full integration test (needs GOOGLE_API_KEY)
pytest evals/eval_reviewer.py -v -m integration

# 4. Confirm no regressions in existing evals
pytest evals/eval_gate.py -v
pytest evals/eval_api.py -v
```

---

### Phase 2: Generic Validation Interface

> **Goal**: Replace the four hardcoded Python tool calls
> (`ruff`, `mypy`, `pytest`, `bandit`) with a configurable list driven by
> `janus.yaml`. Keep the existing calls as defaults when no config exists.

**Effort**: MEDIUM
**Files touched**: `core/validation.py` (new), `core/repo_config.py` (new), `core/gate.py`
**Depends on**: nothing (can be done in parallel with Phase 1)

#### Step 2.1 — Create `core/repo_config.py`

This is the `janus.yaml` loader. Minimal, no magic:

```python
"""repo_config.py — per-repo configuration via janus.yaml."""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass
class CheckConfig:
    """A single validation check from janus.yaml."""
    name: str
    command: str       # Shell command, e.g. "ruff check ."
    timeout: int = 60  # Seconds

@dataclass
class RepoConfig:
    """Parsed janus.yaml. All fields have sensible defaults."""
    checks: list[CheckConfig] = field(default_factory=list)
    language: str = ""  # Auto-detected if empty (Phase 3)
    # Phase 7 fields — not used yet, but reserved in the schema:
    auto_merge: bool = False
    trigger: str = "manual"  # "manual" | "automatic"

    @classmethod
    def from_yaml(cls, path: Path) -> RepoConfig:
        """Load from a janus.yaml file. Returns empty config on any error."""
        try:
            with open(path) as f:
                raw: dict[str, Any] = yaml.safe_load(f) or {}
        except Exception:
            return cls()

        checks = []
        for c in raw.get("validation", {}).get("checks", []):
            if isinstance(c, dict) and "name" in c and "command" in c:
                checks.append(CheckConfig(
                    name=c["name"],
                    command=c["command"],
                    timeout=int(c.get("timeout", 60)),
                ))

        return cls(
            checks=checks,
            language=raw.get("language", ""),
            auto_merge=bool(raw.get("auto_merge", False)),
            trigger=raw.get("trigger", "manual"),
        )

def load_repo_config(repo_dir: str) -> RepoConfig:
    """Load janus.yaml from a repo, or return defaults."""
    path = Path(repo_dir) / "janus.yaml"
    if path.exists():
        return RepoConfig.from_yaml(path)
    return RepoConfig()
```

#### Step 2.2 — Create `core/validation.py`

This is the generic runner. It replaces the hardcoded `run_linter`,
`run_type_check`, etc. with a single `run_checks()` that takes a list of
`CheckConfig`:

```python
"""validation.py — generic validation runner.

Executes arbitrary shell commands from janus.yaml checks with the same
container isolation and path safety that gate.py already provides.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.gate import _run  # Reuse existing container/direct execution
from core.repo_config import CheckConfig, RepoConfig, load_repo_config

@dataclass
class CheckResult:
    check: str
    passed: bool
    detail: str

def run_check(check: CheckConfig, repo_dir: Path) -> CheckResult:
    """Run a single validation check."""
    # Split the command string into args for subprocess
    cmd = check.command.split()
    returncode, output = _run(cmd, cwd=repo_dir, timeout=check.timeout)
    return CheckResult(
        check=check.name,
        passed=(returncode == 0),
        detail=output or "clean",
    )

def run_checks(
    repo_dir: str,
    config: RepoConfig | None = None,
    target_file: str | None = None,
) -> dict[str, Any]:
    """Run all validation checks. Returns the same contract as run_full_gate."""
    if config is None:
        config = load_repo_config(repo_dir)

    results = []
    for check_config in config.checks:
        result = run_check(check_config, Path(repo_dir))
        results.append({
            "check": result.check,
            "passed": result.passed,
            "detail": result.detail,
        })

    all_passed = all(r["passed"] for r in results)
    return {"passed": all_passed, "checks": results}
```

#### Step 2.3 — Wire into `core/gate.py`

The existing `run_full_gate` at `core/gate.py` (around L380) currently
hardcodes the four checks. Modify it to:

1. Call `load_repo_config(repo_dir)` at the top
2. If `config.checks` is non-empty → delegate to `validation.run_checks()`
3. If empty (no janus.yaml or no checks section) → run the existing
   hardcoded `run_linter`, `run_type_check`, `run_tests`,
   `run_security_scan` as today

This keeps backward compatibility: every existing repo without a
`janus.yaml` works exactly as before.

```python
def run_full_gate(repo_dir: str, target_file: str | None = None) -> dict:
    # ... existing path validation ...

    config = load_repo_config(repo_dir)
    if config.checks:
        # janus.yaml defines custom checks — use those
        return run_checks(repo_dir, config, target_file)

    # Default: hardcoded Python checks (backward compatible)
    checks = [
        run_linter(repo_dir, target_file),
        run_type_check(repo_dir, target_file),
        run_tests(repo_dir),
        run_security_scan(repo_dir, target_file),
    ]
    all_passed = all(c["passed"] for c in checks)
    return {"passed": all_passed, "checks": checks}
```

#### Step 2.4 — Add `pyyaml` dependency

In `requirements.txt`, add:
```
pyyaml>=6.0
```

#### Step 2.5 — Verification

```bash
# Unit test: no janus.yaml → existing defaults
pytest evals/eval_gate.py -v

# Unit test: with janus.yaml → custom checks
# Create a test fixture:
mkdir /tmp/test_repo && echo "print('ok')" > /tmp/test_repo/app.py
cat > /tmp/test_repo/janus.yaml << 'EOF'
validation:
  checks:
    - name: syntax
      command: python -m py_compile app.py
EOF
python -c "
from core.repo_config import load_repo_config
c = load_repo_config('/tmp/test_repo')
print(f'Checks: {[ch.name for ch in c.checks]}')
assert c.checks[0].name == 'syntax'
print('ok')
"
```

---

### Phase 3: Language-Agnostic Prompts

> **Goal**: Replace every `python`-specific string in prompts and code
> extraction with a `{language}` template variable.

**Effort**: LOW
**Files touched**: `core/language.py` (new), `core/agents.py`, `core/orchestrator.py`
**Depends on**: nothing (can be done in parallel with Phase 1 and 2)

#### Step 3.1 — Create `core/language.py`

```python
"""language.py — detect programming language from file extension."""

_EXTENSION_MAP = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".jsx": "JavaScript (JSX)",
    ".tsx": "TypeScript (TSX)",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".cs": "C#",
    ".cpp": "C++",
    ".c": "C",
    ".swift": "Swift",
    ".php": "PHP",
}

def detect_language(filename: str) -> str:
    """Detect language from file extension. Returns 'Unknown' if unrecognized."""
    from pathlib import Path
    ext = Path(filename).suffix.lower()
    return _EXTENSION_MAP.get(ext, "Unknown")
```

#### Step 3.2 — Templatize `PATCHER_INSTRUCTION`

In `core/agents.py` L81-98, the current prompt says:

```
as a fenced python code block
```

Replace with:

```
as a fenced {language} code block
```

And change `build_patcher()` signature:

```python
def build_patcher(language: str = "Python") -> tuple[LlmAgent, int]:
    instruction = PATCHER_INSTRUCTION.format(language=language.lower())
    ...
```

#### Step 3.3 — Templatize `REVIEWER_INSTRUCTION_TEMPLATE`

Add `{language}` to the template where it currently says `python`:

```
- IGNORE: ... anything ruff/mypy would already catch
+ - IGNORE: ... anything static analysis tools configured in the
+   gate would already catch
```

The template gains a third slot: `{language}`.

#### Step 3.4 — Generalize `CODE_BLOCK_RE`

In `core/orchestrator.py` L62:

```python
# Current (Python-only):
CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)

# New (any language):
CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\s*\n(.*?)```", re.DOTALL)
```

#### Step 3.5 — Thread `language` through `run_debate`

In `core/orchestrator.py`, `_run_debate_inner`:

```python
from core.language import detect_language

language = detect_language(target_file)

# When building reviewer:
build_reviewer(
    format_examples_for_prompt(examples),
    format_repo_context_for_prompt(repo_context),
    language=language,
)

# When building patcher:
build_patcher(language=language)
```

#### Step 3.6 — Also check `janus.yaml` for language override

If Phase 2 is done, `RepoConfig.language` takes precedence over
extension-based detection:

```python
config = load_repo_config(str(sandbox))
language = config.language or detect_language(target_file)
```

#### Step 3.7 — Verification

```bash
python -c "
from core.language import detect_language
assert detect_language('app.py') == 'Python'
assert detect_language('index.ts') == 'TypeScript'
assert detect_language('Makefile') == 'Unknown'
print('ok')
"
```

---

### Phase 4: Repository Context Generalization

> **Goal**: Make `core/repo_context.py` work for non-Python repos.

**Effort**: MEDIUM
**Files touched**: `core/repo_context.py`, `core/repo_config.py`
**Depends on**: Phase 3 (needs language detection)

#### Step 4.1 — Generalize `_find_call_graph_neighbors`

Currently at `core/repo_context.py` L60+, this uses `ast.parse` (Python
only). Change to:

1. Try `ast.parse` first — if it works, use the existing AST-based logic
2. If `ast.parse` raises `SyntaxError` (non-Python file), fall back to
   text-based name scanning (grep for the function/class name in other
   files with the same extension)
3. The text-based fallback is already roughly what the function does for
   "callers" — extend it to work on any file extension

#### Step 4.2 — Generalize `_find_test_conventions`

Currently scans for `tests/` directory only. Add configurable patterns:

```python
# In repo_config.py, add to RepoConfig:
    test_patterns: list[str] = field(default_factory=lambda: ["tests", "test", "testing", "__tests__", "spec"])
```

In `_find_test_conventions`, use `config.test_patterns` instead of the
hardcoded `settings.REPO_CONTEXT_TEST_DIR_NAMES`.

#### Step 4.3 — Verification

```bash
pytest evals/eval_repo_context.py -v
# Should still pass — Python repos are the common case
```

---

### Phase 5: Multi-Provider LLM / BYOK

> **Goal**: Support Claude, GPT, Groq, and other providers alongside
> Gemini, using ADK's LiteLLM wrapper.

**Effort**: MEDIUM (HIGH if ADK/LiteLLM is incompatible with MCP tools)
**Files touched**: `core/llm_client.py`, `core/config.py`, `core/agents.py`
**Depends on**: nothing technically, but Phase 1 should land first (the
debate loop changes are the higher-risk refactor)

#### Step 5.0 — PREREQUISITE: Verify ADK + LiteLLM + MCP compatibility

**Before writing any code**, run this test:

```python
"""Does ADK's LiteLlm wrapper support MCP tool calls?"""
from google.adk.models import LiteLlm
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import MCPToolset

# Try to build an agent with LiteLlm + MCP tools
model = LiteLlm(model="openai/gpt-4o-mini")
agent = LlmAgent(
    model=model,
    name="test",
    instruction="Test",
    tools=[MCPToolset(...)],  # use any MCP toolset
)
# If this raises or tool calls don't work → STOP.
# BYOK requires a different approach (e.g., direct OpenAI/Anthropic
# SDK wrapped in ADK's BaseLlm interface).
```

If this works → proceed with Steps 5.1-5.5.
If this doesn't → document the failure and escalate the design decision.

#### Step 5.1 — Add `ModelConfig` to `core/config.py`

```python
@dataclass
class ModelConfig:
    """Which model to use for a debate. Decoupled from debate logic."""
    provider: str = "google"      # "google" | "openai" | "anthropic" | "groq"
    model_name: str = "gemini-2.5-flash"
    api_key: str = ""             # BYOK key (empty = use Janus-managed key)
```

Add to `Settings`:

```python
    OPENAI_API_KEY: str = field(default_factory=lambda: _optional("OPENAI_API_KEY", ""))
    ANTHROPIC_API_KEY: str = field(default_factory=lambda: _optional("ANTHROPIC_API_KEY", ""))
```

#### Step 5.2 — Extend `build_model()` in `core/llm_client.py`

The current `build_model` at `core/llm_client.py` (around L150) only
returns `_KeyedGemini` instances. Extend it:

```python
def build_model(
    model_name: str,
    provider: str = "google",
    api_key: str = "",
) -> tuple[Any, int]:
    """Build a model instance for the given provider.

    Returns (model, key_index). key_index is -1 for non-Google providers
    (they don't use the KeyPool).
    """
    if provider == "google":
        # Existing KeyPool logic
        pool = get_key_pool()
        key, index = pool.draw()
        return _KeyedGemini(model=model_name, bound_api_key=key), index

    # Non-Google providers via LiteLLM
    from google.adk.models import LiteLlm

    litellm_model_name = f"{provider}/{model_name}"
    if api_key:
        # BYOK: pass key directly
        return LiteLlm(model=litellm_model_name, api_key=api_key), -1
    else:
        # Use environment variable (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
        return LiteLlm(model=litellm_model_name), -1
```

#### Step 5.3 — Thread `ModelConfig` through agent builders

In `core/agents.py`, update `build_patcher` and `build_reviewer`:

```python
def build_patcher(
    language: str = "Python",
    model_config: ModelConfig | None = None,
) -> tuple[LlmAgent, int]:
    config = model_config or ModelConfig(model_name=settings.MODEL)
    model, key_index = build_model(
        config.model_name, config.provider, config.api_key
    )
    ...
```

#### Step 5.4 — Thread `ModelConfig` through `run_debate`

Add `model_config: ModelConfig | None = None` parameter to
`run_debate()` and `_run_debate_inner()`. Pass it through to
`build_patcher()` and `build_reviewer()`.

The API layer resolves which `ModelConfig` to use based on the request
(free tier → default, BYOK → tenant's key). The debate engine itself
never knows which tier the user is on — **Hard Rule 12**.

#### Step 5.5 — Verification

```bash
# Google (existing, should not regress):
GOOGLE_API_KEYS="dummy-a,dummy-b" pytest evals/eval_llm_client.py -v

# OpenAI (needs a real key):
OPENAI_API_KEY=sk-... python -c "
from core.llm_client import build_model
model, idx = build_model('gpt-4o-mini', provider='openai')
print(f'Model: {model}, index: {idx}')
"
```

---

### Phase 6: GitHub App & Product Layer

> **Goal**: Make Janus usable as a GitHub App triggered by PR comments.

**Effort**: HIGH
**Files touched**: `api/github_app.py` (new), `core/notifications.py`, `storage/models.py`
**Depends on**: Phases 1-3 (the engine must be Reviewer-first and
language-agnostic before exposing it as a product)

#### Key design decisions

1. **Manual trigger by default**: `/janus review` comment on a PR.
   Automatic on-push is opt-in via `janus.yaml` `trigger: automatic`.
2. **Fork PRs are untrusted** (Hard Rule 10): no sandbox execution
   without explicit maintainer approval.
3. **Results posted as PR comments**: structured markdown with per-check
   status, Reviewer verdicts, and links to the full debate.
4. **Installation stored per-tenant**: `storage/models.py` gets a
   `GithubInstallation` model.

This phase is the most product-facing and should be designed carefully
before implementation begins. Use `/grill-me` to align on the exact
PR comment format, the installation flow, and the scope of the initial
GitHub App permissions.

---

### Phase 7: Auto-Merge (Enterprise)

> **Goal**: Optionally merge the PR if the gate passes and the Reviewer
> said PASS.

**Effort**: MEDIUM
**Files touched**: `core/notifications.py`, `core/repo_config.py`
**Depends on**: Phase 6 (needs GitHub App)

#### Key constraints

- **Never auto-merge if `needs_human_review` is True** (INCONCLUSIVE verdict)
- Only enabled when `janus.yaml` has `auto_merge: true`
- Only on branches matching a configurable pattern (e.g., `dependabot/*`)
- Only on PRs from trusted authors (not forks — Hard Rule 10)
- Requires the GitHub App to have `contents: write` permission

---

## 3. Remaining deployment hardening

The application-level Janus 2.0 phases are implemented. GitHub App
installation-scoped authentication is now implemented in
`core/github_credentials.py`: short-lived tokens are minted per installation,
cached by `(tenant_id, installation_id)`, and used by repository materialization,
notifications, and auto-merge. The App private key remains outside the
application database behind the `SecretStore` boundary. A static
`GITHUB_TOKEN` remains only as a legacy fallback for unscoped single-tenant
operation.

The deterministic gate, fork protection, trigger configuration, and auto-merge
authorization remain independent of the credential-provider seam.

## 4. Async lifecycle hardening — mitigation implemented

The original `_persist_session_end` observation remains documented below as a
historical diagnostic finding. The runtime now removes the known event-loop
blocking paths: persistence, worker queue claims, zombie sweeps, worker session
reads, and worker error updates run in threads; each LLM/MCP stream has a
configured deadline; and MCP teardown is bounded without cancelling potentially
stuck anyio subprocess cleanup in the active event loop. Regression tests cover
heartbeat continuity and bounded sync/async cleanup. A persistent environment
with py-spy remains useful for confirming the underlying third-party MCP stack
root cause, but the worker no longer waits indefinitely on these paths.

**`_persist_session_end` was observed to not complete within the full
worker process, in a live end-to-end test, after a real (failing) LLM call
sequence.**

**Update after a second diagnostic pass**: the underlying blocking DB calls
(`_persist_session_start`, `_persist_round`, `_persist_session_end`) are now
routed through a new `_persist_with_timeout` helper (`core/orchestrator.py`)
that runs them via `asyncio.to_thread` and wraps that in
`asyncio.wait_for(timeout=5.0)` — a genuine improvement regardless of this
mystery's outcome, since a blocking synchronous DB call invoked directly
inside an `async def` function stalls the *entire* event loop for its
duration, hang or not. This was verified in isolation to work correctly:
a fast call succeeds, a deliberately-hung call is cut off at exactly the
timeout and logged (`persist_call_timed_out`), and a raising call is caught
and logged (`persist_call_failed`) — all confirmed with a synthetic
`time.sleep`-based test, not assumed.

**Re-run live against the real worker with this fix in place — twice.**
Neither `persist_call_timed_out`, `persist_call_failed`, nor the normal
success log line (`debate_session_completed`) appeared in either run, even
though:
- The first re-run gave ~13 seconds of margin between the logged failure
  and my kill — too close to the (then 15s) timeout to be conclusive.
- After shortening the timeout to 5 seconds specifically to remove that
  ambiguity, the second re-run gave **over 100 seconds** of margin between
  the logged failure and my kill. The timeout should have fired and logged
  with enormous margin to spare. It did not.

This is a materially different, more concerning result than the first
diagnostic pass suggested. It rules out:
- A simple hang inside the persistence function itself (already ruled out
  by the isolated test).
- Simple SQLite lock contention under default settings (would raise
  quickly, not hang past a 5-second `asyncio.wait_for`).
- My own kill timing being the sole confound (100+ seconds of margin with
  a short timeout and still nothing).

**What's newly suspected**: since even `asyncio.wait_for`'s own timeout
mechanism — which relies on the event loop remaining responsive enough to
schedule and fire a timer callback — did not fire, the leading hypothesis
has shifted from "this one function call hangs" to **"something makes the
worker's asyncio event loop itself stop servicing callbacks after a failed
LLM call sequence involving the MCP subprocess"**. If the event loop itself
is not being scheduled, no amount of wrapping the *specific* call in a
timeout would help, because the mechanism that enforces that timeout is
itself starved. This is a materially different, and more serious, class of
bug than originally suspected — worth stating plainly rather than
downplaying.

A raw, synchronous, `os.fsync`-flushed file-write marker (bypassing both
the logging framework and asyncio entirely) was added immediately around
the call as a maximally direct diagnostic, to determine whether execution
reaches the call at all versus hangs inside it. This diagnostic was not
completed — a separate instability in the sandboxed test environment
itself (tool invocations began timing out independent of this specific
test, including on trivial commands) interrupted the investigation before
a result was captured. The instrumentation was removed rather than left in
the codebase, since a hardcoded `/tmp/janus_diag.log` write is not
appropriate to ship, but the finding above (the timeout mechanism itself
not firing) stands on its own as real diagnostic signal, independent of
that incomplete last step.

**Why this wasn't (and likely can't be, here) fully resolved**: reproducing
this reliably requires observing an undisturbed worker process for the
debate's full natural cycle in an environment that (a) doesn't tear down
background processes between observation windows, and (b) doesn't itself
become unstable under repeated heavy test iterations. Neither held reliably
in the sandbox this was diagnosed in.

**Optional historical diagnostic, if third-party root-cause data is needed**:
1. Reproduce in a persistent terminal (a real dev machine or long-lived CI
   job) with a real `GOOGLE_API_KEY`, and attach `py-spy dump` (or
   equivalent) to the worker process if it appears stuck — this inspects
   the actual Python + native stack of a running process directly, which
   would show definitively whether the event loop is blocked and on what,
   rather than continuing to infer it indirectly through log absence.
2. If `py-spy` isn't available, the raw-file-write diagnostic described
   above (re-added temporarily) is the next-best signal — it doesn't
   depend on the logging framework or asyncio machinery, both of which are
   plausible suspects.
3. If a root cause is eventually isolated, record it as a third-party
   MCP lifecycle compatibility note. The Janus runtime paths already use
   thread-offloaded persistence, configured LLM deadlines, and bounded
   shielded MCP cleanup.

**Ready-to-run reproduction script**: `scripts/reproduce_s2.py` automates
the full reproduction sequence — starts the API, enqueues a debate against
`demo_repo`, starts a worker subprocess, streams all logs, watches for the
specific §2 log events, and prints the exact `py-spy dump` command with
the worker PID if the debate appears stuck. See `scripts/README_reproduce_s2.md`
for usage.

**Current implementation status**: all three persistence calls now go
through `_persist_with_timeout` instead of raw blocking calls. Worker database
operations, gate execution, sandbox operations, and MCP teardown are likewise
bounded or thread-offloaded, and regression tests cover event-loop continuity.
The original observation is retained above for historical traceability, not as
an unresolved Janus production path.

## 4. Deferred items carried forward

### DNS rebinding on webhooks — resolved
`post_webhook` resolves and validates every address returned for the destination
hostname, then passes the selected public IP to a custom Requests/urllib3
transport adapter. The adapter connects to that IP directly while preserving
the original hostname for HTTP Host and HTTPS SNI/certificate validation.
Redirects and environment proxies are disabled, so the request cannot escape
the validated destination through either mechanism. Regression coverage proves
that the transport receives the validated IP and does not perform a second
hostname resolution.

### Fine-tuning the Reviewer
The target architecture, once ready, is three layers:

```
Repo-Context Retrieval  →  Behavioral Retrieval  →  Fine-tuned Reviewer LLM  →  Executable proof
   (built)                    (built)                  (not started)              (built, via the gate)
```

Each layer fixes a different failure mode and none substitutes for the
others — repo-context retrieval gives facts about *this* codebase a generic
model can't know; behavioral retrieval gives a sense of what a real catch
looks like without needing fine-tuned weights; fine-tuning would give the
*skill* of reviewing well as a learned prior instead of few-shot-prompted
behavior, at the cost of being expensive to build and prone to going stale
without retrieval alongside it.

**Why deferred, specifically**: both retrieval layers exist but neither is
mature enough that the next unit of effort is better spent on fine-tuning
than on hardening them. The behavioral store is a 25-example seed set. The
repo-context call graph is name-based text scanning, not a resolved static
analysis pass. Growing and hardening those is a better investment right now
than starting a fine-tuning effort on top of an immature retrieval
foundation.

**Revisit when**: the retrieval store has grown substantially past its seed
set (via `retrieval_pipeline/ingest.py`) and the repo-context signals have
been validated against a wider range of real repos without major gaps —
or when a fine-tuning-shaped problem (systematic Reviewer weaknesses that
retrieval can't fix, only learned judgment can) is actually observed in
practice, not hypothesized.

### Admin dashboard / cross-tenant visibility — implemented
Janus now provides an admin-only `GET /admin/debates` endpoint with tenant,
status, pagination, and limit filters, plus a same-origin `/admin` operator
dashboard. Ordinary tenant keys remain strictly isolated and receive `403`
for admin routes.

Admin credentials are loaded from `ADMIN_API_KEYS` as hashed in-memory key
metadata with an explicit `admin` role. Empty configuration disables admin
access. A credential cannot be registered in both the tenant and admin role;
role collisions are removed and fail closed. Responses contain only
non-sensitive debate summaries and exclude tickets, webhook URLs, encrypted
BYOK material, round transcripts, and gate command details.

### Gate baseline diffing — implemented
`run_tests` still executes the full suite, unscoped, because narrowing runtime
coverage to a target file would be unsound. Debate execution now creates an
immutable pre-patch sandbox and passes it to `run_full_gate` as
`baseline_repo_dir`. The gate runs the baseline suite and candidate suite,
extracts stable pytest node IDs, and rejects only failures newly introduced by
the candidate patch. Pre-existing failures are retained as diagnostic metadata
but do not unfairly reject the patch.

Baseline collection and infrastructure failures without parseable pytest
failure IDs remain fail-closed. Invalid baseline paths are rejected before any
validation runs. The standalone and MCP-facing gate calls remain backward
compatible: without a baseline, they preserve the original all-tests-must-pass
behavior. Regression tests cover pre-existing failures, newly introduced
failures, unparseable baseline failures, and invalid baseline paths.

## 5. Historical implementation order

The dependency graph below records how Janus 2.0 was implemented. It is
provided for architectural history; all seven phases and the subsequent
hardening work shown here are complete and regression-tested.

```
Phase 1 (Reviewer-first)
    │
    ├── Phase 2 (Generic validation)
    │       │
    │       └── Phase 4 (Repository context)
    │
    ├── Phase 3 (Language-agnostic prompts)
    │
    └── Phase 5 (Multi-provider BYOK)
            │
            └── Phase 6 (GitHub App)
                    │
                    └── Phase 7 (Auto-merge)
```

The current product backlog is intentionally narrower: grow and validate the
retrieval foundations before considering Reviewer fine-tuning. Optional live
MCP diagnosis may still be performed in a persistent environment, but it is
not a prerequisite for the implemented Janus 2.0 service.

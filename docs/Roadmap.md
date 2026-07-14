# Janus — Roadmap

This is an honest status document, not a pitch. Every item below is
categorized by what it actually is: built and verified, deliberately
deferred (a decision, not an oversight), or genuinely unresolved. See
`ARCHITECTURE.md` for how the built parts actually work.

---

## 1. Status at a glance

| Area | Status |
|---|---|
| REST API (enqueue, poll, health, metrics) | Built, verified |
| Deterministic gate + container isolation | Built, verified |
| Gate check scoping (lint/type/security → target_file) | Built, verified |
| Reviewer counterexample execution (`run_candidate_test`) | Built, verified |
| Behavioral retrieval | Built, verified |
| Repository-context retrieval | Built, verified |
| Multi-key LLM pooling | Built, verified |
| Deploy pipeline (build → push → migrate → roll out → health-check) | Built, verified |
| Notifications (PR comment, webhook) | Built, verified |
| Sandbox-escape fix (MCP-layer repo_dir validation) | Built, verified |
| SSRF protection (webhooks) | Built, verified (DNS rebinding excluded — see §3) |
| Zombie-session sweeper | Built, verified |
| `_persist_session_start` upsert fix | Built, verified |
| `_persist_session_end` full-worker-context reliability | **Unresolved — see §2** |
| Fine-tuning the Reviewer | Deferred — see §4 |
| Admin dashboard | Deferred — see §5 |
| Gate baseline diffing (pre-existing debt vs. patch regressions) | Deferred — see §6 |
| GitHub App / CI-CD step / IDE extension / CLI | Deferred — see §7 |

---

## 2. Unresolved — needs investigation in a real environment

**`_persist_session_end` was observed to not complete within the full
worker process, in a live end-to-end test, after a real (failing) LLM call
sequence.**

What's confirmed:
- The function itself is correct: called in isolation (same async event
  loop, same code path, no prior activity), it completes in milliseconds.
- The upsert fix for `_persist_session_start` is genuinely correct and
  verified — its own log line (`debate_session_persisted`) appears
  correctly in every real run.
- The failure is consistent across three separate full runs: the debate
  correctly reaches the retry-exhausted `RuntimeError`, logs it, and then
  the following `_persist_session_end` call does not appear to complete —
  no success log, no exception log, and the database confirms the write
  never landed (`status` stays `running`, `error_message` stays `None`).

What's ruled out:
- A genuine deadlock in the function itself (isolated test disproves this).
- Simple SQLite lock contention with default settings (would raise an
  immediate `OperationalError`, not hang silently — none was observed).
- Log buffering masking a real success (the *database state itself*, not
  just the log, confirms the write didn't happen).

What's suspected but not proven: something about the interaction between
the MCP subprocess (spawned per-agent via `MCPToolset`, connected over
stdio) and the async worker process, specifically after that subprocess has
been involved in a failed/retried LLM call sequence, leaves something in a
state that blocks or significantly delays the subsequent synchronous DB
write.

**Why this wasn't fully resolved in this environment**: reproducing it
reliably requires observing an undisturbed worker process for the debate's
full natural failure cycle (~90-100 seconds in the environment where this
was found, dominated by a one-time slow connection to the LLM API — not
the retry logic itself, which is fast). The sandbox this was diagnosed in
tears down backgrounded processes between tool invocations, meaning every
observation window was bounded by a single tool call's execution limit,
which sat right at the edge of the natural cycle time. Every kill sent to
end an observation window is a potential confound for what's already a
timing-sensitive bug.

**Next step**: reproduce in a persistent terminal (a real dev machine or a
long-lived CI job), with a real `GOOGLE_API_KEY`, letting the worker run
completely undisturbed through at least one full debate failure. If the
hang reproduces there too, instrument `_persist_session_end` and the
MCPToolset's connection lifecycle directly (e.g. `asyncio.wait_for` with a
short timeout around the DB call, to at least convert a silent hang into a
loud, diagnosable timeout) rather than working around it blind.

**Mitigation already in place**: the call is now wrapped in
`try/except`, so if it *does* raise (as opposed to hang), the failure is
logged (`persist_session_end_failed_after_initial_patch`) instead of
silently disappearing. This does not fix the underlying issue — it only
ensures a raised exception isn't lost. A genuine hang is not helped by
this wrapper at all, which is exactly why this remains open, not closed.

---

## 3. Deferred, not closed: DNS rebinding on webhooks

`post_webhook`'s SSRF protection resolves the destination hostname and
rejects private/internal addresses before making the request. This closes
the direct attack (supplying an internal address as the webhook URL
outright) but not DNS rebinding — a hostname resolves to a safe address at
check time, then a malicious or compromised DNS server returns a different,
internal address at the moment the actual request is made.

Closing this fully requires pinning the specific IP validated by the safety
check and connecting to *that* address directly (a custom `requests`
transport adapter), rather than letting the HTTP client re-resolve DNS
independently. Real, self-contained work — not started.

---

## 4. Deferred: fine-tuning the Reviewer

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

---

## 5. Deferred: admin dashboard / cross-tenant visibility

There is currently no admin role and no way to see system-wide activity
across tenants — `GET /debates/{id}` is deliberately tenant-isolated, by
design, and there's no `GET /admin/debates` list endpoint at all.

This was scoped out explicitly, not forgotten: building it means an admin
key tier in `api/auth.py`, list/filter endpoints that bypass tenant
isolation for that role specifically, and — since raw JSON is a poor fit
for "one operator scanning system-wide activity" — some minimal UI to
actually look at the data, which is a meaningfully different kind of work
from everything else in this project so far.

**Revisit when**: there's an actual operator who needs this, not before —
building visibility tooling for a user that doesn't exist yet is exactly
the kind of premature breadth this project has otherwise avoided.

---

## 6. Deferred: gate baseline diffing

`run_tests` runs the full suite, unscoped, by design (see
`ARCHITECTURE.md` §5.3 for why scoping it would be unsound). The
consequence: a repo with pre-existing failing tests unrelated to any patch
can never pass the gate — confirmed concretely against a real external
repo (`pytest-dev/pluggy`, which fails 5 tests on its own unmodified
`main`).

The correct fix is a different mechanism entirely from scoping: run the
gate once against the *unpatched* code at debate start, capture that as a
baseline, and only fail the final gate on genuinely *new* failures the
patch introduced. This changes `run_full_gate`'s contract (it needs a
baseline to diff against, not just a single snapshot) and costs an extra
full gate run per debate — real, scoped work, not a quick patch.

**Revisit when**: this is prioritized against the other open items —
it's a real product decision (how much gate cost per debate is acceptable
for this correctness gain), not purely an engineering one.

---

## 7. Deferred: everything beyond the REST API

Today the only supported way to use Janus is direct REST calls to
`POST /debates` / `GET /debates/{id}` (plus the optional PR-comment/webhook
side effects). Plausible future integrations, **none built**:

- **GitHub App** — richer than the current PAT-based PR comment
  (`core/notifications.py`), which deliberately uses the Issues API instead
  of Check Runs because a GitHub App needs an installation-token auth flow
  that doesn't exist yet. By far the largest of the four — registering the
  app, building the token exchange, webhook signature verification.
- **CI/CD step** — a packaged action that calls the REST API and fails the
  build on `merged: false`. Smallest of the four; reuses the API exactly as
  it exists today, no new auth model.
- **IDE extension** — send the open file, surface Reviewer findings
  inline. Biggest lift by far — a whole separate codebase with its own
  marketplace publishing pipeline.
- **CLI** — a packaged command-line tool for batch/scripted audits across
  a monorepo.

**Why none are built**: each is a separate consumer of the same engine, not
an improvement to the engine itself — they make Janus reachable from more
places without making the core review mechanism any better. That's only a
good trade once the core has actually been proven under real use, and as
of this document, it has had exactly one real end-to-end run (see
`ARCHITECTURE.md` §9.3 and §2 above) — building four separate front doors
onto a core that's been live-tested once is solving a problem ("nobody can
reach this") this project doesn't have yet, ahead of the problem it does
have ("does the core actually work reliably").

**If picking one to build next**: CI/CD step first — smallest, reuses the
existing API and auth model with zero new complexity, and "does this catch
a real bug in a real CI run" is a more meaningful validation of the whole
project than any UI would be.

---

## 8. Suggested next steps, in order

1. **Resolve §2** in a persistent, real environment — this is the one
   thing standing between "the core mechanism has been proven" and "it
   hasn't."
2. Run a handful more real debates (not just one) against real,
   non-`demo_repo` repositories, now that §2's blocking risk is understood,
   to see what else surfaces the way the `mypy` crash, the counterexample
   bug, and the upsert bug did — each was found only by actually running
   the system, not by code review.
3. Only after that: revisit §6 (baseline diffing) or §7 (a CI/CD step) —
   whichever the accumulated real-world debate results suggest matters
   more.

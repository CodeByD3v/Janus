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

**Concrete next steps, in order of how directly they'd resolve this**:
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
3. Once the event loop's actual blocking point is identified, the fix is
   almost certainly in how `MCPToolset`'s subprocess connection is
   constructed, awaited, or torn down after a failed call — not in the
   persistence functions themselves, which are now confirmed (via the
   isolated test) to be correct in isolation.

**Ready-to-run reproduction script**: `scripts/reproduce_s2.py` automates
the full reproduction sequence — starts the API, enqueues a debate against
`demo_repo`, starts a worker subprocess, streams all logs, watches for the
specific §2 log events, and prints the exact `py-spy dump` command with
the worker PID if the debate appears stuck. See `scripts/README_reproduce_s2.md`
for usage.

**Mitigation already in place, and genuinely valuable regardless of root
cause**: all three persistence calls now go through `_persist_with_timeout`
instead of being invoked as raw blocking calls. If the underlying issue
turns out to be specific to the persistence call after all (rather than
the event loop broadly), this now bounds and surfaces it loudly instead of
losing it silently. If the issue is the event loop itself, this wrapper
alone won't fix it — which is exactly the finding above, stated plainly
rather than claimed fixed.

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

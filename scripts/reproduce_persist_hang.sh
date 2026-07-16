#!/bin/bash
# scripts/reproduce_persist_hang.sh
#
# Reproduces the finding in ROADMAP.md §2: after a real, failing LLM call
# sequence, a debate's final-state persistence appeared not to complete
# within the full worker process — and even asyncio.wait_for's own timeout
# mechanism didn't fire, suggesting the worker's event loop itself may stop
# servicing callbacks in this scenario, not just that one call being slow.
#
# This could not be conclusively diagnosed in the sandboxed environment it
# was found in (backgrounded processes were torn down between observation
# windows, and the environment itself grew unstable under repeated runs).
# It needs a persistent terminal and, ideally, py-spy — this script sets up
# the reproduction; a human runs py-spy at the right moment.
#
# ── Requirements ─────────────────────────────────────────────────────────
#   - A real GOOGLE_API_KEY (this reproduction relies on a genuine LLM call
#     failing after real retries — an invalid/expired key works fine for
#     that, it does not need to be a WORKING key, just one the API will
#     genuinely attempt and reject, or leave GOOGLE_API_KEY unset/invalid
#     to force a fast local failure instead — see NOTES below)
#   - Python 3.12 with this repo's requirements.txt installed
#   - Two terminal sessions (or two panes/tabs) in the SAME persistent
#     shell environment — one runs the worker, the other runs py-spy
#     against it. This is the one thing the sandboxed environment this was
#     found in structurally could not provide.
#   - py-spy: `pip install py-spy`. On Linux, py-spy needs ptrace
#     permission — either run it with `sudo`, or once:
#       echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
#     On macOS, py-spy needs to be run with `sudo` (SIP restricts ptrace).
#
# ── What to do ───────────────────────────────────────────────────────────
#   1. In Terminal A: run this script. It enqueues a debate, starts the
#      worker, and prints the worker's PID.
#   2. Watch Terminal A's output for either of:
#        "debate_failed_initial_patch"      (structured log)
#        "initial_ask_raised_runtimeerror"  (diagnostic trace, if enabled)
#      Once you see that, the debate has failed and _persist_with_timeout
#      is about to be called (or has just been called).
#   3. IMMEDIATELY, in Terminal B, run:
#        py-spy dump --pid <WORKER_PID>
#      (substitute the PID printed by Terminal A). Do this a few times,
#      a couple seconds apart, while Terminal A is still showing "running"
#      for the debate status.
#   4. Look at the py-spy output for the MAIN thread specifically — what
#      is it doing? Is it inside asyncio's event loop select/poll call
#      (normal, idle), inside something related to the MCP subprocess
#      (subprocess.py, anyio, mcp/*), inside sqlite3/SQLAlchemy, or
#      somewhere unexpected? THAT stack trace is the actual answer this
#      whole investigation has been trying to get to indirectly through
#      log timing — py-spy gets it directly.
#   5. Also check: is the worker process's CPU usage (`top`/`ps`) near 0%
#      (genuinely blocked/waiting) or pegged at 100% (spinning)? These
#      point to very different root causes.
#
# ── Optional: enable the raw diagnostic trace ───────────────────────────
#   Set DIAGNOSTIC_PERSIST_TRACE=true (this script does, below) to also
#   get a raw, synchronous, fsync'd trace file at
#   $DIAGNOSTIC_PERSIST_TRACE_PATH (default /tmp/janus_persist_trace.log)
#   — see core/diagnostics.py for exactly why this bypasses logging and
#   asyncio entirely. Tail that file in a third terminal:
#     tail -f /tmp/janus_persist_trace.log
#   If tracing stops appearing entirely partway through (not just slows
#   down), that's strong independent evidence the event loop itself is
#   stuck, not just the persistence call.
#
# ── NOTES on making the failure happen faster ───────────────────────────
#   In the environment this was originally found in, the FIRST LLM call
#   attempt took ~90-100 seconds before failing, apparently dominated by a
#   slow first connection/DNS resolution to a blocked host — not the retry
#   logic itself (three retries completed in ~3 seconds once the first
#   attempt failed). Your network conditions may differ. If you want a
#   fast, reliable failure instead of waiting on a real network timeout,
#   temporarily set GOOGLE_API_KEY to something obviously malformed
#   (e.g. "invalid") — this fails at auth, typically much faster than a
#   network-level timeout, while still exercising the same code path
#   (a RuntimeError from _ask() after retries).

set -euo pipefail

# ── Configuration — edit these for your environment ─────────────────────
export DATABASE_URL="${DATABASE_URL:-sqlite:////tmp/janus_repro.db}"
export GOOGLE_API_KEY="${GOOGLE_API_KEY:?Set GOOGLE_API_KEY before running this script - see NOTES above for using a deliberately invalid key to fail fast}"
export API_KEYS="${API_KEYS:-repro-key:repro-tenant}"
export ALLOWED_REPO_ROOTS="${ALLOWED_REPO_ROOTS:?Set ALLOWED_REPO_ROOTS to the directory containing the repo to test against, e.g. this repos demo_repo directory}"
export USE_CONTAINERIZED_GATE="${USE_CONTAINERIZED_GATE:-false}"
export WORKER_POLL_INTERVAL="${WORKER_POLL_INTERVAL:-2}"
export WORKER_MAX_CONCURRENT="${WORKER_MAX_CONCURRENT:-1}"
export DIAGNOSTIC_PERSIST_TRACE="true"
export DIAGNOSTIC_PERSIST_TRACE_PATH="${DIAGNOSTIC_PERSIST_TRACE_PATH:-/tmp/janus_persist_trace.log}"

REPO_REF="${1:-$ALLOWED_REPO_ROOTS}"
TARGET_FILE="${2:-inventory.py}"
API_PORT="${API_PORT:-8123}"

# Best-effort cleanup of a prior run's SQLite file, handling both the
# 3-slash relative and 4-slash absolute sqlite:/// URL forms.
DB_FILE_PATH="${DATABASE_URL#sqlite:///}"
rm -f "$DB_FILE_PATH" 2>/dev/null || true
rm -f "$DIAGNOSTIC_PERSIST_TRACE_PATH"

echo "=== Starting API server on port $API_PORT ==="
uvicorn api.app:app --host 127.0.0.1 --port "$API_PORT" &
API_PID=$!
sleep 3

echo ""
echo "=== Enqueuing a debate ==="
RESPONSE=$(curl -s -X POST "http://127.0.0.1:$API_PORT/debates" \
  -H "X-API-Key: repro-key" \
  -H "Content-Type: application/json" \
  -d "{
    \"repo_ref\": \"$REPO_REF\",
    \"target_file\": \"$TARGET_FILE\",
    \"ticket\": \"reproduction run for ROADMAP.md section 2\"
  }")
echo "$RESPONSE"
DEBATE_ID=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['debate_id'])")
echo "Debate ID: $DEBATE_ID"

kill "$API_PID" 2>/dev/null || true
sleep 1

echo ""
echo "=== Starting the worker ==="
echo "Watch for 'debate_failed_initial_patch' below, then run py-spy against this PID in another terminal."
echo ""
python3 -m core.worker &
WORKER_PID=$!
echo ">>> WORKER PID: $WORKER_PID <<<"
echo ">>> In another terminal, once the debate fails: py-spy dump --pid $WORKER_PID <<<"
echo ""

# Poll status until it reaches a terminal state, or indefinitely if you
# Ctrl-C this script once you've captured what you need — the worker
# keeps running in the background (kill it manually with:
#   kill $WORKER_PID
# when you're done).
while true; do
  STATUS=$(python3 -c "
import sys; sys.path.insert(0, '.')
from storage.db import get_session
from storage.models import DebateSession
with get_session() as db:
    s = db.query(DebateSession).filter_by(id='$DEBATE_ID').first()
    print(s.status if s else 'NOT_FOUND')
" 2>/dev/null)
  echo "[$(date +%H:%M:%S)] status: $STATUS  (worker PID: $WORKER_PID)"
  if [ "$STATUS" != "running" ] && [ "$STATUS" != "queued" ]; then
    echo ""
    echo "Reached terminal state: $STATUS"
    echo "If this is 'running' and never changes, that's the bug — keep the"
    echo "worker alive and use py-spy now if you haven't already."
    break
  fi
  sleep 5
done

echo ""
echo "=== Diagnostic trace (if it captured anything) ==="
cat "$DIAGNOSTIC_PERSIST_TRACE_PATH" 2>/dev/null || echo "(no trace file — DIAGNOSTIC_PERSIST_TRACE may not have taken effect)"

echo ""
echo "Worker PID $WORKER_PID is still running — inspect it, then: kill $WORKER_PID"

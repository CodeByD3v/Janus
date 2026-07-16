"""
diagnostics.py — opt-in, primitive tracing for reproducing the
persist-hang finding documented in ROADMAP.md §2.

Deliberately the crudest possible instrumentation: a raw, synchronous
file write, flushed and fsync'd immediately, bypassing both the logging
framework and asyncio entirely. This is intentional — the leading
hypothesis for that finding is that the worker's event loop itself may
stop servicing callbacks in some scenarios, which would also prevent
normal logging (and even asyncio.wait_for's own timeout mechanism) from
producing a visible signal. A trace call here does not depend on the
event loop being responsive at all.

Off by default (settings.DIAGNOSTIC_PERSIST_TRACE). Turn on only while
actively reproducing that specific issue — see
scripts/reproduce_persist_hang.sh for the full procedure, including when
to attach py-spy for a definitive stack-level answer, which this module
is a lightweight first step toward, not a replacement for.
"""

from __future__ import annotations

import os
import time

from core.config import settings


def trace(label: str, **fields: object) -> None:
    """Write one trace line, if DIAGNOSTIC_PERSIST_TRACE is enabled.
    No-ops (does nothing, not even a settings check overhead worth
    mentioning) when disabled — safe to call unconditionally from
    production code paths."""
    if not settings.DIAGNOSTIC_PERSIST_TRACE:
        return

    parts = [f"{time.time():.6f}", label]
    parts.extend(f"{k}={v}" for k, v in fields.items())
    line = " ".join(str(p) for p in parts) + "\n"

    # Raw os-level write + fsync, not the logging framework — see this
    # module's docstring for why that distinction matters here.
    path = settings.DIAGNOSTIC_PERSIST_TRACE_PATH
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)

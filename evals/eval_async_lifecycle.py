"""Async lifecycle regressions for MCP/toolset teardown."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.orchestrator as orchestrator
from core.agents import close_agent_toolsets
from core.config import settings as real_settings
from core.orchestrator import _ask


class _Agent:
    def __init__(self, *tools):
        self.tools = list(tools)


class _SlowSyncTool:
    def __init__(self, delay: float):
        self.delay = delay
        self.called = False

    def close(self):
        self.called = True
        import time

        time.sleep(self.delay)


class _NeverRunner:
    def run_async(self, **kwargs):
        async def events():
            await asyncio.Event().wait()
            yield None

        return events()


class _CircuitBreaker:
    def allow_request(self):
        return True

    def record_failure(self):
        pass


class _SlowAsyncTool:
    def __init__(self, started: asyncio.Event):
        self.started = started

    async def close(self):
        self.started.set()
        await asyncio.Event().wait()



def test_sync_tool_close_is_offloaded_and_bounded():
    async def scenario():
        tool = _SlowSyncTool(0.15)
        heartbeat = False

        close_task = asyncio.create_task(
            close_agent_toolsets(_Agent(tool), timeout=0.02)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0.005)
        heartbeat = True
        await close_task
        return heartbeat, tool.called

    heartbeat, called = asyncio.run(scenario())
    assert heartbeat is True
    assert called is True


def test_llm_stream_is_bounded_by_call_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        orchestrator,
        "settings",
        replace(real_settings, LLM_CALL_TIMEOUT_SECONDS=0.02),
    )
    monkeypatch.setattr(orchestrator, "_circuit_breaker", _CircuitBreaker())

    async def scenario():
        with pytest.raises(RuntimeError, match="failed after 1 attempts"):
            await _ask(
                _NeverRunner(),
                "session",
                "user",
                "request",
                max_retries=1,
            )

    asyncio.run(scenario())


def test_async_tool_close_is_bounded():
    async def scenario():
        started = asyncio.Event()
        await close_agent_toolsets(_Agent(_SlowAsyncTool(started)), timeout=0.02)
        return started.is_set()

    assert asyncio.run(scenario()) is True

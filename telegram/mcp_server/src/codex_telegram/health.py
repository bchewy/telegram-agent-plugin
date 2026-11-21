"""Detect a wedged Telethon sender and cap how long a tool call may hang.

Telethon drops an incoming MTProto message whose id is more than 30s away
from ``time.time() + MTProtoState.time_offset`` and logs "Server sent a very
new message ... ignoring". Ten drops in a row raise SecurityError("Too many
messages had to be ignored consecutively"). The recv loop logs that and
continues: it does not reconnect, and it does not fail the RPCs waiting on
those replies. Auto-reconnect does not heal it either, because
``MTProtoState.reset()`` keeps ``time_offset``. The client stays
"connected" and every tool call waits forever. That is the wedge.

A single one of those SecurityErrors, or a burst of ignore warnings, schedules
a forced client reset (the offset is zeroed and the old client is disconnected
before a new one may connect). The watchdog does the same when a cheap probe
or an in-flight call stalls. Tool calls themselves are wrapped in a timeout
so one wedged RPC becomes an error the host can see.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import threading
import time

from telethon import functions

from . import limits
from .client import (
    _escalate,
    _note,
    current_client,
    force_reset_client,
    set_escalation_handler,
)

_SECURITY_SNIPPET = "too many messages had to be ignored"
_IGNORE_SNIPPETS = ("very new message", "very old message")

_installed_handler: logging.Handler | None = None


class StormMonitor:
    """Turn Telethon's ignore-storm warnings into one reset reason.

    Thread-safe: Telethon logs from the recv loop, which is the asyncio
    thread today but the logging API does not promise that.
    """

    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._ignores: list[float] = []
        self._suppressed_until = 0.0

    def observe(self, message: str) -> str | None:
        """Return a reset reason when ``message`` trips the storm, else None."""
        text = message.lower()
        if _SECURITY_SNIPPET in text:
            kind = "security"
        elif any(snippet in text for snippet in _IGNORE_SNIPPETS):
            kind = "ignore"
        else:
            return None

        with self._lock:
            now = self._now()
            if now < self._suppressed_until:
                return None
            if kind == "security":
                self._suppressed_until = now + limits.STORM_SUPPRESS_SECONDS
                self._ignores.clear()
                return (
                    "telethon dropped too many consecutive messages "
                    "(MTProto time_offset is skewed); resetting the client"
                )
            window = limits.STORM_WINDOW_SECONDS
            self._ignores = [stamp for stamp in self._ignores if now - stamp <= window]
            self._ignores.append(now)
            if len(self._ignores) < limits.STORM_IGNORE_THRESHOLD:
                return None
            self._ignores.clear()
            self._suppressed_until = now + limits.STORM_SUPPRESS_SECONDS
            return (
                f"{limits.STORM_IGNORE_THRESHOLD} ignored MTProto messages within "
                f"{window:.0f}s; resetting the client"
            )


class StormLogHandler(logging.Handler):
    def __init__(self, monitor: StormMonitor, schedule: Callable[[str], None]) -> None:
        super().__init__(level=logging.WARNING)
        self._monitor = monitor
        self._schedule = schedule

    def emit(self, record: logging.LogRecord) -> None:
        try:
            reason = self._monitor.observe(record.getMessage())
        except Exception:
            return
        if reason is None:
            return
        try:
            self._schedule(reason)
        except Exception:
            return


def install_storm_handler(schedule: Callable[[str], None]) -> StormMonitor:
    """Attach one handler to the ``telethon`` logger. Replacing is safe."""
    global _installed_handler
    monitor = StormMonitor()
    handler = StormLogHandler(monitor, schedule)
    logger = logging.getLogger("telethon")
    if _installed_handler is not None:
        logger.removeHandler(_installed_handler)
    logger.addHandler(handler)
    _installed_handler = handler
    return monitor


def uninstall_storm_handler() -> None:
    global _installed_handler
    if _installed_handler is None:
        return
    logging.getLogger("telethon").removeHandler(_installed_handler)
    _installed_handler = None


class Inflight:
    """Wall-clock age of tool calls currently inside the timeout wrapper."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[int, float] = {}
        self._next = 0

    def begin(self) -> int:
        with self._lock:
            self._next += 1
            token = self._next
            self._calls[token] = time.monotonic()
            return token

    def end(self, token: int) -> None:
        with self._lock:
            self._calls.pop(token, None)

    def oldest_age(self) -> float | None:
        with self._lock:
            if not self._calls:
                return None
            return time.monotonic() - min(self._calls.values())

    def clear(self) -> None:
        with self._lock:
            self._calls.clear()


INFLIGHT = Inflight()

_GUARD_ATTR = "_codex_telegram_call_guard"


def install_tool_call_guard(mcp) -> None:
    """Cap every MCP tool call so a wedged RPC cannot hang the request."""
    manager = mcp._tool_manager
    if getattr(manager, _GUARD_ATTR, False):
        return
    original = manager.call_tool

    async def guarded_call_tool(name, arguments, *args, **kwargs):
        token = INFLIGHT.begin()
        timeout = limits.TOOL_CALL_TIMEOUT_SECONDS
        try:
            return await asyncio.wait_for(
                original(name, arguments, *args, **kwargs),
                timeout=timeout,
            )
        except TimeoutError as exc:
            # Drop the wedged sender before telling the caller to retry, so
            # the retry lands on a fresh client instead of the same black hole.
            await force_reset_client(reason=f"tool {name!r} exceeded {timeout:.0f}s")
            raise RuntimeError(
                f"{name} timed out after {timeout:.0f}s because the Telegram "
                "connection stopped delivering replies. The connection was "
                "reset; retry."
            ) from exc
        finally:
            INFLIGHT.end(token)

    manager.call_tool = guarded_call_tool
    setattr(manager, _GUARD_ATTR, True)


def _spawn_reset(reason: str) -> None:
    task = asyncio.create_task(_run_reset(reason), name="telegram-storm-reset")
    task.add_done_callback(_log_reset_task)


def _log_reset_task(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _note(f"storm reset ended: {exc!r}")


async def _run_reset(reason: str) -> None:
    await force_reset_client(reason=reason)


def start_self_heal(loop: asyncio.AbstractEventLoop, on_fatal: Callable[[str], None]) -> None:
    """Install the storm handler and the fatal-exit hook on this event loop."""
    set_escalation_handler(on_fatal)

    def schedule(reason: str) -> None:
        loop.call_soon_threadsafe(_spawn_reset, reason)

    install_storm_handler(schedule)


async def probe_client(client) -> None:
    """One cheap RPC. Hanging here means the sender is dropping replies."""
    await client(functions.help.GetNearestDcRequest())


def _looks_connected(client) -> bool:
    is_connected = getattr(client, "is_connected", None)
    if not callable(is_connected):
        return False
    try:
        return bool(is_connected())
    except Exception:
        return False


async def watchdog_loop(stop: asyncio.Event) -> None:
    """Probe the live client and restart the process if a call is stuck.

    Idle backends (no client yet) are left alone: probing must not be what
    opens the session.
    """
    failures = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=limits.HEALTH_PROBE_INTERVAL_SECONDS)
            return
        except TimeoutError:
            pass

        age = INFLIGHT.oldest_age()
        stall_limit = limits.TOOL_CALL_TIMEOUT_SECONDS + limits.STALL_GRACE_SECONDS
        if age is not None and age > stall_limit:
            _escalate(
                f"a tool call has been running {age:.0f}s "
                f"(limit {stall_limit:.0f}s) without its timeout firing"
            )
            return

        client = current_client()
        if client is None or not _looks_connected(client):
            failures = 0
            continue
        try:
            await asyncio.wait_for(probe_client(client), limits.HEALTH_PROBE_TIMEOUT_SECONDS)
        except Exception as exc:
            failures += 1
            _note(f"health probe failed ({failures}/{limits.PROBE_FAILURES_BEFORE_RESET}): {exc!r}")
            if failures >= limits.PROBE_FAILURES_BEFORE_RESET:
                failures = 0
                try:
                    await force_reset_client(reason=f"health probe failed: {exc!r}")
                except Exception as reset_exc:
                    _note(f"health reset failed: {reset_exc!r}")
                    return
        else:
            failures = 0

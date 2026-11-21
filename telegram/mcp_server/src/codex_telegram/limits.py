"""Timeouts shared by the backend and the stdio proxy.

Module globals (not looked up on every call through os.environ) so tests can
monkeypatch them. A real process picks up the environment at import, which is
process start.
"""

from __future__ import annotations

import os


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# A wedged tool call has to come back as a real error before the MCP host's
# tool_timeout_sec (600 in .mcp.json) kills the stream with -32001.
TOOL_CALL_TIMEOUT_SECONDS = _float_env("CODEX_TELEGRAM_TOOL_TIMEOUT", 180.0)

# Extra time the proxy gives the backend to turn a tool timeout into an HTTP
# response. Past this the backend process itself is treated as frozen.
BACKEND_RESPONSE_GRACE_SECONDS = _float_env("CODEX_TELEGRAM_BACKEND_GRACE", 45.0)

# Watchdog cadence. Two timed-out probes in a row force a client reset.
HEALTH_PROBE_INTERVAL_SECONDS = _float_env("CODEX_TELEGRAM_HEALTH_INTERVAL", 30.0)
HEALTH_PROBE_TIMEOUT_SECONDS = _float_env("CODEX_TELEGRAM_HEALTH_PROBE_TIMEOUT", 8.0)
PROBE_FAILURES_BEFORE_RESET = 2

# If a tool call is still in flight this long after its own timeout, the
# timeout wrapper or the event loop is stuck and the process must restart.
# Larger than the client reset-lock bound so a reset that is actually making
# progress is not mistaken for a dead loop.
STALL_GRACE_SECONDS = _float_env("CODEX_TELEGRAM_STALL_GRACE", 60.0)

# "very new" / "very old" warnings. One SecurityError is already ten
# consecutive drops and trips on its own; this threshold is the backstop for
# a storm that has not reached the security line yet. A reconnect blip logs
# a handful of these and must not trip it.
STORM_WINDOW_SECONDS = _float_env("CODEX_TELEGRAM_STORM_WINDOW", 30.0)
STORM_IGNORE_THRESHOLD = 12
# Suppress repeat trips while a reset is in flight. Matches the client reset
# debounce so the log storm cannot schedule a task per line.
STORM_SUPPRESS_SECONDS = _float_env("CODEX_TELEGRAM_RESET_DEBOUNCE", 20.0)

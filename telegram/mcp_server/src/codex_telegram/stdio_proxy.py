"""stdio <-> shared-backend relay: the default ``serve`` entrypoint.

Holds NO Telegram connection. Relays newline-delimited JSON-RPC from the MCP
host on stdin to the shared backend's streamable-http endpoint on a unix
socket and writes replies to stdout. If the backend is not running (or died
mid-session), it is started detached; the backend's own flock guarantees a
single instance, so concurrent proxies racing to start it is harmless — the
losers exit and every proxy connects to the one winner.

Set ``CODEX_TELEGRAM_SHARED_BACKEND=0`` (or ``TELEGRAM_MCP_SHARED_BACKEND=0``)
to skip the proxy and run the old direct-stdio server with its own Telegram
client. Only do that when exactly one agent host uses the session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time

import httpx

from .legacy_serve import kill_legacy_serves, register_proxy
from .runtime_paths import backend_log_path, backend_socket_path, config_dir

SHARED_BACKEND_ENV_VARS = (
    "CODEX_TELEGRAM_SHARED_BACKEND",
    "TELEGRAM_MCP_SHARED_BACKEND",
)
START_TIMEOUT_ENV_VAR = "CODEX_TELEGRAM_BACKEND_START_TIMEOUT"
PROXY_TIMEOUT_ENV_VAR = "CODEX_TELEGRAM_PROXY_TIMEOUT"
LEGACY_CLEANUP_ENV_VAR = "CODEX_TELEGRAM_LEGACY_CLEANUP"

_DISABLED_VALUES = {"0", "false", "no", "off"}

DEFAULT_START_TIMEOUT_SECONDS = 30.0
DEFAULT_TOOL_TIMEOUT_SECONDS = 600.0
CONNECT_TIMEOUT_SECONDS = 5.0
PING_TIMEOUT_SECONDS = 2.0
START_POLL_INTERVAL_SECONDS = 0.3

# Host part is ignored over a unix socket; it only has to be a valid URL.
MCP_URL = "http://backend/mcp"
PING_URL = "http://backend/__ping"
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _env_flag_enabled(names: tuple[str, ...]) -> bool:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip().lower() not in _DISABLED_VALUES
    return True


def shared_backend_enabled() -> bool:
    """Whether ``serve`` should proxy to the shared backend (the default)."""
    if os.name != "posix":
        # The backend needs unix sockets and flock; fall back to direct stdio.
        return False
    return _env_flag_enabled(SHARED_BACKEND_ENV_VARS)


def legacy_cleanup_enabled() -> bool:
    return _env_flag_enabled((LEGACY_CLEANUP_ENV_VAR,))


def _err(message: str) -> None:
    print(f"telegram-proxy pid={os.getpid()}: {message}", file=sys.stderr, flush=True)


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _err(f"ignoring non-numeric {name}={raw!r}")
        return default


def start_backend_process() -> None:
    """Spawn the backend detached, with its output appended to the backend log.

    The backend inherits this proxy's environment (config dir, master key,
    API credentials). Its flock makes concurrent spawns collapse to one.
    """
    log_path = backend_log_path()
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        subprocess.Popen(
            [sys.executable, "-m", "codex_telegram", "backend"],
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
            cwd=str(config_dir()),
        )
    finally:
        os.close(log_fd)


class Relay:
    def __init__(self) -> None:
        self.sock_path = backend_socket_path()
        self.start_timeout = _float_env(START_TIMEOUT_ENV_VAR, DEFAULT_START_TIMEOUT_SECONDS)
        tool_timeout = _float_env(PROXY_TIMEOUT_ENV_VAR, DEFAULT_TOOL_TIMEOUT_SECONDS)
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(self.sock_path)),
            timeout=httpx.Timeout(tool_timeout, connect=CONNECT_TIMEOUT_SECONDS),
        )
        self.out_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()

    async def write(self, obj) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        async with self.out_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    async def _alive(self) -> bool:
        if not self.sock_path.exists():
            return False
        try:
            # Any HTTP answer (even a 404) proves the backend is serving.
            await self.client.get(PING_URL, timeout=PING_TIMEOUT_SECONDS)
            return True
        except httpx.TransportError:
            return False

    async def ensure_backend(self) -> None:
        async with self.start_lock:
            if await self._alive():
                return
            _err("backend not reachable; starting it")
            start_backend_process()
            deadline = time.monotonic() + self.start_timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(START_POLL_INTERVAL_SECONDS)
                if await self._alive():
                    return
            raise RuntimeError(
                f"telegram backend did not start within {self.start_timeout:.0f}s; "
                f"check {backend_log_path()}"
            )

    # Transport failures that mean the backend is gone (dead socket, stale
    # pooled connection to a killed process). Deliberately excludes timeouts:
    # retrying a slow tool call could execute it twice.
    _RETRYABLE_ERRORS = (
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        FileNotFoundError,
    )

    async def post(self, raw: str) -> httpx.Response:
        for attempt in range(2):
            try:
                return await self.client.post(MCP_URL, content=raw.encode("utf-8"), headers=HEADERS)
            except self._RETRYABLE_ERRORS:
                if attempt:
                    raise
                # Backend died mid-session (or never started): restart and retry.
                await self.ensure_backend()
        raise AssertionError("unreachable")

    async def handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        ids = [
            m.get("id")
            for m in (msg if isinstance(msg, list) else [msg])
            if isinstance(m, dict) and "id" in m and "method" in m
        ]
        try:
            resp = await self.post(raw)
            if resp.status_code == 202 or not resp.content:
                return  # notification; nothing to relay back
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                for line in resp.text.splitlines():
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload:
                            await self.write(json.loads(payload))
                return
            body = resp.json()
            if resp.status_code >= 400 and not (isinstance(body, dict) and "jsonrpc" in body):
                raise RuntimeError(f"backend HTTP {resp.status_code}: {resp.text[:300]}")
            await self.write(body)
        except Exception as exc:
            # Report as JSON-RPC errors so the MCP host never hangs on a lost id.
            _err(f"relay error: {exc!r}")
            for request_id in ids:
                await self.write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": f"telegram proxy: {exc}"},
                    }
                )


async def _run_relay() -> None:
    relay = Relay()
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue = asyncio.Queue()

    def reader() -> None:
        for line in sys.stdin.buffer:
            loop.call_soon_threadsafe(lines.put_nowait, line.decode("utf-8", "replace"))
        loop.call_soon_threadsafe(lines.put_nowait, None)

    threading.Thread(target=reader, daemon=True).start()
    try:
        await relay.ensure_backend()
    except Exception as exc:
        # Not fatal: per-request retries may still bring the backend up, and
        # request-level failures surface as JSON-RPC errors.
        _err(str(exc))
    tasks: set[asyncio.Task] = set()
    while True:
        line = await lines.get()
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        task = asyncio.create_task(relay.handle(line))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    if tasks:
        await asyncio.wait(tasks, timeout=5)


def run_proxy() -> None:
    # Importing the package configures rich logging at INFO; a log line per
    # relayed request is pure noise on the host's stderr.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    register_proxy()
    if legacy_cleanup_enabled():
        killed = kill_legacy_serves(log=_err)
        if killed:
            _err(f"removed {len(killed)} legacy serve process(es)")
    asyncio.run(_run_relay())

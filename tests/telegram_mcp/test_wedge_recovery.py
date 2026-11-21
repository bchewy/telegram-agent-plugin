"""Real backend + serve processes, fake Telethon.

Proves a wedged sender recovers with no manual kill, a call during the wedge
returns inside a bound, and the recovery never has two MTProto clients alive.
No network and no Telegram session: sitecustomize replaces the client factory.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from test_shared_backend import _backend_pids, _kill_backends, _lock_path

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc"), reason="Linux-only")

# Short on purpose. The host-side ceiling in .mcp.json is 600s; a wedge has to
# come back well inside that, and these bounds make the test fail if it doesn't.
TOOL_TIMEOUT = "2"
CALL_BOUND_SECONDS = 12.0

SITECUSTOMIZE = textwrap.dedent(
    '''
    import asyncio
    import fcntl
    import logging
    import os

    import codex_telegram.client as client_mod
    from telethon import types

    _MARKER = os.environ["CODEX_TELEGRAM_TEST_CLIENT_MARKER_DIR"]


    def _mode():
        try:
            return open(os.path.join(_MARKER, "mode"), encoding="ascii").read().strip() or "ok"
        except OSError:
            return "ok"


    def _census(delta):
        path = os.path.join(_MARKER, "census")
        with open(path, "a+", encoding="ascii") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            raw = fh.read().strip() or "0 0"
            live, peak = (int(part) for part in raw.split())
            live = max(0, live + delta)
            peak = max(peak, live)
            fh.seek(0)
            fh.truncate()
            fh.write(f"{live} {peak}\\n")
            fh.flush()


    def _append(name, text):
        with open(os.path.join(_MARKER, name), "a", encoding="ascii") as fh:
            fh.write(text + "\\n")


    class _FakeTelegramClient:
        """Stands in for Telethon. `mode` is a file the test flips:

        ok    - RPCs return
        hang  - RPCs wait until disconnect(), i.e. a wedged sender
        storm - RPCs return, and log the production SecurityError text
        """

        def __init__(self):
            self.connected = False
            self._counted = False
            self._released = asyncio.Event()
            self._sender = type("_S", (), {})()
            self._sender._state = type("_St", (), {"time_offset": -12345, "_ignore_count": 9})()

        async def connect(self):
            self.connected = True
            if not self._counted:
                self._counted = True
                _census(+1)
                _append("builds", str(id(self)))

        def is_connected(self):
            return self.connected

        async def is_user_authorized(self):
            return True

        async def disconnect(self):
            self._released.set()
            self.connected = False
            # Production zeroes time_offset before it calls disconnect.
            _append("offsets", str(self._sender._state.time_offset))
            if self._counted:
                self._counted = False
                _census(-1)

        async def _maybe_wedge(self):
            if _mode() == "hang":
                await self._released.wait()
                raise ConnectionError("wedged sender dropped")

        async def __call__(self, request):
            await self._maybe_wedge()
            return types.NearestDc(nearest_dc=1, country="US", this_dc=1)

        async def get_me(self):
            if _mode() == "storm":
                log = logging.getLogger("telethon.network.mtprotosender")
                for _ in range(3):
                    log.warning(
                        "Security error while unpacking a received message: "
                        "Too many messages had to be ignored consecutively"
                    )
            await self._maybe_wedge()
            return types.User(id=424242, first_name="Stub", username="stub_user")


    class _Record:
        session_string = "1"
        api_id = 1
        api_hash = "h"


    def _build(_session, _api_id, _api_hash):
        return _FakeTelegramClient()


    client_mod._build_client = _build
    client_mod.load_session = lambda: _Record()
    # The periodic verify RPC would race the wedge. The watchdog probe is the
    # path under test; keep the 30s verify from issuing its own.
    client_mod.CONNECTION_VERIFY_INTERVAL_SECONDS = 10**9
    '''
)


class _Proxy:
    def __init__(self, env, stderr_path: Path):
        self._stderr = open(stderr_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "codex_telegram", "serve"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
        )
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line)

    def send(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def recv(self, request_id, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no response for id={request_id}")
            line = self._lines.get(timeout=remaining)
            msg = json.loads(line)
            if msg.get("id") == request_id:
                return msg

    def rpc(self, request_id, method, params=None, timeout=20):
        self.send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        return self.recv(request_id, timeout=timeout)

    def close(self):
        if self.proc.poll() is None and self.proc.stdin:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._stderr.close()


def _is_error(reply: dict) -> bool:
    if "error" in reply:
        return True
    result = reply.get("result")
    return isinstance(result, dict) and bool(result.get("isError"))


def _brief(reply: dict) -> str:
    if "error" in reply:
        return str(reply["error"].get("message", reply["error"]))[:300]
    result = reply.get("result", {})
    content = result.get("content") if isinstance(result, dict) else None
    if content:
        return str(content[0].get("text", ""))[:300]
    return json.dumps(reply)[:300]


@pytest.fixture
def wedge_env(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    marker = tmp_path / "marker"
    marker.mkdir()
    (marker / "mode").write_text("ok\n", encoding="ascii")
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    stderr_path = tmp_path / "serve.stderr"
    env = dict(os.environ)
    env.update(
        {
            "CODEX_TELEGRAM_CONFIG_DIR": str(config),
            "CODEX_TELEGRAM_TEST_CLIENT_MARKER_DIR": str(marker),
            "CODEX_TELEGRAM_LEGACY_CLEANUP": "0",
            "CODEX_TELEGRAM_TOOL_TIMEOUT": TOOL_TIMEOUT,
            "CODEX_TELEGRAM_CONNECT_TIMEOUT": "5",
            "CODEX_TELEGRAM_DISCONNECT_TIMEOUT": "2",
            "CODEX_TELEGRAM_RESET_LOCK_TIMEOUT": "5",
            "CODEX_TELEGRAM_RESET_DEBOUNCE": "0.4",
            "CODEX_TELEGRAM_HEALTH_INTERVAL": "0.3",
            "CODEX_TELEGRAM_HEALTH_PROBE_TIMEOUT": "0.4",
            "CODEX_TELEGRAM_BACKEND_GRACE": "8",
            "CODEX_TELEGRAM_STALL_GRACE": "30",
            "PYTHONPATH": os.pathsep.join(
                [str(stub)] + ([pp] if (pp := os.environ.get("PYTHONPATH")) else [])
            ),
            "PYTHONUNBUFFERED": "1",
        }
    )
    yield env, config, marker, stderr_path
    _kill_backends(config)


def _set_mode(marker: Path, mode: str) -> None:
    (marker / "mode").write_text(mode + "\n", encoding="ascii")


def _census(marker: Path) -> tuple[int, int]:
    raw = (marker / "census").read_text(encoding="ascii").strip()
    live, peak = (int(part) for part in raw.split())
    return live, peak


def _offsets(marker: Path) -> list[str]:
    path = marker / "offsets"
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="ascii").splitlines() if line.strip()]


def _fail(config: Path, stderr_path: Path, message: str) -> None:
    log = config / "run" / "backend.log"
    backend = log.read_text(encoding="utf-8", errors="replace")[-4000:] if log.exists() else "<no backend log>"
    serve = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:] if stderr_path.exists() else ""
    raise AssertionError(f"{message}\n--- backend.log ---\n{backend}\n--- serve stderr ---\n{serve}")


def _initialize(proxy: _Proxy) -> None:
    reply = proxy.rpc(
        1,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "wedge-smoke", "version": "0"},
        },
    )
    assert reply["result"]["serverInfo"]["name"] == "telegram"


def test_wedged_call_returns_and_the_next_call_works(wedge_env):
    env, config, marker, stderr_path = wedge_env
    proxy = _Proxy(env, stderr_path)
    try:
        _initialize(proxy)
        (backend_pid,) = _backend_pids(config)

        healthy = proxy.rpc(2, "tools/call", {"name": "get_me", "arguments": {}})
        if _is_error(healthy) or "stub_user" not in json.dumps(healthy):
            _fail(config, stderr_path, f"baseline get_me failed: {_brief(healthy)}")

        _set_mode(marker, "hang")
        started = time.monotonic()
        wedged = proxy.rpc(3, "tools/call", {"name": "get_me", "arguments": {}}, timeout=CALL_BOUND_SECONDS)
        elapsed = time.monotonic() - started
        if not _is_error(wedged) or elapsed >= CALL_BOUND_SECONDS:
            _fail(
                config,
                stderr_path,
                f"wedged get_me did not error inside {CALL_BOUND_SECONDS:.0f}s "
                f"(elapsed={elapsed:.2f}s): {_brief(wedged)}",
            )
        print(f"SMOKE wedged get_me returned in {elapsed:.2f}s: {_brief(wedged)}")

        _set_mode(marker, "ok")
        recovered = proxy.rpc(4, "tools/call", {"name": "get_me", "arguments": {}})
        if _is_error(recovered) or "stub_user" not in json.dumps(recovered):
            _fail(config, stderr_path, f"get_me after recovery failed: {_brief(recovered)}")
        print("SMOKE get_me after recovery: stub_user")

        pids = _backend_pids(config)
        if pids != [backend_pid]:
            _fail(config, stderr_path, f"backend pid changed ({backend_pid} -> {pids}); recovery was not in-process")
        live, peak = _census(marker)
        offsets = _offsets(marker)
        print(f"SMOKE max concurrent MTProto clients: {peak} (live now {live})")
        print(f"SMOKE time_offset seen at disconnect: {offsets}")
        if peak != 1 or live != 1:
            _fail(config, stderr_path, f"client overlap: live={live} peak={peak}")
        if not offsets or any(value != "0" for value in offsets):
            _fail(config, stderr_path, f"time_offset was not cleared: {offsets}")
    finally:
        proxy.close()


def test_security_error_storm_resets_the_client(wedge_env):
    env, config, marker, stderr_path = wedge_env
    proxy = _Proxy(env, stderr_path)
    try:
        _initialize(proxy)
        (backend_pid,) = _backend_pids(config)
        _set_mode(marker, "storm")
        first = proxy.rpc(2, "tools/call", {"name": "get_me", "arguments": {}})
        # The call itself may win the race and return, or lose it and error
        # because the reset disconnected under it. Either way the storm has to
        # have torn the client down with the offset cleared.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "0" not in _offsets(marker):
            time.sleep(0.05)
        offsets = _offsets(marker)
        if "0" not in offsets:
            _fail(config, stderr_path, f"storm did not reset the client (first call: {_brief(first)})")

        _set_mode(marker, "ok")
        recovered = proxy.rpc(3, "tools/call", {"name": "get_me", "arguments": {}})
        if _is_error(recovered) or "stub_user" not in json.dumps(recovered):
            _fail(config, stderr_path, f"get_me after the storm failed: {_brief(recovered)}")
        print(f"SMOKE storm first call: {_brief(first)}")
        print("SMOKE get_me after storm reset: stub_user")

        if _backend_pids(config) != [backend_pid]:
            _fail(config, stderr_path, "storm recovery replaced the backend process")
        live, peak = _census(marker)
        print(f"SMOKE max concurrent MTProto clients during storm: {peak}")
        if peak != 1 or live != 1:
            _fail(config, stderr_path, f"client overlap during storm: live={live} peak={peak}")
        if any(value != "0" for value in _offsets(marker)):
            _fail(config, stderr_path, f"time_offset was not cleared: {_offsets(marker)}")
        builds = (marker / "builds").read_text(encoding="ascii").split()
        if len(builds) < 2:
            _fail(config, stderr_path, f"storm did not build a replacement client: {builds}")
    finally:
        proxy.close()


def test_terminate_locked_backend_frees_the_lock_for_exactly_one_successor(wedge_env):
    """The proxy's last resort: the old backend is dead before a new one runs."""
    from codex_telegram.backend import acquire_backend_lock
    from codex_telegram.supervision import terminate_locked_backend

    env, config, _marker, stderr_path = wedge_env
    proxy = _Proxy(env, stderr_path)
    try:
        _initialize(proxy)
        (old_pid,) = _backend_pids(config)
        assert terminate_locked_backend(_lock_path(config), grace=8) is True
        proxy.proc.send_signal(signal.SIGKILL)
        proxy.proc.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and old_pid in _backend_pids(config):
            time.sleep(0.05)
        assert old_pid not in _backend_pids(config)

        holder = acquire_backend_lock(_lock_path(config))
        assert holder is not None
        assert acquire_backend_lock(_lock_path(config)) is None
        os.close(holder)
    finally:
        if proxy.proc.poll() is None:
            proxy.close()

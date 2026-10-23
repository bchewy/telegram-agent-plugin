"""Shared-backend integration: one flock-guarded backend, many stdio proxies.

Spawns the real `codex-telegram backend` / `codex-telegram serve` subprocesses
against a throwaway config dir. No Telegram network access is involved:
listing tools never touches Telethon, and tool calls are exercised through a
stubbed client factory injected with a sitecustomize.py on PYTHONPATH.
"""

from __future__ import annotations

import fcntl
import json
import os
import queue
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from codex_telegram.backend import acquire_backend_lock

pytestmark = pytest.mark.skipif(
    not os.path.isdir("/proc"), reason="shared backend tests are Linux-only"
)

START_DEADLINE = 45.0

SITECUSTOMIZE = """\
# Test stub injected via PYTHONPATH: replaces the real Telethon client factory
# so backend tool calls never touch the network.
import os

import codex_telegram.client as _client_mod
from telethon import types

_MARKER_DIR = os.environ.get("CODEX_TELEGRAM_TEST_CLIENT_MARKER_DIR")


class _FakeTelegramClient:
    def __init__(self):
        if _MARKER_DIR:
            path = os.path.join(_MARKER_DIR, f"client-{os.getpid()}-{id(self):x}")
            with open(path, "w") as fh:
                fh.write(str(os.getpid()))

    async def get_me(self):
        return types.User(id=424242, first_name="Stub", username="stub_user")


_instance = None


async def _fake_get_client():
    global _instance
    if _instance is None:
        _instance = _FakeTelegramClient()
    return _instance


_client_mod.get_client = _fake_get_client
"""


@pytest.fixture
def shared_env(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()

    env = dict(os.environ)
    env.pop("CODEX_TELEGRAM_SHARED_BACKEND", None)
    env.pop("TELEGRAM_MCP_SHARED_BACKEND", None)
    env.update(
        {
            "CODEX_TELEGRAM_CONFIG_DIR": str(config_dir),
            "CODEX_TELEGRAM_TEST_CLIENT_MARKER_DIR": str(marker_dir),
            # Sweeping is covered by test_legacy_serve; keep it out of the way here.
            "CODEX_TELEGRAM_LEGACY_CLEANUP": "0",
            "PYTHONPATH": os.pathsep.join(
                [str(stub_dir)] + ([env_pp] if (env_pp := os.environ.get("PYTHONPATH")) else [])
            ),
            "PYTHONUNBUFFERED": "1",
        }
    )
    yield env, config_dir, marker_dir
    _kill_backends(config_dir)


def _lock_path(config_dir: Path) -> Path:
    return config_dir / "run" / "backend.lock"


def _sock_path(config_dir: Path) -> Path:
    return config_dir / "run" / "backend.sock"


def _backend_pids(config_dir: Path) -> list[int]:
    """Pids of `python -m codex_telegram backend` processes bound to this config dir."""
    needle = f"CODEX_TELEGRAM_CONFIG_DIR={config_dir}".encode()
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
            if cmdline[-2:-4:-1] != [b"backend", b"codex_telegram"]:
                continue
            if needle in Path(f"/proc/{pid}/environ").read_bytes().split(b"\x00"):
                pids.append(pid)
        except OSError:
            continue
    return pids


def _kill_backends(config_dir: Path) -> None:
    for pid in _backend_pids(config_dir):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _wait_for(predicate, timeout=START_DEADLINE, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {message}")


def _spawn_backend(env) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "codex_telegram", "backend"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class ProxyClient:
    """A `codex-telegram serve` subprocess with line-based JSON-RPC access."""

    def __init__(self, env):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "codex_telegram", "serve"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
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

    def recv(self, request_id, timeout=START_DEADLINE) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no response for id={request_id}")
            line = self._lines.get(timeout=remaining)
            msg = json.loads(line)
            if msg.get("id") == request_id:
                return msg

    def rpc(self, request_id, method, params=None, timeout=START_DEADLINE) -> dict:
        self.send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        return self.recv(request_id, timeout=timeout)

    def initialize(self, request_id=1) -> dict:
        return self.rpc(
            request_id,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest-proxy", "version": "0"},
            },
        )

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


def test_acquire_backend_lock_refuses_second_holder(tmp_path):
    lock_path = tmp_path / "backend.lock"
    first = acquire_backend_lock(lock_path)
    assert first is not None
    assert acquire_backend_lock(lock_path) is None
    fcntl.flock(first, fcntl.LOCK_UN)
    os.close(first)
    second = acquire_backend_lock(lock_path)
    assert second is not None
    os.close(second)


def test_second_backend_process_exits(shared_env):
    env, config_dir, _ = shared_env
    first = _spawn_backend(env)
    try:
        _wait_for(lambda: _sock_path(config_dir).exists(), message="backend socket")
        second = _spawn_backend(env)
        stdout, stderr = second.communicate(timeout=START_DEADLINE)
        assert second.returncode == 0
        assert b"another backend already holds" in stderr
        assert first.poll() is None, "first backend must keep running"
        assert int(_lock_path(config_dir).read_text().strip()) == first.pid
    finally:
        first.terminate()
        first.wait(timeout=15)


def test_socket_and_run_dir_permissions(shared_env):
    env, config_dir, _ = shared_env
    backend = _spawn_backend(env)
    try:
        sock = _sock_path(config_dir)
        _wait_for(sock.exists, message="backend socket")
        assert stat.S_IMODE(sock.stat().st_mode) == 0o600
        assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(_lock_path(config_dir).stat().st_mode) == 0o600
    finally:
        backend.terminate()
        backend.wait(timeout=15)


def test_proxy_autostarts_backend(shared_env):
    env, config_dir, _ = shared_env
    assert not _backend_pids(config_dir)
    proxy = ProxyClient(env)
    try:
        result = proxy.initialize()
        assert result["result"]["serverInfo"]["name"] == "telegram"
        pids = _backend_pids(config_dir)
        assert len(pids) == 1
        assert int(_lock_path(config_dir).read_text().strip()) == pids[0]
    finally:
        proxy.close()


def test_two_concurrent_proxies_share_one_backend(shared_env):
    env, config_dir, marker_dir = shared_env
    proxy_a = ProxyClient(env)
    proxy_b = ProxyClient(env)
    try:
        assert proxy_a.initialize()["result"]["serverInfo"]["name"] == "telegram"
        assert proxy_b.initialize()["result"]["serverInfo"]["name"] == "telegram"

        # Interleave: both requests in flight before either response is read.
        proxy_a.send({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                      "params": {"name": "get_me", "arguments": {}}})
        proxy_b.send({"jsonrpc": "2.0", "id": 20, "method": "tools/call",
                      "params": {"name": "get_me", "arguments": {}}})
        reply_a = proxy_a.recv(10)
        reply_b = proxy_b.recv(20)
        for reply in (reply_a, reply_b):
            assert "error" not in reply, reply
            assert "stub_user" in json.dumps(reply["result"])

        # Both proxies may race to spawn a backend; the flock loser exits as
        # soon as it reaches the lock. Exactly one must remain.
        _wait_for(
            lambda: len(_backend_pids(config_dir)) == 1,
            message="flock loser to exit, leaving one backend",
        )
        (backend_pid,) = _backend_pids(config_dir)
        assert int(_lock_path(config_dir).read_text().strip()) == backend_pid
        markers = list(marker_dir.iterdir())
        assert len(markers) == 1, (
            f"expected exactly one Telegram client across both proxies, got {markers}"
        )
    finally:
        proxy_a.close()
        proxy_b.close()


def test_backend_restart_after_kill(shared_env):
    env, config_dir, _ = shared_env
    proxy = ProxyClient(env)
    try:
        proxy.initialize()
        (old_pid,) = _backend_pids(config_dir)

        os.kill(old_pid, signal.SIGKILL)
        # The dead backend lingers as a zombie of the proxy (which never reaps
        # it), so check the live-backend scan rather than /proc existence.
        _wait_for(lambda: old_pid not in _backend_pids(config_dir), message="backend death")

        reply = proxy.rpc(2, "tools/list")
        assert "error" not in reply, reply
        assert any(t["name"] == "get_me" for t in reply["result"]["tools"])

        (new_pid,) = _backend_pids(config_dir)
        assert new_pid != old_pid
        assert int(_lock_path(config_dir).read_text().strip()) == new_pid
    finally:
        proxy.close()

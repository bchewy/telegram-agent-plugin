"""The wedge: Telethon keeps a skewed time_offset across reconnects and then
drops every server reply while still looking connected. These tests are the
in-process proof that a storm, a hung RPC, and a stuck disconnect are all
handled without ever having two clients alive.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from types import SimpleNamespace

import httpx
import pytest

from codex_telegram import client as client_mod
from codex_telegram import legacy_serve, limits
from codex_telegram import supervision
from codex_telegram.health import (
    INFLIGHT,
    StormMonitor,
    install_tool_call_guard,
    probe_client,
    start_self_heal,
    uninstall_storm_handler,
    watchdog_loop,
)
from codex_telegram.stdio_proxy import Relay
from telethon.crypto import AuthKey
from telethon.network.mtprotostate import MTProtoState


def _session():
    return SimpleNamespace(session_string="s", api_id=1, api_hash="h")


def _state(offset=-4000):
    return SimpleNamespace(time_offset=offset, _ignore_count=7)


class _Client:
    def __init__(self, *, offset=-4000, disconnect_hangs=False, connect_hangs=False):
        self._sender = SimpleNamespace(_state=_state(offset))
        self.connected = False
        self.disconnects = 0
        self._disconnect_hangs = disconnect_hangs
        self._connect_hangs = connect_hangs
        self.requests = 0

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        if self._connect_hangs:
            await asyncio.Future()
        self.connected = True

    async def disconnect(self):
        if self._disconnect_hangs:
            await asyncio.Future()
        self.disconnects += 1
        self.connected = False

    async def is_user_authorized(self) -> bool:
        return True

    async def __call__(self, request):
        self.requests += 1
        return SimpleNamespace(ok=True)


def _arm(monkeypatch, *, debounce=0.0, disconnect_timeout=1.0, connect_timeout=1.0):
    """A non-exiting escalation handler plus short bounds."""
    reasons = []
    monkeypatch.setattr(client_mod, "_escalation_handler", lambda reason: reasons.append(reason))
    monkeypatch.setattr(client_mod, "RESET_DEBOUNCE_SECONDS", debounce)
    monkeypatch.setattr(client_mod, "DISCONNECT_TIMEOUT_SECONDS", disconnect_timeout)
    monkeypatch.setattr(client_mod, "CONNECT_TIMEOUT_SECONDS", connect_timeout)
    monkeypatch.setattr(client_mod, "RESET_LOCK_TIMEOUT_SECONDS", 2.0)
    client_mod._client = None
    client_mod._client_unrecoverable = False
    client_mod._last_reset_monotonic = 0.0
    client_mod._reset_history.clear()
    client_mod._last_client_verify_monotonic = 0.0
    return reasons


def test_telethon_auto_reconnect_keeps_a_skewed_time_offset():
    """Root cause, pinned against the installed Telethon.

    ``MTProtoState.reset()`` is what auto-reconnect runs. It clears the
    session id and the ignore counter and leaves ``time_offset`` alone, so a
    skewed offset survives every "begin reconnect" in the backend log and
    every later reply keeps being discarded as "too new".
    """
    state = MTProtoState(
        AuthKey(os.urandom(256)),
        {"telethon.network.mtprotostate": logging.getLogger("telethon.network.mtprotostate")},
    )
    state.time_offset = -420
    state._ignore_count = 10
    state.reset()
    assert state.time_offset == -420
    assert state._ignore_count == 0


def test_security_error_trips_immediately_and_then_suppresses():
    clock = [0.0]
    monitor = StormMonitor(now=lambda: clock[0])
    message = (
        "Security error while unpacking a received message: "
        "Too many messages had to be ignored consecutively"
    )
    assert monitor.observe(message) is not None
    assert monitor.observe(message) is None  # same storm, one reset
    clock[0] = limits.STORM_SUPPRESS_SECONDS + 1
    assert monitor.observe(message) is not None


def test_ignore_burst_needs_the_threshold_a_reconnect_blip_does_not():
    clock = [0.0]
    monitor = StormMonitor(now=lambda: clock[0])
    warning = "Server sent a very new message with ID 1, ignoring (see FAQ for details)"
    for _ in range(limits.STORM_IGNORE_THRESHOLD - 1):
        assert monitor.observe(warning) is None
    assert "resetting" in monitor.observe(warning)


def test_old_ignore_warnings_outside_the_window_do_not_accumulate():
    clock = [0.0]
    monitor = StormMonitor(now=lambda: clock[0])
    warning = "Server sent a very old message with ID 1, ignoring (see FAQ for details)"
    assert monitor.observe(warning) is None
    clock[0] = limits.STORM_WINDOW_SECONDS + 5
    for _ in range(limits.STORM_IGNORE_THRESHOLD - 1):
        assert monitor.observe(warning) is None


async def test_security_log_line_resets_the_client_and_zeroes_the_offset(monkeypatch):
    reasons = _arm(monkeypatch)
    wedged = _Client(offset=-12345)
    wedged.connected = True
    client_mod._client = wedged

    loop = asyncio.get_running_loop()
    start_self_heal(loop, lambda reason: reasons.append(reason))
    logging.getLogger("telethon.network.mtprotosender").warning(
        "Security error while unpacking a received message: "
        "Too many messages had to be ignored consecutively"
    )
    for _ in range(20):
        if wedged.disconnects:
            break
        await asyncio.sleep(0.02)

    assert wedged.disconnects == 1
    assert wedged._sender._state.time_offset == 0
    assert wedged._sender._state._ignore_count == 0
    assert client_mod._client is None
    assert reasons == []  # in-process reset; the process stays up
    uninstall_storm_handler()


async def test_reset_then_get_client_never_overlaps_two_connections(monkeypatch):
    _arm(monkeypatch)
    live = 0
    peak = 0
    built = []

    class _Tracked(_Client):
        async def connect(self) -> None:
            nonlocal live, peak
            await super().connect()
            live += 1
            peak = max(peak, live)

        async def disconnect(self):
            nonlocal live
            # Still counted as live until the disconnect actually finishes,
            # which is the window a buggy reset would open a second client in.
            await asyncio.sleep(0.05)
            live -= 1
            await super().disconnect()

    def build(*_args):
        client = _Tracked(offset=-50)
        built.append(client)
        return client

    monkeypatch.setattr(client_mod, "_build_client", build)
    monkeypatch.setattr(client_mod, "load_session", _session)

    first = await client_mod.get_client()
    assert await client_mod.force_reset_client(reason="storm") is True
    second = await client_mod.get_client()

    assert first is not second
    assert len(built) == 2
    assert peak == 1
    assert live == 1
    assert first._sender._state.time_offset == 0
    assert not first.connected
    assert second.connected


async def test_reset_blocks_behind_an_inflight_connect(monkeypatch):
    """A connect already holding the client lock finishes, and is torn down,
    before a reset is allowed to run. Nothing connects in between."""
    _arm(monkeypatch)
    release = asyncio.Event()
    built = []

    class _Gated(_Client):
        async def connect(self) -> None:
            await release.wait()
            await super().connect()

    def build(*_args):
        client = _Gated()
        built.append(client)
        return client

    monkeypatch.setattr(client_mod, "_build_client", build)
    monkeypatch.setattr(client_mod, "load_session", _session)

    connecting = asyncio.create_task(client_mod.get_client())
    # Let get_client take the lock and park inside connect().
    for _ in range(50):
        if built:
            break
        await asyncio.sleep(0.01)
    assert built, "connect never started"

    resetting = asyncio.create_task(client_mod.force_reset_client(reason="storm"))
    await asyncio.sleep(0.05)
    assert not resetting.done()
    assert len(built) == 1

    release.set()
    first = await connecting
    assert await resetting is True
    assert len(built) == 1
    assert client_mod.current_client() is None
    assert first.disconnects == 1
    assert not first.connected


async def test_stuck_disconnect_never_opens_a_second_client(monkeypatch):
    reasons = _arm(monkeypatch, disconnect_timeout=0.05)
    stuck = _Client(disconnect_hangs=True)
    stuck.connected = True
    client_mod._client = stuck
    built = []
    monkeypatch.setattr(client_mod, "_build_client", lambda *_a: built.append(1))
    monkeypatch.setattr(client_mod, "load_session", _session)

    with pytest.raises(ConnectionError, match="shutdown timed out"):
        await client_mod.force_reset_client(reason="disconnect hung")

    # The old client could not be proven dead, so get_client must refuse to
    # dial Telegram at all. Escalation (process exit in production) is the
    # only way out, and it has to happen before any new client exists.
    with pytest.raises(ConnectionError, match="could not be shut down"):
        await client_mod.get_client()

    assert built == []
    assert reasons  # asked the process to exit
    assert client_mod._client_unrecoverable is True


async def test_connect_timeout_tears_the_half_open_client_down(monkeypatch):
    _arm(monkeypatch, connect_timeout=0.05)
    built = []

    def build(*_args):
        client = _Client(connect_hangs=True)
        built.append(client)
        return client

    monkeypatch.setattr(client_mod, "_build_client", build)
    monkeypatch.setattr(client_mod, "load_session", _session)

    with pytest.raises(ConnectionError, match="connect timed out"):
        await client_mod.get_client()

    assert len(built) == 1
    assert built[0].disconnects == 1
    assert client_mod._client is None
    assert client_mod._client_unrecoverable is False


async def test_wedged_tool_call_errors_inside_the_timeout_and_resets(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(limits, "TOOL_CALL_TIMEOUT_SECONDS", 0.2)
    wedged = _Client()
    wedged.connected = True
    client_mod._client = wedged

    class _Manager:
        async def call_tool(self, name, arguments, *args, **kwargs):
            await asyncio.Future()

    class _MCP:
        _tool_manager = _Manager()

    mcp = _MCP()
    install_tool_call_guard(mcp)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="get_me timed out after"):
        await mcp._tool_manager.call_tool("get_me", {})
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert wedged.disconnects == 1
    assert wedged._sender._state.time_offset == 0
    assert client_mod._client is None
    # The wrapper released its in-flight entry, so the watchdog does not see
    # a call that already returned.
    assert INFLIGHT.oldest_age() is None


async def test_cancelled_rpc_becomes_a_tool_error_and_a_real_cancel_does_not(monkeypatch):
    """Telethon disconnect() cancels the pending RPC future.

    That CancelledError is a BaseException, so FastMCP will not turn it into
    a tool result. The guard has to. Cancelling the tool task itself — the
    host dropped the HTTP request — must still propagate.
    """
    _arm(monkeypatch)

    class _RPC:
        def __init__(self):
            self.fut: asyncio.Future | None = None

        async def call_tool(self, name, arguments, *args, **kwargs):
            self.fut = asyncio.get_running_loop().create_future()
            await self.fut

    rpc = _RPC()

    class _MCP:
        _tool_manager = rpc

    mcp = _MCP()
    install_tool_call_guard(mcp)

    stalled = asyncio.create_task(mcp._tool_manager.call_tool("get_me", {}))
    for _ in range(50):
        if rpc.fut is not None:
            break
        await asyncio.sleep(0.01)
    assert rpc.fut is not None
    rpc.fut.cancel()
    with pytest.raises(ConnectionError, match="connection was reset"):
        await stalled

    stalled = asyncio.create_task(mcp._tool_manager.call_tool("get_me", {}))
    for _ in range(50):
        if rpc.fut is not None and not rpc.fut.done():
            break
        await asyncio.sleep(0.01)
    stalled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stalled


async def test_watchdog_survives_a_probe_cancelled_by_disconnect(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(limits, "HEALTH_PROBE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(limits, "HEALTH_PROBE_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(limits, "PROBE_FAILURES_BEFORE_RESET", 2)

    class _CancelledProbe(_Client):
        async def __call__(self, request):
            raise asyncio.CancelledError()

    client = _CancelledProbe()
    client.connected = True
    client_mod._client = client
    stop = asyncio.Event()
    task = asyncio.create_task(watchdog_loop(stop))
    for _ in range(50):
        if client.disconnects:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await task
    assert client.disconnects == 1
    assert client_mod.current_client() is None
    assert task.cancelled() is False


async def test_cancelled_connect_tears_the_sender_down_before_the_lock_drops(monkeypatch):
    _arm(monkeypatch, connect_timeout=5.0)
    started = asyncio.Event()
    release = asyncio.Event()
    built = []

    class _Slow(_Client):
        async def connect(self) -> None:
            started.set()
            await release.wait()
            await super().connect()

    def build(*_args):
        client = _Slow()
        built.append(client)
        return client

    monkeypatch.setattr(client_mod, "_build_client", build)
    monkeypatch.setattr(client_mod, "load_session", _session)

    connecting = asyncio.create_task(client_mod.get_client())
    await asyncio.wait_for(started.wait(), timeout=1)
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting

    assert len(built) == 1
    assert built[0].disconnects == 1
    assert client_mod._client is None
    assert client_mod._client_unrecoverable is False

    release.set()
    second = await client_mod.get_client()
    assert second.connected
    assert second is not built[0]
    assert sum(client.connected for client in built) == 1


async def test_discard_blocks_a_new_connect_until_the_old_sender_is_down(monkeypatch):
    _arm(monkeypatch)
    old = _Client()
    old.connected = True
    gate = asyncio.Event()

    async def slow_disconnect():
        await gate.wait()
        old.disconnects += 1
        old.connected = False

    old.disconnect = slow_disconnect  # type: ignore[method-assign]
    client_mod._client = old
    monkeypatch.setattr(client_mod, "_build_client", lambda *_a: _Client())
    monkeypatch.setattr(client_mod, "load_session", _session)

    discarding = asyncio.create_task(client_mod._discard_client(old))
    await asyncio.sleep(0.05)
    connecting = asyncio.create_task(client_mod.get_client())
    await asyncio.sleep(0.05)
    assert not connecting.done()
    assert not old.disconnects
    gate.set()
    await discarding
    fresh = await connecting
    assert old.disconnects == 1
    assert not old.connected
    assert fresh.connected
    assert fresh is not old


async def test_watchdog_resets_a_client_whose_rpcs_never_return(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(limits, "HEALTH_PROBE_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(limits, "HEALTH_PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(limits, "PROBE_FAILURES_BEFORE_RESET", 2)

    class _Silent(_Client):
        async def __call__(self, request):
            await asyncio.Future()

    silent = _Silent()
    silent.connected = True
    client_mod._client = silent
    stop = asyncio.Event()
    task = asyncio.create_task(watchdog_loop(stop))
    for _ in range(40):
        if silent.disconnects:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await task

    assert silent.disconnects == 1
    assert silent._sender._state.time_offset == 0
    assert client_mod.current_client() is None


async def test_watchdog_does_not_connect_an_idle_backend(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(limits, "HEALTH_PROBE_INTERVAL_SECONDS", 0.05)
    probes = []

    async def explode(_client):
        probes.append(1)
        raise AssertionError("probed")

    monkeypatch.setattr("codex_telegram.health.probe_client", explode)
    assert client_mod.current_client() is None
    stop = asyncio.Event()
    task = asyncio.create_task(watchdog_loop(stop))
    await asyncio.sleep(0.2)
    stop.set()
    await task
    assert probes == []


async def test_probe_client_sends_one_cheap_rpc():
    seen = []

    class _Probe:
        async def __call__(self, request):
            seen.append(type(request).__name__)
            return SimpleNamespace()

    await probe_client(_Probe())
    assert seen == ["GetNearestDcRequest"]


def test_repeated_resets_escalate_instead_of_looping(monkeypatch):
    reasons = _arm(monkeypatch, debounce=0.0)
    monkeypatch.setattr(client_mod, "MAX_RESETS_BEFORE_RESTART", 2)

    async def run():
        for _ in range(2):
            client_mod._client = _Client()
            client_mod._client.connected = True
            assert await client_mod.force_reset_client(reason="storm") is True
        client_mod._client = _Client()
        with pytest.raises(ConnectionError, match="kept wedging"):
            await client_mod.force_reset_client(reason="storm")

    asyncio.run(run())
    assert reasons
    assert "did not stick" in reasons[-1]


async def test_debounce_does_not_spare_a_client_connected_after_the_reset(monkeypatch):
    _arm(monkeypatch, debounce=30.0)
    first = _Client()
    first.connected = True
    client_mod._client = first
    assert await client_mod.force_reset_client(reason="storm") is True
    assert first.disconnects == 1

    # Same object put back is the generation we just retired.
    client_mod._client = first
    first.connected = True
    assert await client_mod.force_reset_client(reason="again") is False
    assert first.disconnects == 1

    second = _Client()
    second.connected = True
    client_mod._client = second
    assert await client_mod.force_reset_client(reason="new client wedged") is True
    assert second.disconnects == 1
    assert client_mod.current_client() is None


def test_reap_kills_only_orphaned_proxies(monkeypatch, tmp_path):
    killed = []
    dropped = []
    monkeypatch.setenv("CODEX_TELEGRAM_CONFIG_DIR", str(tmp_path))
    # 10: orphaned serve. 11: orphan that opted out. 20: serve whose parent
    # is alive. 77: pid reused by something that is not a serve.
    monkeypatch.setattr(
        legacy_serve, "registered_proxy_pids", lambda: {10, 11, 20, 77, os.getpid()}
    )
    monkeypatch.setattr(legacy_serve, "_self_and_ancestors", lambda: {os.getpid()})
    monkeypatch.setattr(
        legacy_serve,
        "_parent_pid",
        lambda pid: {10: 1, 11: 1, 20: 50, 77: 1, os.getpid(): 49}[pid],
    )
    monkeypatch.setattr(legacy_serve, "_pid_is_live", lambda pid: pid == 50)

    def cmdline(pid):
        if pid in (10, 11, 20):
            return ["python", "-m", "codex_telegram", "serve"]
        return ["python", "/usr/bin/unrelated-daemon"]

    monkeypatch.setattr(legacy_serve, "_read_cmdline", cmdline)
    monkeypatch.setattr(legacy_serve, "_process_allows_orphan", lambda pid: pid == 11)
    monkeypatch.setattr(legacy_serve.os, "kill", lambda pid, _sig: killed.append(pid))
    monkeypatch.setattr(
        legacy_serve.Path,
        "unlink",
        lambda self, missing_ok=False: dropped.append(self.name),
    )

    assert legacy_serve.reap_orphaned_proxies() == [10]
    assert 11 not in killed
    assert 20 not in killed
    assert 77 not in killed
    assert os.getpid() not in killed
    assert "77" in dropped


def test_parent_gone_tracks_reparent_and_zombies(monkeypatch):
    monkeypatch.delenv("CODEX_TELEGRAM_ALLOW_ORPHAN", raising=False)
    monkeypatch.setattr(legacy_serve.os, "getppid", lambda: 1)
    # Reparented to init, including the case where the host died before we
    # sampled and original_ppid is already 1.
    assert legacy_serve.parent_gone(400) is True
    assert legacy_serve.parent_gone(1) is True

    monkeypatch.setenv("CODEX_TELEGRAM_ALLOW_ORPHAN", "1")
    assert legacy_serve.parent_gone(1) is False
    monkeypatch.delenv("CODEX_TELEGRAM_ALLOW_ORPHAN")

    monkeypatch.setattr(legacy_serve.os, "getppid", lambda: 50)
    monkeypatch.setattr(legacy_serve, "_pid_is_live", lambda _pid: False)
    assert legacy_serve.parent_gone(50) is True
    monkeypatch.setattr(legacy_serve, "_pid_is_live", lambda _pid: True)
    assert legacy_serve.parent_gone(50) is False

    # No procfs (macOS). A live parent whose pid still matches must not look
    # dead just because /proc/<pid>/stat cannot be read.
    monkeypatch.setattr(legacy_serve.os.path, "isdir", lambda _path: False)
    monkeypatch.setattr(legacy_serve, "_pid_is_live", lambda _pid: False)
    assert legacy_serve.parent_gone(50) is False


def test_terminate_refuses_to_signal_a_non_backend(monkeypatch, tmp_path):
    lock = tmp_path / "backend.lock"
    lock.write_text("99\n")
    monkeypatch.setattr(supervision, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(supervision, "is_backend_process", lambda _pid: False)
    signals = []
    monkeypatch.setattr(supervision.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    with pytest.raises(RuntimeError, match="refusing to signal"):
        supervision.terminate_locked_backend(lock)
    assert signals == []


def test_terminate_sigkills_a_backend_that_ignores_sigterm(monkeypatch, tmp_path):
    lock = tmp_path / "backend.lock"
    lock.write_text("99\n")
    alive = True
    signals = []

    monkeypatch.setattr(supervision, "pid_alive", lambda _pid: alive)
    monkeypatch.setattr(supervision, "is_backend_process", lambda _pid: True)

    def kill(_pid, sig):
        nonlocal alive
        signals.append(sig)
        if sig == signal.SIGKILL:
            alive = False

    monkeypatch.setattr(supervision.os, "kill", kill)
    assert supervision.terminate_locked_backend(lock, grace=0.2) is True
    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_terminate_does_not_kill_a_replacement_backend(monkeypatch, tmp_path):
    lock = tmp_path / "backend.lock"
    lock.write_text("200\n")
    signals = []
    monkeypatch.setattr(supervision, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(supervision, "is_backend_process", lambda _pid: True)
    monkeypatch.setattr(supervision.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    # The request was in flight against 100. 200 is the backend a peer already
    # started. Signaling it would wedge the session we just recovered.
    assert supervision.terminate_locked_backend(lock, expected_pid=100) is False
    assert signals == []


def test_is_backend_process_matches_the_module_invocation_only():
    assert supervision._BACKEND_ARGV_TAIL == (b"-m", b"codex_telegram", b"backend")
    # The pytest process is not a backend, whatever its command line contains.
    assert supervision.is_backend_process(os.getpid()) is False


async def test_proxy_timeout_restarts_the_backend_without_retrying_the_call(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_TELEGRAM_CONFIG_DIR", str(tmp_path))
    relay = Relay()
    relay.response_deadline = 2
    calls = []

    async def post(_url, content, headers):
        calls.append("post")
        raise httpx.ReadTimeout("backend wedged")

    relay.client.post = post

    async def restart(self, expected_pid=None):
        calls.append("restart")

    monkeypatch.setattr(Relay, "_restart_unresponsive_backend", restart)
    with pytest.raises(httpx.ReadTimeout):
        await relay.post("{}")
    # One POST, then a restart. Not a second POST: the call may already have
    # been a send, and retrying it would duplicate the Telegram side effect.
    assert calls == ["post", "restart"]


async def test_restart_spawns_only_after_the_old_backend_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_TELEGRAM_CONFIG_DIR", str(tmp_path))
    relay = Relay()
    order = []

    def kill(path, expected_pid=None):
        order.append("kill")
        raise RuntimeError("pid is not a codex_telegram backend")

    monkeypatch.setattr("codex_telegram.stdio_proxy.terminate_locked_backend", kill)

    async def ensure(self):
        order.append("spawn")

    monkeypatch.setattr(Relay, "ensure_backend", ensure)
    await relay._restart_unresponsive_backend()
    assert order == ["kill"]


@pytest.mark.parametrize(
    "exc_type",
    [httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError],
)
async def test_proxy_does_not_replay_a_request_the_backend_may_have_accepted(
    tmp_path, monkeypatch, exc_type
):
    monkeypatch.setenv("CODEX_TELEGRAM_CONFIG_DIR", str(tmp_path))
    relay = Relay()
    calls = []

    async def post(_url, content, headers):
        calls.append("post")
        raise exc_type("backend closed the socket")

    relay.client.post = post

    async def ensure(self):
        calls.append("ensure")

    monkeypatch.setattr(Relay, "ensure_backend", ensure)
    with pytest.raises(exc_type):
        await relay.post("{}")
    assert calls == ["post"]


async def test_proxy_retries_a_connect_failure_that_never_reached_the_backend(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CODEX_TELEGRAM_CONFIG_DIR", str(tmp_path))
    relay = Relay()
    calls = []

    async def post(_url, content, headers):
        calls.append("post")
        if calls.count("post") == 1:
            raise httpx.ConnectError("socket not listening")
        return httpx.Response(202)

    relay.client.post = post

    async def ensure(self):
        calls.append("ensure")

    monkeypatch.setattr(Relay, "ensure_backend", ensure)
    response = await relay.post("{}")
    assert response.status_code == 202
    assert calls == ["post", "ensure", "post"]

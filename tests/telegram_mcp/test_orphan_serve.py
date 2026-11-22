"""A serve exits when its parent or its stdin goes away, and startup does not
kill a serve whose host is still alive. Serves hold no MTProto client."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from test_shared_backend import _kill_backends

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc"), reason="Linux-only")


def _env(config: Path) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "CODEX_TELEGRAM_CONFIG_DIR": str(config),
            "CODEX_TELEGRAM_LEGACY_CLEANUP": "0",
            # Long on purpose: exiting has to happen because stdin/the parent
            # went away, not because starting the backend timed out.
            "CODEX_TELEGRAM_BACKEND_START_TIMEOUT": "30",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return False
    _pid, _comm, tail = stat.read_text(encoding="ascii", errors="replace").rpartition(")")
    return bool(tail.split()) and tail.split()[0] != "Z"


def _wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


@pytest.fixture
def config_dir(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    yield config
    _kill_backends(config)


def test_serve_exits_when_stdin_closes(config_dir, tmp_path):
    stderr_path = tmp_path / "stderr"
    with open(stderr_path, "w", encoding="utf-8") as stderr:
        proc = subprocess.Popen(
            [sys.executable, "-m", "codex_telegram", "serve"],
            env=_env(config_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
        time.sleep(0.3)
        assert _alive(proc.pid)
        proc.stdin.close()
        try:
            code = proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise AssertionError(
                "serve survived stdin EOF past 8s (start timeout is 30s)\n"
                + stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            )
    assert code == 0


def test_serve_exits_when_parent_dies_while_stdin_stays_open(config_dir, tmp_path):
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    pidfile = tmp_path / "serve.pid"
    launcher = tmp_path / "launcher.py"
    # The launcher's only job is to spawn the serve with stdin pointed at the
    # pipe and then die. The test holds the write end, so the serve never sees
    # stdin EOF; it has to notice that its parent pid is gone.
    launcher.write_text(
        textwrap.dedent(
            f"""
            import os, subprocess, sys, time
            from pathlib import Path
            os.dup2({read_fd}, 0)
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_telegram", "serve"],
                env=os.environ.copy(),
                stdout=subprocess.DEVNULL,
                stderr=open(sys.argv[2], "w"),
            )
            open(sys.argv[1], "w").write(str(proc.pid))
            # Stay alive until the serve has finished importing and recorded
            # our pid. Exiting during its import races the reparent, and the
            # serve would never have seen a live parent to watch.
            config = Path(os.environ["CODEX_TELEGRAM_CONFIG_DIR"])
            registry = config / "run" / "proxies" / str(proc.pid)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not registry.exists():
                if proc.poll() is not None:
                    sys.exit(f"serve exited early: {{proc.returncode}}")
                time.sleep(0.05)
            if not registry.exists():
                sys.exit("serve never registered")
            time.sleep(0.5)
            os._exit(0)
            """
        ),
        encoding="utf-8",
    )
    stderr_path = tmp_path / "stderr"
    pid = None
    try:
        subprocess.run(
            [sys.executable, str(launcher), str(pidfile), str(stderr_path)],
            env=_env(config_dir),
            check=True,
            pass_fds=(read_fd,),
        )
        os.close(read_fd)
        read_fd = -1
        pid = int(pidfile.read_text(encoding="ascii"))
        if not _wait_dead(pid, 8):
            raise AssertionError(
                f"serve {pid} still alive 8s after its parent exited\n"
                + stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            )
    finally:
        if read_fd >= 0:
            os.close(read_fd)
        os.close(write_fd)
        if pid and _alive(pid):
            os.kill(pid, 9)


def test_startup_reap_spares_a_serve_whose_parent_is_alive(config_dir, tmp_path):
    env = _env(config_dir)
    env.pop("CODEX_TELEGRAM_LEGACY_CLEANUP", None)  # reap runs at startup
    held = subprocess.Popen(
        [sys.executable, "-m", "codex_telegram", "serve"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        registry = config_dir / "run" / "proxies" / str(held.pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not registry.exists():
            time.sleep(0.05)
        assert registry.exists(), "serve never registered itself"
        assert _alive(held.pid)

        other = subprocess.Popen(
            [sys.executable, "-m", "codex_telegram", "serve"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(1.0)
            assert _alive(held.pid), "startup reap killed a serve whose parent is alive"
        finally:
            other.stdin.close()
            try:
                other.wait(timeout=8)
            except subprocess.TimeoutExpired:
                other.kill()
                other.wait()
    finally:
        if held.poll() is None:
            held.stdin.close()
            try:
                held.wait(timeout=8)
            except subprocess.TimeoutExpired:
                held.kill()
                held.wait()

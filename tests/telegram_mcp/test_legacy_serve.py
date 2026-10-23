"""Legacy direct-stdio serve detection: precise matching, never the proxy itself."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from codex_telegram import legacy_serve
from codex_telegram.legacy_serve import (
    find_legacy_serve_pids,
    is_direct_serve_cmdline,
    is_uv_serve_wrapper_cmdline,
    kill_legacy_serves,
    register_proxy,
    registered_proxy_pids,
)

pytestmark = pytest.mark.skipif(
    not os.path.isdir("/proc"), reason="legacy sweep is /proc-based (Linux only)"
)


class TestDirectServeMatching:
    def test_matches_console_script_invocation(self):
        assert is_direct_serve_cmdline(
            ["/repo/mcp_server/.venv/bin/python", "/repo/mcp_server/.venv/bin/codex-telegram", "serve"]
        )

    def test_matches_module_invocation(self):
        assert is_direct_serve_cmdline(["/usr/bin/python3.12", "-m", "codex_telegram", "serve"])

    def test_rejects_pgrep_with_single_pattern_arg(self):
        # The production hotfix bug: `pgrep -f '... codex-telegram serve$'`
        # matched the other wrapper's own pgrep. A structural match cannot.
        assert not is_direct_serve_cmdline(
            ["pgrep", "-f", "^[^ ]*/python [^ ]*/codex-telegram serve$"]
        )

    def test_rejects_pgrep_with_split_pattern_args(self):
        assert not is_direct_serve_cmdline(["pgrep", "-f", "codex-telegram", "serve"])

    def test_rejects_grep(self):
        assert not is_direct_serve_cmdline(["grep", "codex-telegram serve"])

    def test_rejects_shell_wrapper(self):
        assert not is_direct_serve_cmdline(["bash", "/somewhere/mcp-serve.sh", "serve"])

    def test_rejects_backend_command(self):
        assert not is_direct_serve_cmdline(["/usr/bin/python3", "-m", "codex_telegram", "backend"])

    def test_rejects_login_command(self):
        assert not is_direct_serve_cmdline(
            ["/usr/bin/python3", "/x/bin/codex-telegram", "login"]
        )

    def test_rejects_empty_and_none(self):
        assert not is_direct_serve_cmdline(None)
        assert not is_direct_serve_cmdline([])


class TestUvWrapperMatching:
    def test_matches_uv_run_wrapper(self):
        assert is_uv_serve_wrapper_cmdline(
            ["/usr/local/bin/uv", "run", "--project", "./mcp_server", "codex-telegram", "serve"]
        )

    def test_rejects_other_uv_commands(self):
        assert not is_uv_serve_wrapper_cmdline(["/usr/local/bin/uv", "sync", "codex-telegram", "serve"])
        assert not is_uv_serve_wrapper_cmdline(["uv", "run", "codex-telegram", "login"])


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_TELEGRAM_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _spawn_fake_legacy_serve(tmp_path: Path, config_dir: Path) -> subprocess.Popen:
    """A real process whose cmdline is `<python> .../codex-telegram serve` and
    whose environment points at `config_dir`, exactly like an old-style serve."""
    script = tmp_path / "codex-telegram"
    script.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    env = dict(os.environ, CODEX_TELEGRAM_CONFIG_DIR=str(config_dir))
    return subprocess.Popen(
        [sys.executable, str(script), "serve"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class TestSweep:
    def test_finds_legacy_serve_for_same_config_dir(self, isolated_config, tmp_path):
        proc = _spawn_fake_legacy_serve(tmp_path, isolated_config)
        try:
            time.sleep(0.2)
            assert proc.pid in find_legacy_serve_pids(min_age_seconds=0.0)
        finally:
            proc.kill()
            proc.wait()

    def test_grace_period_protects_fresh_processes(self, isolated_config, tmp_path):
        proc = _spawn_fake_legacy_serve(tmp_path, isolated_config)
        try:
            time.sleep(0.2)
            assert proc.pid not in find_legacy_serve_pids(min_age_seconds=60.0)
        finally:
            proc.kill()
            proc.wait()

    def test_registered_proxy_is_never_matched(self, isolated_config, tmp_path):
        proc = _spawn_fake_legacy_serve(tmp_path, isolated_config)
        try:
            time.sleep(0.2)
            registry_entry = legacy_serve.proxy_registry_dir() / str(proc.pid)
            registry_entry.write_text(f"{proc.pid}\n", encoding="ascii")
            assert proc.pid not in find_legacy_serve_pids(min_age_seconds=0.0)
        finally:
            proc.kill()
            proc.wait()

    def test_other_config_dir_is_left_alone(self, isolated_config, tmp_path):
        other_dir = tmp_path / "other-config"
        other_dir.mkdir()
        proc = _spawn_fake_legacy_serve(tmp_path, other_dir)
        try:
            time.sleep(0.2)
            assert proc.pid not in find_legacy_serve_pids(min_age_seconds=0.0)
        finally:
            proc.kill()
            proc.wait()

    def test_own_process_is_never_matched(self, isolated_config):
        assert os.getpid() not in find_legacy_serve_pids(min_age_seconds=0.0)

    def test_kill_terminates_legacy_serve(self, isolated_config, tmp_path):
        proc = _spawn_fake_legacy_serve(tmp_path, isolated_config)
        try:
            time.sleep(0.2)
            killed = kill_legacy_serves(min_age_seconds=0.0)
            assert proc.pid in killed
            assert proc.wait(timeout=10) != 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class TestRegistry:
    def test_register_and_cleanup_of_stale_entries(self, isolated_config):
        path = register_proxy()
        assert path.exists()
        # A pid that cannot exist (beyond pid_max) is dropped as stale.
        stale = legacy_serve.proxy_registry_dir() / "4194399"
        stale.write_text("4194399\n", encoding="ascii")
        pids = registered_proxy_pids()
        assert os.getpid() in pids
        assert 4194399 not in pids
        assert not stale.exists()

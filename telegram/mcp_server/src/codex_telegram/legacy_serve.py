"""Detect and stop legacy direct-stdio ``codex-telegram serve`` processes.

Before the shared backend existed, every agent host's ``serve`` held its own
MTProto client on the same stored session. During a rolling upgrade those old
processes may still be running, so each new proxy sweeps them once at startup.

Matching is done structurally on ``/proc/<pid>/cmdline`` instead of
``pgrep -f``: the production hotfix learned the hard way that a pgrep pattern
can match the *other* wrapper's own pgrep process. A structural match (argv[0]
is a python interpreter, the last argument is exactly ``serve``) can never
match a pgrep/grep/shell process whose pattern merely *contains* those words.

The new proxy's own argv is identical to a legacy serve (both are
``codex-telegram serve``), so cmdline matching alone cannot tell them apart.
Three guards keep the sweep from killing live proxies:

* every proxy registers its pid under ``<run_dir>/proxies/`` before sweeping,
  and registered pids are never killed;
* matching processes younger than a grace period are skipped, covering the
  window where a concurrent proxy has exec'd but not yet registered itself;
* only processes whose ``CODEX_TELEGRAM_CONFIG_DIR`` (read from
  ``/proc/<pid>/environ``) resolves to the same config dir as ours are
  touched — a serve for a different account/session is left alone, and a
  process whose environment cannot be read is skipped (fail safe).

Everything here is Linux-``/proc`` based; on other platforms the sweep is a
no-op.
"""

from __future__ import annotations

import atexit
import os
import signal
from pathlib import Path

from .runtime_paths import config_dir, proxy_registry_dir
from .session_store import CONFIG_DIR_ENV_VAR

STARTUP_GRACE_SECONDS = 10.0

_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "codex-telegram"


def _basename(arg: str) -> str:
    return os.path.basename(arg)


def _read_cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return [part.decode("utf-8", "replace") for part in raw.rstrip(b"\x00").split(b"\x00")]


def _read_stat_fields(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the (comm) entry, so index 0 is
    field 3 of proc(5). comm may contain spaces/parens; split after the
    last ')'."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    _, _, tail = stat.rpartition(")")
    return tail.split()


def _parent_pid(pid: int) -> int | None:
    fields = _read_stat_fields(pid)
    if not fields or len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _process_age_seconds(pid: int) -> float | None:
    fields = _read_stat_fields(pid)
    if not fields or len(fields) < 20:
        return None
    try:
        starttime_ticks = float(fields[19])  # proc(5) field 22: starttime
        uptime = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return uptime - starttime_ticks / os.sysconf("SC_CLK_TCK")


def _process_config_dir(pid: int) -> Path | None:
    """The config dir the process sees, from its environment; None when the
    environment cannot be read."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    prefix = f"{CONFIG_DIR_ENV_VAR}=".encode("ascii")
    for entry in raw.split(b"\x00"):
        if entry.startswith(prefix):
            value = entry[len(prefix):].decode("utf-8", "replace")
            if value:
                return Path(value).expanduser().resolve()
    return _DEFAULT_CONFIG_DIR.resolve()


def is_direct_serve_cmdline(cmdline: list[str] | None) -> bool:
    """True for ``<python> .../codex-telegram serve`` or
    ``<python> -m codex_telegram serve``.

    Deliberately strict: the final argv entry must be exactly ``serve`` and
    argv[0] must be a python interpreter, so a ``pgrep -f '...serve$'``, a
    grep, or a shell wrapper can never match.
    """
    if not cmdline or len(cmdline) < 3 or cmdline[-1] != "serve":
        return False
    if not _basename(cmdline[0]).startswith("python"):
        return False
    if _basename(cmdline[-2]) == "codex-telegram":
        return True
    return cmdline[-3:-1] == ["-m", "codex_telegram"]


def is_uv_serve_wrapper_cmdline(cmdline: list[str] | None) -> bool:
    """True for a ``uv run ... codex-telegram serve`` parent wrapper."""
    if not cmdline or len(cmdline) < 4:
        return False
    return (
        _basename(cmdline[0]) == "uv"
        and cmdline[1] == "run"
        and cmdline[-2:] == ["codex-telegram", "serve"]
    )


def register_proxy() -> Path:
    """Record this process in the proxy registry so sweeps never target it."""
    path = proxy_registry_dir() / str(os.getpid())
    path.write_text(f"{os.getpid()}\n", encoding="ascii")
    atexit.register(lambda: path.unlink(missing_ok=True))
    return path


def registered_proxy_pids() -> set[int]:
    pids: set[int] = set()
    for entry in proxy_registry_dir().iterdir():
        try:
            pid = int(entry.name)
        except ValueError:
            entry.unlink(missing_ok=True)
            continue
        if not Path(f"/proc/{pid}").exists():
            # Dead proxy left a stale entry behind (hard kill skips atexit).
            entry.unlink(missing_ok=True)
            continue
        pids.add(pid)
    return pids


def _self_and_ancestors() -> set[int]:
    pids: set[int] = set()
    pid = os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        parent = _parent_pid(pid)
        if parent is None:
            break
        pid = parent
    return pids


def find_legacy_serve_pids(*, min_age_seconds: float = STARTUP_GRACE_SECONDS) -> list[int]:
    if not os.path.isdir("/proc"):
        return []
    own_config_dir = config_dir().resolve()
    protected = _self_and_ancestors() | registered_proxy_pids()
    uid = os.getuid()
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in protected:
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        if not is_direct_serve_cmdline(_read_cmdline(pid)):
            continue
        age = _process_age_seconds(pid)
        if age is None or age < min_age_seconds:
            continue
        if _process_config_dir(pid) != own_config_dir:
            continue
        found.append(pid)
    return found


def kill_legacy_serves(
    *,
    min_age_seconds: float = STARTUP_GRACE_SECONDS,
    log=None,
) -> list[int]:
    """SIGTERM every legacy serve (and its uv-run wrapper); return killed pids."""
    killed: list[int] = []
    ancestors = _self_and_ancestors()
    for pid in find_legacy_serve_pids(min_age_seconds=min_age_seconds):
        parent = _parent_pid(pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        killed.append(pid)
        if log is not None:
            log(f"stopped legacy serve pid={pid}")
        if (
            parent is not None
            and parent > 1
            and parent not in ancestors
            and is_uv_serve_wrapper_cmdline(_read_cmdline(parent))
        ):
            try:
                os.kill(parent, signal.SIGTERM)
            except OSError:
                continue
            killed.append(parent)
            if log is not None:
                log(f"stopped legacy serve wrapper pid={parent}")
    return killed

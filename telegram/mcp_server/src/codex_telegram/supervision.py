"""Stop a wedged backend without ever leaving two of them alive.

The proxy uses this when the backend holds the unix socket but stops
answering. The old process has to be dead — flock released, Telethon client
gone with it — before the caller spawns a replacement. Signaling a pid whose
command line is not this package's backend is refused.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time

_BACKEND_ARGV_TAIL = (b"-m", b"codex_telegram", b"backend")


def _read_lock_record(lock_path: Path) -> tuple[int | None, str | None]:
    """Pid and start token. The token is absent on locks written before it existed."""
    try:
        text = lock_path.read_text(encoding="ascii").strip()
    except OSError:
        return None, None
    if not text:
        return None, None
    pid_text, _sep, token = text.partition(" ")
    try:
        pid = int(pid_text)
    except ValueError:
        return None, None
    token = token.strip() or None
    return pid, token


def _read_lock_pid(lock_path: Path) -> int | None:
    pid, _token = _read_lock_record(lock_path)
    return pid


def _ps_field(pid: int, column: str) -> str | None:
    """One `ps -o` column. Used when `/proc` is not there (macOS)."""
    try:
        out = subprocess.check_output(
            ["ps", "-o", f"{column}=", "-p", str(pid)],
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.strip()
    return text or None


def _posix_pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_start_token(pid: int) -> str | None:
    """Stable for the life of that pid. proc(5) field 22, or `ps` lstart."""
    if os.path.isdir(f"/proc/{pid}"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        _pid, _comm, tail = stat.rpartition(")")
        fields = tail.split()
        # field 3 is index 0; field 22 is index 19.
        if len(fields) < 20:
            return None
        return fields[19]
    return _ps_field(pid, "lstart")


def _proc_state(pid: int) -> str | None:
    """proc(5) state character, or None if the process does not exist.

    ``Z`` is a zombie: the kernel has already released its flocks and closed
    its sockets, it just has not been reaped. Callers treat that as dead so a
    new backend can take the lock without waiting on the parent.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    _pid, _comm, tail = stat.rpartition(")")
    fields = tail.split()
    if not fields:
        return None
    return fields[0]


def pid_alive(pid: int) -> bool:
    if os.path.isdir("/proc"):
        state = _proc_state(pid)
        return state is not None and state != "Z"
    return _posix_pid_exists(pid)


def _command_is_backend(parts: list[str]) -> bool:
    if len(parts) >= 4 and parts[-3:] == ["-m", "codex_telegram", "backend"]:
        return parts[0].rsplit("/", 1)[-1].startswith("python")
    if len(parts) >= 2 and parts[-1] == "backend":
        return parts[-2].rsplit("/", 1)[-1] == "codex-telegram"
    return False


def is_backend_process(pid: int) -> bool:
    """True only for ``python -m codex_telegram backend`` (or the console script)."""
    if not os.path.isdir("/proc"):
        command = _ps_field(pid, "command")
        return bool(command) and _command_is_backend(command.split())
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    parts = [part for part in raw.split(b"\x00") if part]
    if len(parts) >= 3 and tuple(parts[-3:]) == _BACKEND_ARGV_TAIL:
        return True
    return len(parts) >= 2 and parts[-1] == b"backend" and parts[-2].endswith(b"codex-telegram")


def locked_backend_pid(lock_path: Path) -> int | None:
    """Pid currently holding the backend lock, if that process is still alive."""
    pid = _read_lock_pid(lock_path)
    if pid is None or pid <= 0 or not pid_alive(pid):
        return None
    return pid


def _wait_until_dead(pid: int, grace: float) -> bool:
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


def terminate_locked_backend(
    lock_path: Path,
    *,
    grace: float = 8.0,
    expected_pid: int | None = None,
) -> bool:
    """SIGTERM the backend holding ``lock_path``, SIGKILL if it survives.

    Returns False when no backend was running, or when ``expected_pid`` is set
    and the lock now names a different process (a peer proxy already replaced
    the one this request was talking to). Raises if the pid in the lock file
    is not our backend (we will not signal an unrelated process) or if it is
    still alive after SIGKILL. Returns only once the old process can no longer
    hold an MTProto session.
    """
    pid, token = _read_lock_record(lock_path)
    if expected_pid is not None and pid != expected_pid:
        return False
    if pid is None or pid <= 0 or not pid_alive(pid):
        return False
    if not is_backend_process(pid):
        raise RuntimeError(
            f"refusing to signal pid {pid}: its command line is not a "
            "codex_telegram backend"
        )
    if token is not None:
        live = process_start_token(pid)
        if live != token:
            raise RuntimeError(
                f"refusing to signal pid {pid}: its start time does not match "
                "the backend lock"
            )
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    if _wait_until_dead(pid, grace):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    if _wait_until_dead(pid, grace):
        return True
    raise RuntimeError(f"backend pid {pid} survived SIGKILL")

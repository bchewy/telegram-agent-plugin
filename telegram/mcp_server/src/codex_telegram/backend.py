"""Single-instance shared MCP backend that owns the only Telegram client.

Every agent host spawns its own stdio ``serve``. Before the shared backend
existed, each of those held a separate MTProto client on the same stored
session, which Telegram punishes with ``AUTH_KEY_DUPLICATED`` (it revokes the
auth key) and which showed up as ``get_me``/``get_unread`` timeouts. This
process is therefore the only place a Telethon client may live: stdio proxies
(see ``stdio_proxy``) relay JSON-RPC to it over streamable HTTP on a unix
socket.

Single instance is enforced with a non-blocking ``flock`` on ``backend.lock``
held for the life of the process; a second backend exits immediately. The
socket is bound by us rather than uvicorn so it is created 0600 inside a 0700
run dir. To restart, kill this process — the next proxy request respawns it
under the same lock.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import time
from pathlib import Path

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from .runtime_paths import backend_lock_path, backend_socket_path
from .server import mcp

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX; backend mode needs flock
    fcntl = None  # type: ignore[assignment]

SOCKET_BACKLOG = 64
GRACEFUL_SHUTDOWN_SECONDS = 5

_NOISY_LOGGERS = (
    "mcp",
    "mcp.server",
    "mcp.server.streamable_http",
    "mcp.server.lowlevel.server",
    "httpx",
    "httpcore",
)


def _log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"{timestamp} backend pid={os.getpid()} {message}", file=sys.stderr, flush=True)


def acquire_backend_lock(lock_path: Path) -> int | None:
    """Take the single-instance lock; return None when another backend holds it.

    The returned fd must stay open for the life of the backend — the flock is
    released when it is closed.
    """
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    return fd


def read_backend_pid(lock_path: Path) -> int | None:
    """Best-effort pid of the lock-holding backend (for diagnostics/tests)."""
    try:
        return int(lock_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def bind_backend_socket(sock_path: Path) -> socket.socket:
    # Only the lock holder calls this, so any existing socket file is stale.
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(sock_path))
    # uvicorn's own uds= support chmods the socket 0666; we bind it ourselves
    # and hand uvicorn the fd so the socket stays owner-only.
    os.chmod(sock_path, 0o600)
    sock.listen(SOCKET_BACKLOG)
    return sock


def build_app():
    """Configure the existing FastMCP server for shared streamable-http use."""
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.streamable_http_path = "/mcp"
    # Clients reach us through a 0600 unix socket, never the network, so Host
    # header (DNS rebinding) checks would only produce false rejections here.
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    return mcp.streamable_http_app()


def run_backend() -> int:
    if fcntl is None:
        _log("the shared backend requires a POSIX platform (fcntl/flock)")
        return 1

    # Everything the backend creates (socket, lock, logs) stays owner-only.
    os.umask(0o077)

    lock_path = backend_lock_path()
    lock_fd = acquire_backend_lock(lock_path)
    if lock_fd is None:
        _log(f"another backend already holds {lock_path}; exiting")
        return 0

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    sock_path = backend_socket_path()
    app = build_app()
    server_socket = bind_backend_socket(sock_path)
    _log(f"listening on {sock_path}")

    config = uvicorn.Config(
        app,
        fd=server_socket.fileno(),
        log_level="warning",
        lifespan="on",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    finally:
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        _log("stopped")
    return 0

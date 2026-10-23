"""Filesystem locations for the shared-backend runtime.

Everything defaults to a `run/` directory next to the existing session/config
dir (``CODEX_TELEGRAM_CONFIG_DIR`` or ``~/.config/codex-telegram``) and each
path can be overridden individually through an env var. Nothing in here may
hardcode machine-specific paths.
"""

from __future__ import annotations

import os
from pathlib import Path

from .session_store import _config_dir

BACKEND_SOCK_ENV_VAR = "CODEX_TELEGRAM_BACKEND_SOCK"
BACKEND_LOCK_ENV_VAR = "CODEX_TELEGRAM_BACKEND_LOCK"
BACKEND_LOG_ENV_VAR = "CODEX_TELEGRAM_BACKEND_LOG"

RUN_DIR_NAME = "run"
PROXY_REGISTRY_DIR_NAME = "proxies"


def config_dir() -> Path:
    return _config_dir()


def run_dir() -> Path:
    """Runtime dir for the socket/lock/log, created 0700 (socket parent must
    not be traversable by other users)."""
    path = config_dir() / RUN_DIR_NAME
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir mode is filtered by umask and ignored for pre-existing dirs.
    os.chmod(path, 0o700)
    return path


def _path_from_env(env_var: str, default_name: str) -> Path:
    override = os.getenv(env_var)
    if override:
        path = Path(override).expanduser()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path
    return run_dir() / default_name


def backend_socket_path() -> Path:
    return _path_from_env(BACKEND_SOCK_ENV_VAR, "backend.sock")


def backend_lock_path() -> Path:
    return _path_from_env(BACKEND_LOCK_ENV_VAR, "backend.lock")


def backend_log_path() -> Path:
    return _path_from_env(BACKEND_LOG_ENV_VAR, "backend.log")


def proxy_registry_dir() -> Path:
    path = run_dir() / PROXY_REGISTRY_DIR_NAME
    path.mkdir(mode=0o700, exist_ok=True)
    return path

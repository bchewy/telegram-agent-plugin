from __future__ import annotations

import pytest

from codex_telegram.__main__ import build_parser
from codex_telegram.stdio_proxy import shared_backend_enabled


def test_cli_accepts_login_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "login",
            "--api-id",
            "12345",
            "--api-hash",
            "secret",
            "--phone",
            "+15555555555",
        ]
    )

    assert args.command == "login"
    assert args.api_id == 12345
    assert args.api_hash == "secret"
    assert args.phone == "+15555555555"


def test_cli_rejects_master_key_flag():
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["login", "--master-key", "abc"])


def test_cli_accepts_serve_command():
    parser = build_parser()
    args = parser.parse_args(["serve"])
    assert args.command == "serve"


def test_cli_accepts_backend_command():
    parser = build_parser()
    args = parser.parse_args(["backend"])
    assert args.command == "backend"


def test_shared_backend_enabled_by_default(monkeypatch):
    monkeypatch.delenv("CODEX_TELEGRAM_SHARED_BACKEND", raising=False)
    monkeypatch.delenv("TELEGRAM_MCP_SHARED_BACKEND", raising=False)
    assert shared_backend_enabled()


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
@pytest.mark.parametrize(
    "env_var", ["CODEX_TELEGRAM_SHARED_BACKEND", "TELEGRAM_MCP_SHARED_BACKEND"]
)
def test_shared_backend_opt_out(monkeypatch, env_var, value):
    monkeypatch.delenv("CODEX_TELEGRAM_SHARED_BACKEND", raising=False)
    monkeypatch.delenv("TELEGRAM_MCP_SHARED_BACKEND", raising=False)
    monkeypatch.setenv(env_var, value)
    assert not shared_backend_enabled()


def test_shared_backend_explicit_enable(monkeypatch):
    monkeypatch.setenv("CODEX_TELEGRAM_SHARED_BACKEND", "1")
    assert shared_backend_enabled()

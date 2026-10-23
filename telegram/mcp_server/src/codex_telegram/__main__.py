from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .auth import login_interactive, whoami_interactive
from .backend import run_backend
from .server import run_server
from .session_store import clear_session, describe_storage
from .stdio_proxy import run_proxy, shared_backend_enabled


def _redact_phone(phone: str | None) -> str | None:
    if not phone:
        return phone
    if len(phone) <= 5:
        # Too short to keep a meaningful prefix/suffix; mask everything.
        return "*" * len(phone)
    return phone[:3] + "*" * (len(phone) - 5) + phone[-2:]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-telegram",
        description="Telethon-backed Telegram MCP server for Codex.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "serve",
        help=(
            "Run the MCP server over stdio. By default this is a thin proxy to "
            "the shared single-instance backend; set CODEX_TELEGRAM_SHARED_BACKEND=0 "
            "for the legacy direct mode with its own Telegram client."
        ),
    )

    subparsers.add_parser(
        "backend",
        help=(
            "Run the shared single-instance backend: streamable HTTP on a 0600 "
            "unix socket, flock-guarded so only one Telegram client ever exists."
        ),
    )

    login = subparsers.add_parser("login", help="Authenticate a Telegram account.")
    login.add_argument("--api-id", type=int, default=None)
    login.add_argument("--api-hash", default=None)
    login.add_argument("--phone", default=None)

    subparsers.add_parser("whoami", help="Print the currently stored Telegram account.")

    subparsers.add_parser("logout", help="Clear the stored Telegram session.")

    subparsers.add_parser("storage", help="Describe the current credential storage backend.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "serve":
        if shared_backend_enabled():
            run_proxy()
        else:
            run_server()
        return

    if args.command == "backend":
        sys.exit(run_backend())

    if args.command == "login":
        result = asyncio.run(
            login_interactive(
                api_id=args.api_id,
                api_hash=args.api_hash,
                phone=args.phone,
            )
        )
        print(
            json.dumps(
                {
                    "ok": result["ok"],
                    "storage": result["storage"],
                    "user_id": result["user_id"],
                    "username": result["username"],
                    "display_name": result["display_name"],
                    "phone": _redact_phone(result.get("phone")),
                }
            )
        )
        return

    if args.command == "whoami":
        result = asyncio.run(whoami_interactive())
        print(
            json.dumps(
                {
                    "user_ref": result.get("user_ref"),
                    "username": result.get("username"),
                    "display_name": result.get("display_name"),
                    "phone": _redact_phone(result.get("phone")),
                }
            )
        )
        return

    if args.command == "logout":
        removed = clear_session(prompt_if_missing=True)
        print(json.dumps({"logged_out": removed}))
        return

    if args.command == "storage":
        print(json.dumps(describe_storage(), indent=2))
        return

    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()

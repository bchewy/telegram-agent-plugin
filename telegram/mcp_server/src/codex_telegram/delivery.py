"""Tools whose Telegram RPC must not be issued twice.

A dropped ack after ``send_message`` / ``send_file`` / ``forward`` / ``edit``
does not mean the server rejected the call. Retrying it delivers a second
copy. Callers get an error that says delivery is unknown instead.
"""

from __future__ import annotations

# Names are the MCP tool names, not the Telethon methods.
MUTATING_TOOLS = frozenset(
    {
        "send_message",
        "reply_message",
        "edit_message",
        "delete_messages",
        "forward_messages",
        "pin_message",
        "unpin_message",
        "send_photo",
        "send_document",
        "send_voice",
        "send_video",
        "send_sticker",
        "send_animation",
        "send_poll",
        "set_profile_photo",
        "delete_contact",
        "delete_chat",
    }
)


def is_mutating_tool(name: str | None) -> bool:
    return isinstance(name, str) and name in MUTATING_TOOLS


def delivery_unknown_message(name: str) -> str:
    return (
        f"{name}: delivery unknown. The request reached Telegram and the "
        "outcome was not observed, so it was not retried. It may already "
        "have been delivered."
    )

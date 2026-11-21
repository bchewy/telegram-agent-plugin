from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .health import install_tool_call_guard
from .tools import account, cache, contacts, dialogs, extras, groups, media, messages

mcp = FastMCP(
    name="telegram",
    instructions=(
        "Tools for reading, summarizing, searching, sending, and managing Telegram "
        "messages with a user-authorized Telethon session."
    ),
    json_response=True,
)

dialogs.register(mcp)
messages.register(mcp)
cache.register(mcp)
media.register(mcp)
groups.register(mcp)
contacts.register(mcp)
extras.register(mcp)
account.register(mcp)
install_tool_call_guard(mcp)


def run_server() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_server()

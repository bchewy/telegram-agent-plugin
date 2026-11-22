"""Keep a wedged-client test from killing the pytest process.

Escalation is ``os._exit`` in production so a stuck Telethon client cannot be
followed by a second one. Inside the suite it raises instead, and the globals
a test mutates are cleared so they cannot leak into the next test.
"""

from __future__ import annotations

import asyncio

import pytest

from codex_telegram import client as client_mod
from codex_telegram.health import INFLIGHT, uninstall_storm_handler


@pytest.fixture(autouse=True)
def _isolate_telegram_runtime(monkeypatch):
    def _fail_closed(reason: str) -> None:
        raise SystemExit(reason)

    monkeypatch.setattr(client_mod, "_escalation_handler", _fail_closed)
    # pytest-asyncio gives each test its own loop, and asyncio.Lock stays
    # bound to the loop that first waited on it. A fresh lock per test keeps
    # a waiter in this test from tripping over the previous test's loop.
    client_mod._client_lock = asyncio.Lock()
    yield
    client_mod._client = None
    client_mod._client_unrecoverable = False
    client_mod._last_reset_monotonic = 0.0
    client_mod._debounced_generation = None
    client_mod._reset_history.clear()
    client_mod._last_client_verify_monotonic = 0.0
    INFLIGHT.clear()
    uninstall_storm_handler()

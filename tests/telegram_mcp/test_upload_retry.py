from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_telegram import client as client_mod
from codex_telegram.client import TelegramUploadError, upload_with_retry
from codex_telegram.tools import media


@pytest.fixture
def upload_file(tmp_path) -> Path:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 1024)
    return path


@pytest.fixture
def retry_env(monkeypatch):
    """Stub the client lifecycle hooks `upload_with_retry` uses between attempts."""
    state = SimpleNamespace(discards=0, sleeps=[], fresh_clients=[])

    async def fake_get_client():
        fresh = SimpleNamespace(name=f"fresh-{len(state.fresh_clients)}")
        state.fresh_clients.append(fresh)
        return fresh

    async def fake_discard_client():
        state.discards += 1

    async def fake_sleep(seconds):
        state.sleeps.append(seconds)

    monkeypatch.setattr(client_mod, "get_client", fake_get_client)
    monkeypatch.setattr(client_mod, "_discard_client", fake_discard_client)
    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)
    return state


async def test_succeeds_on_later_attempt_after_connection_errors(upload_file, retry_env):
    failures = [ConnectionResetError("peer reset"), BrokenPipeError("broken pipe")]
    attempts = []

    async def do_upload(client):
        attempts.append(client)
        if failures:
            raise failures.pop(0)
        return "sent"

    result = await upload_with_retry(do_upload, file_path=upload_file, tool_name="send_video")

    assert result == "sent"
    assert len(attempts) == 3
    assert retry_env.discards == 2
    assert retry_env.sleeps == [2.0, 5.0]


async def test_exhausted_retries_fail_fast_with_clear_error(upload_file, retry_env):
    attempts = []

    async def do_upload(client):
        attempts.append(client)
        raise ConnectionResetError("peer reset")

    with pytest.raises(TelegramUploadError) as excinfo:
        await upload_with_retry(do_upload, file_path=upload_file, tool_name="send_video")

    assert len(attempts) == client_mod.UPLOAD_ATTEMPTS
    assert retry_env.discards == client_mod.UPLOAD_ATTEMPTS
    message = str(excinfo.value)
    assert "send_video" in message
    assert "video.mp4" in message
    assert f"after {client_mod.UPLOAD_ATTEMPTS} attempts" in message
    assert "ConnectionResetError" in message
    assert isinstance(excinfo.value.__cause__, ConnectionResetError)


async def test_non_connection_errors_propagate_without_retry(upload_file, retry_env):
    attempts = []

    async def do_upload(client):
        attempts.append(client)
        raise ValueError("bad caption")

    with pytest.raises(ValueError, match="bad caption"):
        await upload_with_retry(do_upload, file_path=upload_file, tool_name="send_video")

    assert len(attempts) == 1
    assert retry_env.discards == 0
    assert retry_env.sleeps == []


async def test_stalled_upload_times_out_and_retries(upload_file, retry_env, monkeypatch):
    monkeypatch.setattr(client_mod, "_upload_timeout_seconds", lambda _path: 0.01)
    attempts = []

    async def do_upload(client):
        attempts.append(client)
        # A reconnect-looping Telethon client never resolves the request;
        # the per-attempt timeout must convert that hang into a retry.
        await asyncio.Future()

    with pytest.raises(TelegramUploadError) as excinfo:
        await upload_with_retry(do_upload, file_path=upload_file, tool_name="send_document")

    assert len(attempts) == client_mod.UPLOAD_ATTEMPTS
    assert isinstance(excinfo.value.__cause__, TimeoutError)


async def test_retries_use_a_fresh_client_after_discard(upload_file, retry_env):
    initial_client = SimpleNamespace(name="initial")
    seen = []

    async def do_upload(client):
        seen.append(client)
        if len(seen) == 1:
            raise ConnectionResetError("peer reset")
        return "sent"

    result = await upload_with_retry(
        do_upload,
        file_path=upload_file,
        tool_name="send_photo",
        client=initial_client,
    )

    assert result == "sent"
    assert seen[0] is initial_client
    assert seen[1] is retry_env.fresh_clients[0]
    assert retry_env.discards == 1


def test_upload_timeout_scales_with_file_size(tmp_path):
    small = tmp_path / "small.bin"
    small.write_bytes(b"x")
    large = tmp_path / "large.bin"
    large.write_bytes(b"x" * (7 * 1024 * 1024))

    small_timeout = client_mod._upload_timeout_seconds(small)
    large_timeout = client_mod._upload_timeout_seconds(large)

    assert small_timeout == pytest.approx(client_mod.UPLOAD_BASE_TIMEOUT_SECONDS, abs=1.0)
    assert large_timeout > small_timeout
    expected = client_mod.UPLOAD_BASE_TIMEOUT_SECONDS + (
        7 * 1024 * 1024 / client_mod.UPLOAD_MIN_BYTES_PER_SECOND
    )
    assert large_timeout == pytest.approx(expected)


class _FakeMCP:
    def __init__(self):
        self.tools: dict[str, object] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _async_value(value):
    async def inner(*_args, **_kwargs):
        return value

    return inner


async def test_send_video_recovers_from_mid_upload_connection_drop(
    upload_file, retry_env, monkeypatch
):
    mcp = _FakeMCP()
    media.register(mcp)
    send_video = mcp.tools["send_video"]

    sent_message = SimpleNamespace(id=99)
    send_file_calls = []

    async def flaky_send_file(entity, path, **kwargs):
        send_file_calls.append((entity, path, kwargs))
        if len(send_file_calls) == 1:
            raise ConnectionResetError("upload died mid-chunk")
        return sent_message

    initial_client = SimpleNamespace(send_file=flaky_send_file)
    # The fresh client handed out after the discard must also upload.
    retry_env_get_client = client_mod.get_client

    async def fake_get_client():
        fresh = await retry_env_get_client()
        fresh.send_file = flaky_send_file
        return fresh

    monkeypatch.setattr(client_mod, "get_client", fake_get_client)
    monkeypatch.setattr(media, "get_client", _async_value(initial_client))
    monkeypatch.setattr(media, "resolve_entity", _async_value(SimpleNamespace(id=1)))
    monkeypatch.setattr(
        media, "resolve_upload_path", lambda *_args, **_kwargs: (upload_file, None)
    )
    monkeypatch.setattr(media, "message_to_dict", lambda message: {"id": message.id})

    result = await send_video(chat_ref="@friend", file_path=str(upload_file))

    assert result == {"id": 99}
    assert len(send_file_calls) == 2
    assert retry_env.discards == 1
    assert send_file_calls[1][2]["supports_streaming"] is True

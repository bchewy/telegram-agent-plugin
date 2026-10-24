from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal

from telethon import functions

from ..client import get_client, upload_with_retry, with_flood_wait
from ..helpers import (
    ensure_download_dir,
    message_to_dict,
    parse_datetime,
    peer_ref,
    resolve_entity,
    resolve_upload_path,
)
from ..safety import require_destructive


async def _resolve_message(client, chat_ref: str, message_id: int):
    entity = await resolve_entity(client, chat_ref)
    message = await client.get_messages(entity, ids=message_id)
    if not message:
        raise RuntimeError(f"Message {message_id} was not found in {chat_ref}.")
    return entity, message


def _resolve_skill_script(name: str) -> Path:
    override = os.getenv("CODEX_TELEGRAM_MEDIA_SCRIPTS_DIR")
    if override:
        candidate = (Path(override).expanduser() / name).resolve()
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"could not locate skill script {name} in {candidate.parent}")

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "skills" / "telegram-media-inspect" / "scripts" / name
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(f"could not locate skill script {name}")


INSPECT_SCRIPT_TIMEOUT_SECONDS = 300.0


async def _inspect_media_file(script: Path, media_path: str | Path, output_dir: Path | None = None) -> dict:
    command = ["bash", str(script), str(media_path)]
    if output_dir is not None:
        command.append(str(output_dir))

    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Own process group, so a kill reaches ffmpeg/whisper children and
        # not just the bash wrapper.
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=INSPECT_SCRIPT_TIMEOUT_SECONDS
        )
    except (TimeoutError, asyncio.CancelledError) as exc:
        # Don't leave orphaned ffmpeg/whisper processes behind when the
        # inspection times out or the MCP call is cancelled.
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, PermissionError):
                proc.kill()
            await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise RuntimeError(
            f"{script.name} timed out after {INSPECT_SCRIPT_TIMEOUT_SECONDS:.0f}s"
        ) from exc

    if proc.returncode != 0:
        error = stderr.decode(errors="replace").strip() or f"{script.name} failed"
        raise RuntimeError(error)

    lines = [line.strip() for line in stdout.decode(errors="replace").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"{script.name} did not return a JSON payload")

    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{script.name} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"{script.name} returned JSON {type(payload).__name__}, expected object"
        )
    return payload


def register(mcp) -> None:
    @mcp.tool()
    @with_flood_wait
    async def download_media(
        chat_ref: str,
        message_id: int | None = None,
        output_dir: str | None = None,
        limit: int = 1,
    ) -> dict:
        """Download media from one message or the latest media messages in a chat."""
        client = await get_client()
        entity = await resolve_entity(client, chat_ref)
        target_dir = ensure_download_dir(output_dir)

        if message_id is not None:
            _, message = await _resolve_message(client, chat_ref, message_id)
            file_path = await client.download_media(message, file=target_dir)
            return {
                "chat_ref": peer_ref(entity),
                "message_id": message_id,
                "downloaded_to": file_path,
            }

        messages = await client.get_messages(entity, limit=max(limit * 4, limit))
        downloaded = []
        for message in messages:
            if not message.media:
                continue
            file_path = await client.download_media(message, file=target_dir)
            downloaded.append({"message_id": message.id, "path": file_path})
            if len(downloaded) >= limit:
                break
        return {"chat_ref": peer_ref(entity), "downloads": downloaded}

    @mcp.tool()
    @with_flood_wait
    async def download_profile_photo(
        chat_ref: str,
        output_dir: str | None = None,
        download_big: bool = True,
    ) -> dict:
        """Download the profile photo for a user/chat/channel."""
        client = await get_client()
        entity = await resolve_entity(client, chat_ref)
        target_dir = ensure_download_dir(output_dir)
        file_path = await client.download_profile_photo(entity, file=target_dir, download_big=download_big)
        return {"chat_ref": peer_ref(entity), "downloaded_to": file_path}

    @mcp.tool()
    @with_flood_wait
    async def send_photo(
        chat_ref: str,
        file_path: str,
        caption: str | None = None,
        reply_to: int | None = None,
        schedule_at: str | None = None,
        silent: bool = False,
        allow_arbitrary_path: bool = False,
    ) -> dict:
        """Send a photo."""
        client = await get_client()
        entity = await resolve_entity(client, chat_ref)
        upload_path, warning = resolve_upload_path(
            file_path,
            allow_arbitrary_path=allow_arbitrary_path,
        )
        message = await upload_with_retry(
            lambda c: c.send_file(
                entity,
                upload_path,
                caption=caption,
                reply_to=reply_to,
                silent=silent,
                schedule=parse_datetime(schedule_at),
            ),
            file_path=upload_path,
            tool_name="send_photo",
            client=client,
        )
        payload = message_to_dict(message)
        if warning:
            payload["upload_warning"] = warning
        return payload

    @mcp.tool()
    @with_flood_wait
    async def send_document(
        chat_ref: str,
        file_path: str,
        caption: str | None = None,
        reply_to: int | None = None,
        force_document: bool = True,
        schedule_at: str | None = None,
        silent: bool = False,
        allow_arbitrary_path: bool = False,
    ) -> dict:
        """Send a document/file."""
        client = await get_client()
        entity = await resolve_entity(client, chat_ref)
        upload_path, warning = resolve_upload_path(
            file_path,
            allow_arbitrary_path=allow_arbitrary_path,
        )
        message = await upload_with_retry(
            lambda c: c.send_file(
                entity,
                upload_path,
                caption=caption,
                reply_to=reply_to,
                force_document=force_document,
                silent=silent,
                schedule=parse_datetime(schedule_at),
            ),
            file_path=upload_path,
            tool_name="send_document",
            client=client,
        )
        payload = message_to_dict(message)
        if warning:
            payload["upload_warning"] = warning
        return payload

    @mcp.tool()
    @with_flood_wait
    async def send_voice(
        chat_ref: str,
        file_path: str,
        caption: str | None = None,
        reply_to: int | None = None,
        schedule_at: str | None = None,
        silent: bool = False,
        allow_arbitrary_path: bool = False,
    ) -> dict:
        """Send a voice note."""
        client = await get_client()
        entity = await resolve_entity(client, chat_ref)
        upload_path, warning = resolve_upload_path(
            file_path,
            allow_arbitrary_path=allow_arbitrary_path,
        )
        message = await upload_with_retry(
            lambda c: c.send_file(
                entity,
                upload_path,
                caption=caption,
                reply_to=reply_to,
                voice_note=True,
                silent=silent,
                schedule=parse_datetime(schedule_at),
            ),
            file_path=upload_path,
            tool_name="send_voice",
            client=client,
        )
        payload = message_to_dict(message)
        if warning:
            payload["upload_warning"] = warning
        return payload

    @mcp.tool()
    @with_flood_wait
    async def send_video(
        chat_ref: str,
        file_path: str,
        caption: str | None = None,
        reply_to: int | None = None,
        supports_streaming: bool = True,
        schedule_at: str | None = None,
        silent: bool = False,
        allow_arbitrary_path: bool = False,
    ) -> dict:
        """Send a video."""
        client = await get_client()
        entity = await resolve_entity(client, chat_ref)
        upload_path, warning = resolve_upload_path(
            file_path,
            allow_arbitrary_path=allow_arbitrary_path,
        )
        message = await upload_with_retry(
            lambda c: c.send_file(
                entity,
                upload_path,
                caption=caption,
                reply_to=reply_to,
                supports_streaming=supports_streaming,
                silent=silent,
                schedule=parse_datetime(schedule_at),
            ),
            file_path=upload_path,
            tool_name="send_video",
            client=client,
        )
        payload = message_to_dict(message)
        if warning:
            payload["upload_warning"] = warning
        return payload

    @mcp.tool()
    @with_flood_wait
    async def send_sticker(
        chat_ref: str,
        file_path: str,
        reply_to: int | None = None,
        schedule_at: str | None = None,
        silent: bool = False,
        allow_arbitrary_path: bool = False,
    ) -> dict:
        """Send a sticker file."""
        client = await get_client()
        entity = await resolve_entity(client, chat_ref)
        upload_path, warning = resolve_upload_path(
            file_path,
            allow_arbitrary_path=allow_arbitrary_path,
        )
        message = await upload_with_retry(
            lambda c: c.send_file(
                entity,
                upload_path,
                reply_to=reply_to,
                silent=silent,
                schedule=parse_datetime(schedule_at),
            ),
            file_path=upload_path,
            tool_name="send_sticker",
            client=client,
        )
        payload = message_to_dict(message)
        if warning:
            payload["upload_warning"] = warning
        return payload

    @mcp.tool()
    @with_flood_wait
    async def send_animation(
        chat_ref: str,
        file_path: str,
        caption: str | None = None,
        reply_to: int | None = None,
        schedule_at: str | None = None,
        silent: bool = False,
        allow_arbitrary_path: bool = False,
    ) -> dict:
        """Send a GIF/animation."""
        client = await get_client()
        entity = await resolve_entity(client, chat_ref)
        upload_path, warning = resolve_upload_path(
            file_path,
            allow_arbitrary_path=allow_arbitrary_path,
        )
        message = await upload_with_retry(
            lambda c: c.send_file(
                entity,
                upload_path,
                caption=caption,
                reply_to=reply_to,
                silent=silent,
                schedule=parse_datetime(schedule_at),
            ),
            file_path=upload_path,
            tool_name="send_animation",
            client=client,
        )
        payload = message_to_dict(message)
        if warning:
            payload["upload_warning"] = warning
        return payload

    @mcp.tool()
    @with_flood_wait
    async def get_media_info(chat_ref: str, message_id: int) -> dict:
        """Return media/file metadata for a Telegram message."""
        client = await get_client()
        entity, message = await _resolve_message(client, chat_ref, message_id)
        return {
            "chat_ref": peer_ref(entity),
            "message_id": message_id,
            "message": message_to_dict(message),
        }

    @mcp.tool()
    @with_flood_wait
    async def inspect_message_media(
        chat_ref: str,
        message_id: int,
        output_dir: str | None = None,
    ) -> dict:
        """Download a Telegram media message and return transcript/contact-sheet paths."""
        client = await get_client()
        entity, message = await _resolve_message(client, chat_ref, message_id)
        download_dir = ensure_download_dir(output_dir)
        file_path = await client.download_media(message, file=download_dir)
        if not file_path:
            raise RuntimeError(f"Message {message_id} did not produce a downloadable media file.")

        inspect_output_dir = download_dir if output_dir else None
        payload = await _inspect_media_file(
            _resolve_skill_script("inspect_bubble.sh"),
            file_path,
            inspect_output_dir,
        )
        if not isinstance(payload, dict):
            raise RuntimeError("inspect_bubble.sh returned JSON that is not an object")
        payload["chat_ref"] = peer_ref(entity)
        payload["message_id"] = message_id
        payload["downloaded_to"] = str(file_path)
        return payload

    @mcp.tool()
    @with_flood_wait
    async def set_profile_photo(
        file_path: str,
        confirm: bool = False,
        allow_arbitrary_path: bool = False,
    ) -> dict:
        """Upload a new account profile photo."""
        require_destructive("set_profile_photo", confirm)
        client = await get_client()
        upload_path, warning = resolve_upload_path(
            file_path,
            allow_arbitrary_path=allow_arbitrary_path,
        )
        async def _upload_profile_photo(c):
            uploaded = await c.upload_file(upload_path)
            return await c(functions.photos.UploadProfilePhotoRequest(file=uploaded))

        result = await upload_with_retry(
            _upload_profile_photo,
            file_path=upload_path,
            tool_name="set_profile_photo",
            client=client,
        )
        photo = result.photo if hasattr(result, "photo") else None
        payload = {
            "updated": True,
            "photo_id": getattr(photo, "id", None),
        }
        if warning:
            payload["upload_warning"] = warning
        return payload

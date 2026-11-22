from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
import functools
import logging
import math
import os
from pathlib import Path
import sys
import time
from typing import ParamSpec, TypeVar

from telethon import TelegramClient, errors, functions
from telethon.sessions import StringSession

from . import __version__
from .delivery import delivery_unknown_message
from .proxy import mtproxy_client_kwargs
from .session_store import MissingSessionError, load_session

P = ParamSpec("P")
R = TypeVar("R")

logger = logging.getLogger(__name__)

_client: TelegramClient | None = None
_client_lock = asyncio.Lock()
_generation_seq = 0
_last_client_verify_monotonic = 0.0
# Set when a client could not be proven dead. Opening another one would put
# two MTProto sessions on the same auth key, which Telegram revokes.
_client_unrecoverable = False
_last_reset_monotonic = 0.0
# id() of the client the last reset retired. The debounce covers that
# generation only, so a client connected after the reset is not suppressed.
_debounced_generation: int | None = None
_reset_history: list[float] = []

CONNECTION_VERIFY_INTERVAL_SECONDS = 30.0
CONNECTION_VERIFY_TIMEOUT_SECONDS = 5.0


def _float_env(name: str, default: float, *, allow_zero: bool = False) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if (
        not math.isfinite(value)
        or value < 0
        or (value == 0 and not allow_zero)
    ):
        return default
    return value


# Telethon's connect() awaits an InvokeWithLayer RPC with no timeout of its
# own. A half-dead MTProxy accepts the TCP connection and then never replies,
# and get_client holds _client_lock across connect, so one stuck connect used
# to wedge every later tool call. Bound it.
CONNECT_TIMEOUT_SECONDS = _float_env("CODEX_TELEGRAM_CONNECT_TIMEOUT", 25.0)
# disconnect() waits on the sender loops, which a reconnect storm can pin.
# Past this bound the old client is NOT known to be dead, so we refuse to
# build a replacement and restart the process instead.
DISCONNECT_TIMEOUT_SECONDS = _float_env("CODEX_TELEGRAM_DISCONNECT_TIMEOUT", 8.0)
# How long a forced reset waits to take _client_lock. Longer than connect +
# disconnect so a legitimate in-flight rebuild finishes before we escalate.
RESET_LOCK_TIMEOUT_SECONDS = _float_env("CODEX_TELEGRAM_RESET_LOCK_TIMEOUT", 45.0)
# Collapse the storm handler, the watchdog and a tool timeout firing together
# into a single reset.
RESET_DEBOUNCE_SECONDS = _float_env("CODEX_TELEGRAM_RESET_DEBOUNCE", 20.0, allow_zero=True)
# In-process resets that keep wedging mean the process itself is poisoned
# (offset corrected, but the sender loops or the proxy socket are not). Die
# and let a proxy respawn one clean backend.
RESET_ESCALATE_WINDOW_SECONDS = _float_env("CODEX_TELEGRAM_RESET_ESCALATE_WINDOW", 300.0)
MAX_RESETS_BEFORE_RESTART = 3

# Production wires this to os._exit so the backend flock dies with the
# process. Tests replace it. A handler that returns is treated as "the old
# client is still alive": get_client then refuses to build another one.
_escalation_handler: Callable[[str], None] | None = None


def set_escalation_handler(handler: Callable[[str], None] | None) -> None:
    global _escalation_handler
    _escalation_handler = handler


def _note(message: str) -> None:
    """stderr, not logging: the backend's log file is the process stderr."""
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"{timestamp} telegram pid={os.getpid()} {message}", file=sys.stderr, flush=True)


def _escalate(reason: str) -> None:
    global _client_unrecoverable
    _client_unrecoverable = True
    handler = _escalation_handler
    if handler is not None:
        handler(reason)
        return
    _note(
        f"FATAL {reason}; exiting so no second MTProto client can connect "
        "and a proxy can respawn one backend"
    )
    os._exit(86)
DIALOG_CACHE_TTL_SECONDS = 60.0
DIALOG_CACHE_ATTR = "_codex_telegram_dialog_cache"
DEFAULT_TAKEOUT_KWARGS = {
    "users": True,
    "chats": True,
    "megagroups": True,
    "channels": True,
}


# Once ``do_upload`` is entered, a drop or a stall means Telegram may already
# have accepted the file. A second attempt would deliver it twice, so the
# call fails with delivery unknown instead of retrying. The budget still
# bounds how long that single attempt may hang.
UPLOAD_ATTEMPTS = 1
UPLOAD_RETRY_BACKOFF_SECONDS = ()
UPLOAD_BASE_TIMEOUT_SECONDS = 20.0
UPLOAD_MIN_BYTES_PER_SECOND = 128 * 1024
UPLOAD_MAX_ATTEMPT_TIMEOUT_SECONDS = 40.0
UPLOAD_TOTAL_BUDGET_SECONDS = 70.0


class TelegramDeliveryUnknown(RuntimeError):
    """The RPC was issued and the reply was never observed.

    FastMCP stringifies tool exceptions, so the message is the whole signal
    the caller gets: delivery is unknown and the call was not repeated.
    """

    def __init__(self, *, tool_name: str, cause: BaseException):
        self.tool_name = tool_name
        super().__init__(f"{delivery_unknown_message(tool_name)} ({type(cause).__name__}: {cause})")


class TelegramUploadError(RuntimeError):
    """Raised when a media upload keeps failing on connection errors.

    Subclasses RuntimeError so FastMCP stringifies it like other tool errors;
    the message carries attempt/file details so MCP callers can tell a flaky
    transfer from a bad request.
    """

    def __init__(self, *, tool_name: str, file_path: Path, attempts: int, last_error: BaseException):
        self.tool_name = tool_name
        self.attempts = attempts
        super().__init__(
            f"{tool_name}: upload of {file_path.name} failed after {attempts} attempts "
            f"({type(last_error).__name__}: {last_error}). The Telegram connection "
            "dropped mid-transfer each time; check the network/proxy and retry."
        )


def _upload_cleanup_reserve() -> float:
    """How much of the upload budget is held back for disconnect.

    The process-level disconnect timeout can be larger than the upload
    budget. Reserving all of it would refuse every transfer, including one
    on a client that is already connected.
    """
    reserve = DISCONNECT_TIMEOUT_SECONDS
    if reserve <= 0:
        return 0.0
    return min(reserve, UPLOAD_TOTAL_BUDGET_SECONDS / 2)


def client_generation(client: TelegramClient) -> int:
    """Monotonic id assigned to a client object. ``id()`` can be reused."""
    global _generation_seq
    gen = getattr(client, "_codex_telegram_generation", None)
    if isinstance(gen, int):
        return gen
    _generation_seq += 1
    try:
        setattr(client, "_codex_telegram_generation", _generation_seq)
    except (AttributeError, TypeError):
        pass
    return _generation_seq


def _upload_timeout_seconds(file_path: Path) -> float:
    """Per-attempt stall timeout, scaled with size and capped for host MCP waits."""
    size = file_path.stat().st_size
    scaled = UPLOAD_BASE_TIMEOUT_SECONDS + size / UPLOAD_MIN_BYTES_PER_SECOND
    return min(scaled, UPLOAD_MAX_ATTEMPT_TIMEOUT_SECONDS)


async def upload_with_retry(
    do_upload: Callable[[TelegramClient], Awaitable[R]],
    *,
    file_path: Path,
    tool_name: str,
    client: TelegramClient | None = None,
) -> R:
    """Run ``do_upload(client)`` once. A drop after it starts is not retried.

    A connection drop or a stall after ``do_upload`` starts is delivery
    unknown: Telegram may already have the file, so there is no second
    attempt. Non-connection errors (FloodWait, bad arguments, ...) propagate
    immediately. The attempt is bounded by ``UPLOAD_TOTAL_BUDGET_SECONDS``.
    """
    per_attempt = _upload_timeout_seconds(file_path)
    deadline = time.monotonic() + UPLOAD_TOTAL_BUDGET_SECONDS
    reserve = _upload_cleanup_reserve()
    last_error: BaseException | None = None
    attempts_used = 0
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if client is None:
            # Leave the reserve for get_client's own disconnect if this
            # wait cancels a connect. wait_for does not return until that
            # cleanup finishes, so the timeout has to end before the budget.
            if reserve > 0 and remaining <= reserve:
                last_error = TimeoutError("upload budget exhausted")
                break
            acquire_timeout = remaining - reserve if reserve > 0 else remaining
            try:
                client = await asyncio.wait_for(get_client(), timeout=acquire_timeout)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                last_error = exc
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_error = TimeoutError("upload budget exhausted during reconnect")
                break
        # A failed attempt then waits out disconnect. Starting a transfer
        # that leaves less than the reserve pushes the error past the budget.
        if reserve > 0 and remaining <= reserve:
            if last_error is None:
                last_error = TimeoutError("upload budget exhausted")
            break
        transfer = min(per_attempt, remaining - reserve)
        attempts_used = attempt
        try:
            return await asyncio.wait_for(do_upload(client), timeout=transfer)
        except (ConnectionError, TimeoutError) as exc:
            failed = client
            client = None
            # Holds the client lock until disconnect finishes, so a concurrent
            # get_client cannot dial a second session on the same auth key.
            # The send itself is not repeated.
            await _discard_client(failed)
            raise TelegramDeliveryUnknown(tool_name=tool_name, cause=exc) from exc
    if last_error is None:
        last_error = TimeoutError("upload budget exhausted")
    raise TelegramUploadError(
        tool_name=tool_name,
        file_path=file_path,
        attempts=attempts_used,
        last_error=last_error,
    ) from last_error


class TelegramFloodWaitError(RuntimeError):
    """RuntimeError that preserves Telegram FloodWait metadata.

    Subclasses RuntimeError so existing in-process callers can keep working,
    while carrying `seconds`, `tool_name`, and `attempts` as both attributes
    (for `except TelegramFloodWaitError as exc:` consumers) and a parseable
    suffix in the error message — FastMCP stringifies tool exceptions before
    sending them to the client, so structured attrs alone are not visible
    to MCP callers.
    """

    def __init__(self, *, seconds: int, tool_name: str | None = None, attempts: int = 1):
        self.seconds = seconds
        self.tool_name = tool_name
        self.attempts = attempts
        target = tool_name or "this request"
        super().__init__(
            f"Telegram rate limited {target} for {seconds} seconds. "
            "Wait for the flood window to expire and retry. "
            f"[flood_wait_seconds={seconds} tool_name={tool_name or 'unknown'} "
            f"attempts={attempts}]"
        )


async def list_all_dialogs(client) -> list:
    """Return every dialog for the authenticated account.

    Cached on the client instance for `DIALOG_CACHE_TTL_SECONDS` so repeated
    entity resolution and unread scans don't refetch the full list on every
    call. Replaces the previous `get_dialogs(limit=200)` pattern that
    silently truncated power-user accounts.
    """
    cached = getattr(client, DIALOG_CACHE_ATTR, None)
    if cached is not None:
        fetched_at, dialogs = cached
        if time.monotonic() - fetched_at < DIALOG_CACHE_TTL_SECONDS:
            return dialogs

    dialogs = [dialog async for dialog in client.iter_dialogs()]
    setattr(client, DIALOG_CACHE_ATTR, (time.monotonic(), dialogs))
    return dialogs


def invalidate_dialog_cache(client) -> None:
    if hasattr(client, DIALOG_CACHE_ATTR):
        delattr(client, DIALOG_CACHE_ATTR)


def _build_client(session_string: str, api_id: int, api_hash: str) -> TelegramClient:
    return TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
        device_model="Codex Telegram Plugin",
        system_version="Codex",
        app_version=__version__,
        lang_code="en",
        system_lang_code="en",
        **mtproxy_client_kwargs(),
    )


def _zero_time_offset(client: TelegramClient) -> None:
    """Drop a skewed MTProto time offset before the client goes away.

    ``MTProtoState.reset()`` — which Telethon calls on every auto-reconnect —
    clears the session id and the ignore counter but deliberately keeps
    ``time_offset``. Once that offset is wrong, every server reply looks "too
    new" (or too old), ten of those raise SecurityError, and the recv loop
    logs the error and continues. Pending RPCs are never completed and the
    client still reports itself connected. Zero the offset here so a reconnect
    that races the shutdown can't keep discarding replies; the replacement
    client starts at offset 0 either way.
    """
    sender = getattr(client, "_sender", None)
    state = getattr(sender, "_state", None)
    if state is None or not hasattr(state, "time_offset"):
        return
    offset = state.time_offset
    state.time_offset = 0
    if hasattr(state, "_ignore_count"):
        state._ignore_count = 0
    if isinstance(offset, (int, float)) and offset != 0:
        _note(f"cleared telethon time_offset {offset} before reconnect")


async def _shutdown_client(client: TelegramClient, *, context: str) -> None:
    """Disconnect ``client`` within a hard bound.

    A client we cannot prove dead must never be followed by a new
    ``client.connect()``: that is two MTProto clients on one session.
    """
    _zero_time_offset(client)
    try:
        disconnect_result = client.disconnect()
        if disconnect_result is not None:
            await asyncio.wait_for(disconnect_result, DISCONNECT_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, TimeoutError):
        _escalate(
            f"telegram client did not disconnect within "
            f"{DISCONNECT_TIMEOUT_SECONDS:.0f}s ({context})"
        )
        raise ConnectionError(
            f"Telegram client shutdown timed out ({context}); the backend is restarting."
        )
    except Exception:
        # A connection that is already broken raises while it closes. That
        # still means the client has finished disconnecting.
        logger.debug("disconnect raised during %s", context, exc_info=True)


async def _await_shutdown(client: TelegramClient, *, context: str) -> None:
    """Disconnect ``client`` even when this task is already being cancelled.

    The next ``await`` in a cancelled task raises ``CancelledError`` immediately,
    which would skip teardown and return with the sender still connected. Drain
    the cancellation, close the sender, then restore it so the caller still
    unwinds.
    """
    task = asyncio.current_task()
    job = asyncio.create_task(_shutdown_client(client, context=context))
    drained = False
    try:
        while not job.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
                    drained = True
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                # A cancel that arrives during shutdown must not drop the
                # caller's lock. Keep waiting; the disconnect has its own bound.
                drained = True
                continue
        job.result()
    finally:
        if drained and task is not None:
            task.cancel()


async def _discard_locked(expected: TelegramClient | None = None) -> None:
    """Disconnect the shared client. Caller holds ``_client_lock``.

    The lock is held across the disconnect on purpose: ``get_client`` takes
    the same lock before ``connect()``, so a replacement cannot start while
    this sender is still up. ``expected`` lets a stale caller (an upload that
    failed, a verify that lost the race) no-op once a newer client is installed.
    """
    global _client, _last_client_verify_monotonic

    client = _client
    if client is None:
        return
    if expected is not None and client is not expected:
        return
    # Stop handing the doomed client out before awaiting anything.
    _client = None
    _last_client_verify_monotonic = 0.0
    await _await_shutdown(client, context="discard")


async def _discard_client(expected: TelegramClient | None = None) -> None:
    async with _client_lock:
        await _discard_locked(expected)


async def force_reset_client(*, reason: str) -> bool:
    """Tear the shared client down so the next call connects a fresh one.

    The fresh client has a new ``MTProtoState`` with ``time_offset == 0``,
    which is the only thing that ends an ignore-storm: Telethon's own
    reconnect keeps the bad offset. Returns False when this same client was
    just reset (or nothing is connected) and the call is inside the debounce
    window. A client connected after that reset is not covered by it.

    Takes ``_client_lock`` so a reset can never overlap a rebuild. Two live
    clients on one session is the failure this whole process exists to avoid.
    """
    global _last_reset_monotonic, _debounced_generation

    if _client_unrecoverable:
        _escalate(f"refusing a second MTProto client after a stuck shutdown ({reason})")
        raise ConnectionError("Telegram client could not be shut down; the backend is restarting.")

    now = time.monotonic()
    current = _client
    current_gen = client_generation(current) if current is not None else None
    if now - _last_reset_monotonic < RESET_DEBOUNCE_SECONDS and (
        current is None or current_gen == _debounced_generation
    ):
        return False

    _note(f"resetting telegram client: {reason}")
    try:
        await asyncio.wait_for(_client_lock.acquire(), RESET_LOCK_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, TimeoutError):
        _escalate(
            f"client lock not released within {RESET_LOCK_TIMEOUT_SECONDS:.0f}s "
            f"during forced reset ({reason})"
        )
        raise ConnectionError("Telegram client lock is stuck; the backend is restarting.")
    try:
        if current is None:
            # Nothing was published when the reset was requested. The connect
            # that held the lock has since published its client; that client
            # is what this reset is waiting out.
            await _discard_locked()
        else:
            # Only the client we decided to reset. A replacement published
            # while this waited on the lock is a different object and stays up.
            await _discard_locked(current)
    finally:
        _client_lock.release()

    # Stamp only after the client is actually retired. A reset cancelled
    # while it waited on the lock must not suppress the next one.
    finished = time.monotonic()
    _last_reset_monotonic = finished
    if current_gen is not None:
        _debounced_generation = current_gen
    cutoff = finished - RESET_ESCALATE_WINDOW_SECONDS
    _reset_history.append(finished)
    _reset_history[:] = [stamp for stamp in _reset_history if stamp >= cutoff]
    if len(_reset_history) > MAX_RESETS_BEFORE_RESTART:
        _escalate(
            f"{reason}; {len(_reset_history)} client resets within "
            f"{RESET_ESCALATE_WINDOW_SECONDS:.0f}s did not stick"
        )
        raise ConnectionError("Telegram client kept wedging; the backend is restarting.")
    return True


def current_client() -> TelegramClient | None:
    """The live shared client, or None. Never connects (the watchdog must not
    open a session just to see if an idle backend is healthy)."""
    return _client


async def _verify_client_connection(client: TelegramClient, *, holding_lock: bool = False) -> None:
    global _last_client_verify_monotonic

    if not client.is_connected():
        raise ConnectionError("Telegram client is disconnected.")

    now = time.monotonic()
    if now - _last_client_verify_monotonic < CONNECTION_VERIFY_INTERVAL_SECONDS:
        return

    try:
        await asyncio.wait_for(
            client(functions.help.GetNearestDcRequest()),
            timeout=CONNECTION_VERIFY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if holding_lock:
            await _discard_locked(client)
        else:
            await _discard_client(client)
        raise ConnectionError("Telegram connection is stale.") from exc

    _last_client_verify_monotonic = now


async def get_client() -> TelegramClient:
    global _client, _last_client_verify_monotonic

    if _client_unrecoverable:
        _escalate("refusing to open a second MTProto client after a stuck shutdown")
        raise ConnectionError(
            "Telegram client could not be shut down; the backend is restarting."
        )

    # Capture a local reference: the verify call awaits, and a concurrent
    # caller may discard the global `_client` in the meantime.
    client = _client
    if client:
        try:
            await _verify_client_connection(client, holding_lock=False)
            return client
        except ConnectionError:
            pass

    async with _client_lock:
        client = _client
        if client:
            try:
                await _verify_client_connection(client, holding_lock=True)
                return client
            except ConnectionError:
                pass

        try:
            record = load_session()
        except MissingSessionError as exc:
            # Preserve the storage-layer detail: "encrypted session found but
            # no master key" needs a different remediation than "no session".
            raise RuntimeError(f"Telegram is not authenticated. {exc}") from exc

        client = _build_client(record.session_string, record.api_id, record.api_hash)
        try:
            await asyncio.wait_for(_connect_and_check(client), CONNECT_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            # The cancelled connect may have left a half-started sender.
            # Prove it is dead before anyone is allowed to build another.
            await _await_shutdown(client, context="connect timeout")
            raise ConnectionError(
                f"Telegram connect timed out after {CONNECT_TIMEOUT_SECONDS:.0f}s "
                "(proxy unreachable or not delivering replies). The connection "
                "was torn down; retry shortly."
            ) from exc
        except asyncio.CancelledError:
            # The tool deadline (or the host dropping the request) can cancel
            # get_client while Telethon is already past TCP connect, waiting
            # on the startup RPC. CancelledError is a BaseException, so the
            # Exception handler below does not run. The client is not in
            # `_client` yet; leaving it connected lets the next call dial a
            # second session on the same auth key.
            if _client is not client:
                await _await_shutdown(client, context="connect cancelled")
            raise
        except Exception:
            if _client is not client:
                await _await_shutdown(client, context="connect failed")
            raise

        _client = client
        client_generation(client)
        _last_client_verify_monotonic = time.monotonic()
        return _client


async def _connect_and_check(client: TelegramClient) -> None:
    await client.connect()
    if not await client.is_user_authorized():
        # get_client shuts the unpublished client down once, on the way out.
        # Disconnecting here as well hides the login error if that second
        # shutdown times out and escalates.
        raise RuntimeError(
            "Stored Telegram session is no longer authorized. "
            "Re-run `python -m codex_telegram login`."
        )


async def disconnect_client() -> None:
    await _discard_client()


@asynccontextmanager
async def get_history_client(
    *,
    use_takeout: bool = False,
    takeout_kwargs: dict | None = None,
):
    client = await get_client()
    if not use_takeout:
        yield client
        return

    options = dict(DEFAULT_TAKEOUT_KWARGS)
    if takeout_kwargs:
        options.update(takeout_kwargs)

    # Telethon raises if a takeout init request is sent while a previous
    # takeout session (opened with finalize=False) is still active. Calling
    # takeout() without scope kwargs reuses the active session instead.
    # Telegram allows one takeout per session, so the active session's scope
    # wins; use end_takeout_session first to start over with a new scope.
    if client.session.takeout_id is not None:
        async with client.takeout(finalize=False) as takeout:
            yield takeout
        return

    try:
        async with client.takeout(finalize=False, **options) as takeout:
            yield takeout
    except errors.TakeoutInitDelayError as exc:
        raise RuntimeError(
            "Telegram takeout could not start yet. "
            f"Wait {exc.seconds} seconds and retry."
        ) from exc


def with_flood_wait(
    func: Callable[P, Awaitable[R]], *, max_sleep_seconds: int = 60
) -> Callable[P, Awaitable[R]]:
    """Convert propagated FloodWaitErrors into structured TelegramFloodWaitErrors.

    Telethon itself auto-sleeps floods up to its `flood_sleep_threshold`
    (60s by default), so with default settings every error reaching this
    wrapper exceeds `max_sleep_seconds` and is re-raised immediately as a
    structured error. The sleep-and-retry branch only engages when the
    client is configured with a lower threshold.
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        attempts = 0
        while True:
            try:
                return await func(*args, **kwargs)
            except errors.FloodWaitError as exc:
                attempts += 1
                if attempts > 1 or exc.seconds > max_sleep_seconds:
                    raise TelegramFloodWaitError(
                        seconds=exc.seconds,
                        tool_name=func.__name__,
                        attempts=attempts,
                    ) from exc
                await asyncio.sleep(exc.seconds)

    return wrapper

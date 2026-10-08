"""Push a thread to a durable machine and continue it there.

Sending side (:func:`push_thread`), in order:

1. **Check** the thread can leave: a Codex or Claude Code thread, not the
   voice dispatcher or the thread on a live call, no running or queued turn,
   no pending approval. A running turn is never interrupted: the push is
   refused with a clear message instead (callers may wait and retry).
2. **Check** the destination: a durable target that is online, runs a
   runtime that accepts pushes, has the thread's backend, and mirrors the
   thread exchange; and the thread's folder is inside a synced root here.
3. **Pause** the thread here: its push record goes to ``pushing``, which
   blocks new turns from every Openbase surface (see ``thread_moves``).
4. **Transfer**: unload the thread from the local Codex app-server, write
   its snapshot to the exchange folder, and flush Openbase Sync for the
   project folder and the exchange (the barrier returns once the hub has
   both), then ask the target to take it (:func:`accept_push` there).
5. **Mark** the local copy ``moved`` (read-only, linking to the moved
   copy), or roll back to usable when the target refused before taking it.

Receiving side (:func:`accept_push`): verify the folder and backend, wait
for the snapshot, import exactly that snapshot through the device-sync
importer (fast-forward only), clear any stale moved marker, and optionally
start a turn with the user's follow-up message.

Every step is idempotent per operation id. A push whose outcome is unknown
(the target may have taken the thread but the answer was lost) stays
``uncertain`` — still blocked here — until a retry with the same operation
id learns the answer, so a thread can never continue in two places.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from openbase_coder_cli.services import durable_targets as targets_mod
from openbase_coder_cli.services.durable_targets import DurableTarget, TargetError
from openbase_coder_cli.thread_sync import thread_handoff as handoff
from openbase_coder_cli.thread_sync import thread_moves as moves
from openbase_coder_cli.thread_sync.thread_handoff import HandoffError, HandoffSnapshot

logger = logging.getLogger(__name__)

FLUSH_TIMEOUT_SECONDS = 30.0
SETTLE_TIMEOUT_SECONDS = 5.0
SNAPSHOT_WAIT_SECONDS = 20.0
SNAPSHOT_POLL_SECONDS = 0.5
EXPORT_ATTEMPTS = 4
EXPORT_RETRY_SECONDS = 1.5
MAX_MESSAGE_CHARS = 100_000

_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


class PushError(RuntimeError):
    """A push stopped; ``code`` is stable and ``message`` is for people.

    ``safe_to_retry`` tells clients whether retrying can succeed without a
    change on their side (for example after a turn finishes or a peer comes
    back online).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        safe_to_retry: bool = False,
        http_status: int = 409,
    ):
        super().__init__(message)
        self.code = code
        self.safe_to_retry = safe_to_retry
        self.http_status = http_status

    def to_json(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "code": self.code,
            "safe_to_retry": self.safe_to_retry,
        }


def _from_handoff(exc: HandoffError) -> PushError:
    return PushError(exc.code, str(exc), safe_to_retry=exc.retryable)


def _from_target(exc: TargetError) -> PushError:
    return PushError(exc.code, str(exc), safe_to_retry=exc.retryable)


def _thread_lock(thread_id: str) -> threading.Lock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(thread_id, threading.Lock())


# --- shared helpers ----------------------------------------------------------


def backend_manager(manager: Any, backend: str) -> Any | None:
    """The per-backend session manager for ``backend`` (None if not set up)."""
    resolver = getattr(manager, "manager_for_backend", None)
    if callable(resolver):
        return resolver(backend)
    own = getattr(manager, "_execution_backend", None)
    return manager if own == backend else None


def configured_backends() -> list[str]:
    from openbase_coder_cli.backend_config import configured_execution_backends

    return list(configured_execution_backends())


def _thread_backend(manager: Any, thread: Any) -> str | None:
    """The execution backend a thread runs on (codex or claude_code).

    A thread records the identity that ran it (openbase_cloud is Claude Code
    with Openbase Cloud credentials); handoff works per execution backend.
    """
    from openbase_coder_cli.backend_config import (
        execution_backend_for_configured_backend,
    )

    backend = thread.backend or getattr(manager, "_execution_backend", None)
    return execution_backend_for_configured_backend(backend) if backend else None


async def busy_reason(manager: Any, thread: Any) -> str | None:
    """Why the thread cannot leave right now (None when it can)."""
    from openbase_coder_cli.livekit_voice_route import get_livekit_voice_route_state

    route = get_livekit_voice_route_state()
    if thread.session_id == route.dispatcher_thread_id:
        return "The voice dispatcher thread stays on this computer."
    if thread.session_id == route.active_target_thread_id:
        return "Leave this thread's voice call before pushing it."
    status = getattr(thread.status, "value", thread.status)
    if thread.current_run is not None or status in {"running", "waiting"}:
        return "A turn is running. Wait for it to finish (or stop it), then push."
    if thread.queued_turns:
        return "Clear the queued prompts before pushing this thread."
    try:
        approvals = await manager.list_approval_requests()
    except Exception:  # noqa: BLE001 - an approval probe failure is not a block
        approvals = []
    for item in approvals:
        if (item.get("thread_id") or item.get("threadId")) == thread.session_id:
            return "Answer the pending approval before pushing this thread."
    return None


def _synced_root(directory: str) -> Path | None:
    from openbase_coder_cli.agent_remote import read_sync_facts, synced_root_for

    facts = read_sync_facts()
    if not facts.configured:
        return None
    return synced_root_for(Path(directory), facts.roots, Path.home())


def _exchange_synced() -> bool:
    from openbase_coder_cli.sync_daemon import path_is_synced

    try:
        return path_is_synced(handoff.exchange_dir())
    except OSError:
        return False


def _barrier(kind: str, path: str, timeout_seconds: float) -> dict[str, Any]:
    from openbase_coder_cli.sync_daemon import SyncDaemonClient

    client = SyncDaemonClient(timeout=timeout_seconds + 5.0)
    return client.barrier(kind, path=path, timeout_ms=int(timeout_seconds * 1000))


def flush_to_peer(paths: list[str], target_name: str) -> None:
    """Return once the paired peer holds this computer's changes under ``paths``."""
    from openbase_coder_cli.sync_daemon import SyncDaemonError

    for path in paths:
        try:
            result = _barrier("flush", path, FLUSH_TIMEOUT_SECONDS)
        except SyncDaemonError as exc:
            if isinstance(exc.__cause__, (TimeoutError, socket.timeout)):
                # The daemon is running but did not answer in time (busy
                # catching up, e.g. a large rescan): not the same as "off".
                raise PushError(
                    "sync_lagging",
                    f"Openbase Sync did not finish handing the folder to "
                    f"{target_name} in time; it is still catching up. "
                    "Try again in a moment.",
                    safe_to_retry=True,
                ) from exc
            raise PushError(
                "sync_unavailable",
                "Openbase Sync is not running on this computer, so the "
                "thread's files cannot be handed over.",
                safe_to_retry=True,
            ) from exc
        outcome = result.get("result")
        if outcome == "ok":
            continue
        if outcome == "offline":
            raise PushError(
                "target_offline",
                f"Openbase Sync cannot reach {target_name}. "
                "Make sure it is on and connected, then try again.",
                safe_to_retry=True,
            )
        lag = result.get("lag") or 0
        raise PushError(
            "sync_lagging",
            f"Files are still syncing to {target_name}"
            + (f" ({lag} changes pending)" if lag else "")
            + ". Try again in a moment.",
            safe_to_retry=True,
        )


async def _unload_codex_thread(manager: Any, thread_id: str) -> None:
    """Let the local Codex app-server close the thread's rollout file.

    A loaded thread keeps its rollout open for writing, and the exporter
    rightly refuses to snapshot a file that may still change. Unsubscribing
    this server's connection unloads an idle thread; another client that
    has it open (a ``codex`` TUI) keeps it loaded, and the export then
    reports the thread as busy.
    """
    client = getattr(manager, "_client", None)
    request = getattr(client, "request", None)
    if not callable(request):
        return
    try:
        await request("thread/unsubscribe", {"threadId": thread_id})
    except Exception as exc:  # noqa: BLE001 - best effort; export decides
        logger.info(
            "thread_push unsubscribe_failed thread_id=%s error=%s", thread_id, exc
        )


async def _export_with_retries(backend: str, entity_id: str) -> HandoffSnapshot:
    last: HandoffError | None = None
    for attempt in range(EXPORT_ATTEMPTS):
        try:
            return await asyncio.to_thread(handoff.export_one, backend, entity_id)
        except HandoffError as exc:
            if exc.code != "thread_busy" or attempt == EXPORT_ATTEMPTS - 1:
                raise
            last = exc
            await asyncio.sleep(EXPORT_RETRY_SECONDS)
    assert last is not None  # pragma: no cover - loop always returns or raises
    raise last


def _target_reason(
    info: dict[str, Any], backend: str, target: DurableTarget
) -> str | None:
    if backend not in (info.get("backends") or []):
        label = "Codex" if backend == handoff.CODEX else "Claude Code"
        return f"{label} is not set up on {target.name}."
    if not info.get("exchange_synced"):
        return f"Thread sync is not enabled on {target.name}."
    return None


# --- sending side ----------------------------------------------------------


async def push_options(manager: Any, thread_id: str) -> dict[str, Any]:
    """Where this thread can go, and why it cannot go right now."""
    thread = await manager.get_thread_state(thread_id)
    if thread is None:
        raise PushError("not_found", "Thread not found.", http_status=404)
    backend = _thread_backend(manager, thread)
    move = moves.get_move(thread_id)
    reason = None
    if move and move.get("state") in moves.BLOCKING_STATES:
        reason = moves.blocking_move_message(move)
    elif backend not in handoff.SUPPORTED_BACKENDS:
        reason = "Only Codex and Claude Code threads can be pushed."
    elif not thread.directory or _synced_root(thread.directory) is None:
        reason = "This thread's folder is not in a synced folder."
    elif not _exchange_synced():
        reason = "Thread sync is not enabled on this computer."
    else:
        reason = await busy_reason(manager, thread)

    def probe(target: DurableTarget) -> dict[str, Any]:
        entry = {**target.to_json(), "online": False, "reason": None}
        try:
            info = targets_mod.target_info(target)
        except TargetError as exc:
            entry["reason"] = str(exc)
            return entry
        entry["online"] = True
        entry["name"] = (
            target.name
            if target.name != target.host
            else (info.get("device_name") or target.name)
        )
        entry["reason"] = _target_reason(info, backend or "", target)
        return entry

    targets = await asyncio.to_thread(targets_mod.durable_targets)
    entries = await asyncio.gather(
        *(asyncio.to_thread(probe, target) for target in targets)
    )
    return {
        "thread_id": thread_id,
        "this_is_durable": await asyncio.to_thread(
            targets_mod.this_computer_is_durable
        ),
        "blocked_reason": reason,
        "moved_to": moves.moved_to_payload(thread_id),
        "targets": list(entries),
    }


async def push_thread(
    manager: Any,
    thread_id: str,
    *,
    to: str | None = None,
    message: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Move ``thread_id`` to a durable machine (see module docstring)."""
    if message is not None and len(message) > MAX_MESSAGE_CHARS:
        raise PushError("message_too_long", "The follow-up message is too long.")
    lock = _thread_lock(thread_id)
    if not lock.acquire(blocking=False):
        raise PushError(
            "push_in_progress",
            "This thread is already being pushed.",
            safe_to_retry=True,
        )
    try:
        return await _push_locked(
            manager,
            thread_id,
            to=to,
            message=(message or "").strip() or None,
            request_id=request_id,
        )
    finally:
        lock.release()


def _moved_result(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": moves.STATE_MOVED,
        "thread_id": record["thread_id"],
        "operation_id": record.get("operation_id"),
        "moved_to": moves.moved_to_payload(record["thread_id"]),
        "turn_started": bool(record.get("turn_started")),
        "turn_error": record.get("turn_error"),
    }


async def _push_locked(
    manager: Any,
    thread_id: str,
    *,
    to: str | None,
    message: str | None,
    request_id: str | None,
) -> dict[str, Any]:
    existing = moves.get_move(thread_id)
    state = (existing or {}).get("state")
    if state == moves.STATE_MOVED:
        return _moved_existing(existing, to)
    if state in {moves.STATE_PUSHING, moves.STATE_UNCERTAIN}:
        return await _resume_push(dict(existing), to)

    thread = await manager.get_thread_state(thread_id)
    if thread is None:
        raise PushError("not_found", "Thread not found.", http_status=404)
    backend = _thread_backend(manager, thread)
    if backend not in handoff.SUPPORTED_BACKENDS:
        raise PushError(
            "unsupported_backend", "Only Codex and Claude Code threads can be pushed."
        )
    if reason := await busy_reason(manager, thread):
        raise PushError("thread_busy", reason, safe_to_retry=True)
    if not thread.directory:
        raise PushError("folder_not_synced", "This thread has no working folder.")
    root = await asyncio.to_thread(_synced_root, thread.directory)
    if root is None:
        raise PushError(
            "folder_not_synced",
            f"{thread.directory} is not in a synced folder, so the durable "
            "machine would not have its files. Add it under Settings → Sync.",
        )
    if not await asyncio.to_thread(_exchange_synced):
        raise PushError(
            "exchange_not_synced",
            "Thread sync is not enabled on this computer (the thread exchange "
            "folder is not synced).",
        )
    targets = await asyncio.to_thread(targets_mod.durable_targets)
    if not targets:
        if await asyncio.to_thread(targets_mod.this_computer_is_durable):
            raise PushError(
                "no_durable_target",
                "This computer is your durable machine; threads here already "
                "keep running.",
            )
        raise PushError(
            "no_durable_target",
            "No durable machine is set up. Pair this computer with an always-on "
            "computer under Settings → Sync first.",
        )
    target = targets_mod.find_target(to, targets)
    if target is None:
        raise PushError(
            "unknown_target",
            f"{to} is not one of your durable machines "
            f"({', '.join(t.name for t in targets)}).",
        )
    try:
        info = await asyncio.to_thread(targets_mod.target_info, target)
    except TargetError as exc:
        raise _from_target(exc) from exc
    if reason := _target_reason(info, backend, target):
        raise PushError("target_not_ready", reason)

    try:
        entity_id = handoff.entity_id_for(
            backend, thread.session_id, thread.backend_session_id
        )
    except HandoffError as exc:
        raise _from_handoff(exc) from exc
    operation_id = _operation_id(request_id)
    record = moves.set_move(
        thread_id,
        state=moves.STATE_PUSHING,
        operation_id=operation_id,
        backend=backend,
        entity_id=entity_id,
        directory=targets_mod.home_relative(thread.directory),
        sync_root=str(root),
        target=_target_record(target, info),
        message=message,
        started_at=moves.now_iso(),
        error=None,
        error_code=None,
    )
    # A turn may have started between the check above and the record that
    # now blocks new ones; re-check before anything leaves this computer.
    fresh = await manager.get_thread_state(thread_id)
    if fresh is None or (reason := await busy_reason(manager, fresh)):
        moves.clear_move(thread_id)
        raise PushError(
            "thread_busy", reason or "Thread not found.", safe_to_retry=True
        )

    try:
        if backend == handoff.CODEX:
            await _unload_codex_thread(
                backend_manager(manager, backend) or manager, thread.session_id
            )
        snapshot = await _export_with_retries(backend, entity_id)
        record = moves.set_move(
            thread_id,
            fingerprint=snapshot.fingerprint,
            source_device_id=snapshot.source_device_id,
        )
        await asyncio.to_thread(
            flush_to_peer,
            [thread.directory, str(handoff.exchange_dir())],
            target.name,
        )
    except HandoffError as exc:
        _fail(thread_id, exc.code, str(exc))
        raise _from_handoff(exc) from exc
    except PushError as exc:
        _fail(thread_id, exc.code, str(exc))
        raise
    return await _deliver(record, target)


def _operation_id(request_id: str | None) -> str:
    if not request_id:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(str(request_id)))
    except ValueError as exc:
        raise PushError(
            "invalid_request", "request_id must be a UUID.", http_status=400
        ) from exc


def _target_record(target: DurableTarget, info: dict[str, Any]) -> dict[str, Any]:
    record = target.to_json()
    record["base_url"] = target.base_url
    if record["name"] == record["host"] and info.get("device_name"):
        record["name"] = str(info["device_name"])
    return record


def _target_from_record(record: dict[str, Any]) -> DurableTarget:
    stored = record.get("target") or {}
    return DurableTarget(
        key=str(stored.get("key") or stored.get("host") or ""),
        name=str(stored.get("name") or stored.get("host") or "the durable machine"),
        host=str(stored.get("host") or ""),
        base_url=str(stored.get("base_url") or ""),
        kind=str(stored.get("kind") or targets_mod.KIND_SYNC_HUB),
    )


def _fail(thread_id: str, code: str, message: str) -> None:
    """The push stopped before the target took the thread: usable here again."""
    moves.set_move(
        thread_id,
        state=moves.STATE_FAILED,
        error=message,
        error_code=code,
        failed_at=moves.now_iso(),
    )


def _moved_existing(existing: dict[str, Any], to: str | None) -> dict[str, Any]:
    target = _target_from_record(existing)
    if to and to.strip().lower() not in {
        target.key.lower(),
        target.name.lower(),
        target.host.lower(),
    }:
        raise PushError(
            "already_moved",
            f"This thread already moved to {target.name}.",
        )
    return _moved_result(existing)


async def _resume_push(existing: dict[str, Any], to: str | None) -> dict[str, Any]:
    """Finish a push that was interrupted after the thread was paused."""
    target = _target_from_record(existing)
    if to and to.strip().lower() not in {
        target.key.lower(),
        target.name.lower(),
        target.host.lower(),
    }:
        raise PushError(
            "push_in_progress",
            f"A push of this thread to {target.name} has not finished. "
            f"Retry it with --to {target.key}, or cancel it first.",
        )
    if not existing.get("fingerprint"):
        # Paused but nothing left this computer: start over from scratch.
        moves.clear_move(existing["thread_id"])
        raise PushError(
            "push_interrupted",
            "The previous push stopped before transferring anything. Push again.",
            safe_to_retry=True,
        )
    # The earlier attempt may have stopped before its sync barrier returned;
    # flushing again is cheap when everything already arrived.
    directory = str(_expand_home_relative(str(existing.get("directory") or "~")))
    # A failure here leaves the thread paused: whether the target already
    # took it is still unknown, so it must not become usable here yet.
    await asyncio.to_thread(
        flush_to_peer, [directory, str(handoff.exchange_dir())], target.name
    )
    return await _deliver(existing, target)


def _arrival_payload(record: dict[str, Any]) -> dict[str, Any]:
    from openbase_coder_cli.thread_sync.thread_exchange import read_device_identity

    identity = read_device_identity()
    return {
        "protocol": targets_mod.PUSH_PROTOCOL_VERSION,
        "operation_id": record["operation_id"],
        "thread_id": record["thread_id"],
        "backend": record["backend"],
        "entity_id": record["entity_id"],
        "fingerprint": record["fingerprint"],
        "source_device_id": record["source_device_id"],
        "source_device_name": identity.device_name if identity else None,
        "directory": record["directory"],
        "message": record.get("message"),
    }


async def _deliver(record: dict[str, Any], target: DurableTarget) -> dict[str, Any]:
    thread_id = record["thread_id"]
    try:
        result = await asyncio.to_thread(
            targets_mod.send_arrival, target, _arrival_payload(record)
        )
    except TargetError as exc:
        if exc.reached:
            moves.set_move(
                thread_id,
                state=moves.STATE_UNCERTAIN,
                error=str(exc),
                error_code=exc.code,
            )
        else:
            _fail(thread_id, exc.code, str(exc))
        raise _from_target(exc) from exc
    record = moves.set_move(
        thread_id,
        state=moves.STATE_MOVED,
        target_thread_id=str(result["thread_id"]),
        moved_at=moves.now_iso(),
        turn_started=bool(result.get("turn_started")),
        turn_error=result.get("turn_error"),
        error=None,
        error_code=None,
    )
    logger.info(
        "thread_push moved thread_id=%s target=%s target_thread_id=%s",
        thread_id,
        target.name,
        record["target_thread_id"],
    )
    return _moved_result(record)


async def cancel_push(thread_id: str, *, force: bool = False) -> dict[str, Any]:
    """Make a thread usable here again after a push that did not finish.

    The target is asked first: a push it already took is completed instead
    (the thread lives there now). ``force`` releases the thread without that
    confirmation — only for a target that is gone for good.
    """
    existing = moves.get_move(thread_id)
    state = (existing or {}).get("state")
    if state == moves.STATE_MOVED:
        raise PushError(
            "already_moved",
            "This thread already moved. Use `openbase-coder threads push "
            "--release` to make this copy usable again.",
        )
    if state not in {moves.STATE_PUSHING, moves.STATE_UNCERTAIN}:
        moves.clear_move(thread_id)
        return {"state": "local", "thread_id": thread_id}
    if existing.get("fingerprint") and not force:
        target = _target_from_record(existing)
        try:
            return await asyncio.to_thread(_confirm_not_arrived, existing, target)
        except TargetError as exc:
            raise PushError(
                "target_unreachable",
                f"Cannot confirm with {target.name} whether it took the thread: "
                f"{exc}. Retry when it is reachable, or cancel with --force if "
                "it is gone for good.",
                safe_to_retry=True,
            ) from exc
    moves.clear_move(thread_id)
    return {"state": "local", "thread_id": thread_id}


def _confirm_not_arrived(
    record: dict[str, Any], target: DurableTarget
) -> dict[str, Any]:
    import httpx

    try:
        response = httpx.get(
            f"{target.base_url}{targets_mod.ARRIVALS_PATH}{record['operation_id']}/",
            headers=targets_mod._headers(),
            timeout=targets_mod.PROBE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise TargetError("target_offline", f"{target.name} is not reachable") from exc
    if response.status_code == 404:
        moves.clear_move(record["thread_id"])
        return {"state": "local", "thread_id": record["thread_id"]}
    if response.status_code != 200:
        raise TargetError("target_error", f"HTTP {response.status_code}")
    arrival = response.json()
    if arrival.get("state") == "ready" and arrival.get("thread_id"):
        done = moves.set_move(
            record["thread_id"],
            state=moves.STATE_MOVED,
            target_thread_id=str(arrival["thread_id"]),
            moved_at=moves.now_iso(),
        )
        return _moved_result(done)
    raise TargetError("target_busy", "the target is still taking the thread")


def moved_thread_detail(thread_id: str) -> dict[str, Any] | None:
    """The live copy of a thread that moved away, served by its new computer.

    Lets a fleet-scoped client that opens the old copy follow the thread to
    where it continues (it then talks to that computer directly). None when
    the thread did not move, moved under another id, or the target is not
    reachable — callers fall back to the read-only local copy.
    """
    move = moves.get_move(thread_id)
    if not move or move.get("state") != moves.STATE_MOVED:
        return None
    target_thread_id = move.get("target_thread_id") or thread_id
    if target_thread_id != thread_id:
        return None
    target = _target_from_record(move)
    if not target.base_url:
        return None
    return targets_mod.fetch_thread(target, target_thread_id)


def release_moved(thread_id: str) -> dict[str, Any]:
    """Make a moved thread's local copy usable again (explicit escape hatch)."""
    removed = moves.clear_move(thread_id)
    return {"state": "local", "thread_id": thread_id, "released": bool(removed)}


# --- receiving side --------------------------------------------------------


def target_capabilities() -> dict[str, Any]:
    """What this computer can receive (``GET /api/threads/push/target/``)."""
    from openbase_coder_cli.thread_sync.thread_exchange import (
        get_or_create_device_identity,
    )

    return {
        "accepts_pushes": True,
        "protocol": targets_mod.PUSH_PROTOCOL_VERSION,
        "device_name": get_or_create_device_identity().device_name,
        "durable": targets_mod.this_computer_is_durable(),
        "backends": configured_backends(),
        "exchange_synced": _exchange_synced(),
    }


_REQUIRED_ARRIVAL_FIELDS = (
    "operation_id",
    "thread_id",
    "backend",
    "entity_id",
    "fingerprint",
    "source_device_id",
    "directory",
)


def _expand_home_relative(value: str) -> Path:
    if value == "~":
        return Path.home()
    if value.startswith("~/"):
        return Path.home() / value[2:]
    return Path(value)


async def accept_push(manager: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Take a pushed thread (``POST /api/threads/push/arrivals/``)."""
    missing = [
        key for key in _REQUIRED_ARRIVAL_FIELDS if not isinstance(payload.get(key), str)
    ]
    if missing:
        raise PushError(
            "invalid_request",
            f"Missing fields: {', '.join(missing)}",
            http_status=400,
        )
    operation_id = _operation_id(payload["operation_id"])
    lock = _thread_lock(f"arrival:{payload['entity_id']}")
    if not lock.acquire(blocking=False):
        raise PushError(
            "push_in_progress",
            "This thread is already being received.",
            safe_to_retry=True,
        )
    try:
        return await _accept_locked(manager, operation_id, payload)
    finally:
        lock.release()


async def _accept_locked(
    manager: Any, operation_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    previous = moves.get_arrival(operation_id)
    if previous and previous.get("state") == "ready":
        return _arrival_result(previous)

    backend = payload["backend"]
    if backend not in handoff.SUPPORTED_BACKENDS:
        raise PushError(
            "unsupported_backend", "Only Codex and Claude Code threads can be pushed."
        )
    backend_mgr = backend_manager(manager, backend)
    if backend_mgr is None:
        label = "Codex" if backend == handoff.CODEX else "Claude Code"
        raise PushError("backend_missing", f"{label} is not set up on this computer.")
    if not await asyncio.to_thread(_exchange_synced):
        raise PushError(
            "exchange_not_synced", "Thread sync is not enabled on this computer."
        )
    directory = _expand_home_relative(payload["directory"])
    if not directory.is_dir():
        raise PushError(
            "folder_missing",
            f"{payload['directory']} does not exist on this computer yet. "
            "Check that the folder syncs here, then try again.",
            safe_to_retry=True,
        )
    if await asyncio.to_thread(_synced_root, str(directory)) is None:
        raise PushError(
            "folder_not_synced",
            f"{payload['directory']} is not in a synced folder on this computer.",
        )

    snapshot = HandoffSnapshot(
        backend=backend,
        entity_id=payload["entity_id"],
        fingerprint=payload["fingerprint"],
        source_device_id=payload["source_device_id"],
    )
    moves.set_arrival(
        operation_id,
        state="importing",
        source_thread_id=payload["thread_id"],
        source_device_name=payload.get("source_device_name"),
        backend=backend,
        entity_id=snapshot.entity_id,
        fingerprint=snapshot.fingerprint,
    )
    await asyncio.to_thread(
        _settle_inbound, [str(handoff.exchange_dir()), str(directory)]
    )
    if not await _wait_for_snapshot(snapshot):
        raise PushError(
            "snapshot_missing",
            "The thread's transcript has not arrived here yet. Try again in a moment.",
            safe_to_retry=True,
        )
    local_id = await asyncio.to_thread(handoff.local_thread_id, snapshot)
    if local_id:
        local = await _thread_state_or_none(manager, local_id)
        if local is not None and (reason := await busy_reason(manager, local)):
            raise PushError("target_busy", reason, safe_to_retry=True)
        if backend == handoff.CODEX:
            await _unload_codex_thread(backend_mgr, local_id)
    try:
        outcome = await asyncio.to_thread(handoff.import_one, snapshot)
    except HandoffError as exc:
        raise _from_handoff(exc) from exc
    target_id = await asyncio.to_thread(handoff.local_thread_id, snapshot)
    thread = await _thread_state_or_none(manager, target_id) if target_id else None
    if thread is None:
        raise PushError(
            "import_failed",
            "The thread was imported but this computer's agent cannot open it.",
        )
    # The thread lives here now: forget any earlier move away from here.
    for stale in {target_id, payload["thread_id"]}:
        existing = moves.get_move(stale)
        if existing and existing.get("state") != moves.STATE_PUSHING:
            moves.clear_move(stale)

    record = moves.set_arrival(
        operation_id, state="imported", thread_id=target_id, import_outcome=outcome
    )
    message = payload.get("message")
    if (
        isinstance(message, str)
        and message.strip()
        and not record.get("turn_requested")
    ):
        moves.set_arrival(operation_id, turn_requested=True)
        try:
            await manager.start_turn(target_id, message.strip()[:MAX_MESSAGE_CHARS])
            moves.set_arrival(operation_id, turn_started=True)
        except Exception as exc:  # noqa: BLE001 - the thread still arrived
            logger.warning(
                "thread_push follow_up_failed thread_id=%s error=%s", target_id, exc
            )
            moves.set_arrival(operation_id, turn_error=str(exc))
    record = moves.set_arrival(operation_id, state="ready")
    _invalidate_thread_caches()
    logger.info(
        "thread_push arrived thread_id=%s from=%s outcome=%s",
        target_id,
        payload.get("source_device_name"),
        outcome,
    )
    return _arrival_result(record)


async def _thread_state_or_none(manager: Any, thread_id: str) -> Any | None:
    """The thread's state, or None when this computer's agent cannot open it.

    A Codex app-server answers a thread it does not hold with an error
    ("thread not loaded"), not an empty result.
    """
    try:
        return await manager.get_thread_state(thread_id)
    except (RuntimeError, ValueError) as exc:
        logger.info(
            "thread_push thread_unreadable thread_id=%s error=%s", thread_id, exc
        )
        return None


def _arrival_result(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": record.get("state"),
        "operation_id": record.get("operation_id"),
        "thread_id": record.get("thread_id"),
        "turn_started": bool(record.get("turn_started")),
        "turn_error": record.get("turn_error"),
    }


def arrival_status(operation_id: str) -> dict[str, Any] | None:
    record = moves.get_arrival(_operation_id(operation_id))
    return _arrival_result(record) if record else None


def _settle_inbound(paths: list[str]) -> None:
    from openbase_coder_cli.sync_daemon import SyncDaemonError

    for path in paths:
        try:
            _barrier("settle", path, SETTLE_TIMEOUT_SECONDS)
        except SyncDaemonError:
            # The snapshot wait below is the real gate.
            return


async def _wait_for_snapshot(snapshot: HandoffSnapshot) -> bool:
    deadline = time.monotonic() + SNAPSHOT_WAIT_SECONDS
    while True:
        if await asyncio.to_thread(handoff.snapshot_present, snapshot):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(SNAPSHOT_POLL_SECONDS)


def _invalidate_thread_caches() -> None:
    try:
        from openbase_coder_cli.openbase_coder_cli_app.thread_cache import (
            invalidate_thread_list_cache,
        )

        invalidate_thread_list_cache()
    except Exception:  # noqa: BLE001 - cache invalidation is best effort
        logger.debug("thread cache invalidation failed", exc_info=True)

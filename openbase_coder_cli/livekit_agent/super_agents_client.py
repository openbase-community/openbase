from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from super_agents.app_protocol import (
    extract_started_turn_id as _extract_turn_id,
)
from super_agents.app_protocol import (
    is_queue_item_id as _is_queue_item_id,
)
from super_agents.app_protocol import (
    response_is_queued as _response_is_queued,
)

from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
)
from openbase_coder_cli.backend_config import (
    configured_execution_backend as _configured_execution_backend,
)
from openbase_coder_cli.claude_auth import (
    heal_claude_auth,
    is_claude_auth_failure_text,
    verified_claude_auth_status,
)
from openbase_coder_cli.codex_session_defaults import (
    DEFAULT_CODEX_APPROVAL_POLICY,
    DEFAULT_CODEX_SANDBOX,
)
from openbase_coder_cli.dispatcher_config import (
    dispatcher_model,
    super_agents_model,
)
from openbase_coder_cli.livekit_agent.codex_turns import (
    _prompt_debug_fields,
    _super_agent_name,
)
from openbase_coder_cli.livekit_agent.super_agents_client_common import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_DISPATCHER_LABEL,
    DISPATCH_TIMING_LOG,
    logger,
)
from openbase_coder_cli.livekit_agent.super_agents_client_threads import (
    SuperAgentsClientThreadsMixin,
)
from openbase_coder_cli.livekit_agent.super_agents_client_turns import (
    SuperAgentsClientTurnsMixin,
)
from openbase_coder_cli.livekit_agent.super_agents_speech import (
    _last_useful_message,
    _looks_like_metadata_identifier,
    _looks_like_schema_label,
    _looks_like_timestamp,
    _normalize_speech_candidate,
    _progress_has_pending_requests,
    _should_ignore_speech_text,
    _speech_text_from_progress,
    _text_content,
    _user_message_texts,
)
from openbase_coder_cli.paths import CODEX_DISPATCHER_CONFIG_PATH

# The speech helpers moved to ``super_agents_speech`` and the Super Agents
# protocol/backend helpers moved to the mixin modules; keep them importable
# from this module for the tests and other callers that import them here.
__all__ = [
    "SuperAgentsLiveKitClient",
    "_configured_execution_backend",
    "_extract_turn_id",
    "_last_useful_message",
    "_looks_like_metadata_identifier",
    "_looks_like_schema_label",
    "_looks_like_timestamp",
    "_normalize_speech_candidate",
    "_progress_has_pending_requests",
    "_response_is_queued",
    "_should_ignore_speech_text",
    "_speech_text_from_progress",
    "_text_content",
    "_user_message_texts",
]

TURN_POLL_INTERVAL_SECONDS = 0.5
# A busy app-server can miss individual progress polls (observed: thread/read
# timing out after 30s while the backend churned on tool output). Keep polling
# through transient failures instead of killing the voice generation that is
# waiting to speak the answer; only give up after this many consecutive
# failures.
TURN_POLL_MAX_CONSECUTIVE_FAILURES = 10
TURN_POLL_FAILURE_BACKOFF_MAX_SECONDS = 5.0
# Claude Code can mark a turn completed just before the useful assistant text
# is visible in the compact progress snapshot. Do not fall through to speech
# extraction on a terminal-but-empty snapshot immediately; give the store a
# short window to expose the answer.
TURN_POLL_COMPLETED_EMPTY_SPEECH_GRACE_SECONDS = 2.0
# How long to wait after a turn finishes before treating its unclaimed answer
# as orphaned (no voice dispatch consumed it) and handing it to the orphaned
# result handler to be spoken directly.
ORPHANED_RESULT_GRACE_SECONDS = 1.5
# The Openbase Claude Code credential is a copy of the normal login, so it
# goes stale whenever the normal config dir rotates the shared refresh token
# first. Claude Code then answers turns with a "Failed to authenticate ..."
# string instead of erroring, which would otherwise be spoken on every turn
# until someone runs `openbase-coder claude sync-state` by hand.
CLAUDE_AUTH_HEAL_DEBOUNCE_SECONDS = 300.0
_last_claude_auth_heal_monotonic: float | None = None


def _maybe_schedule_claude_auth_heal(speech_text: str) -> None:
    """Re-bridge Claude auth in the background when a turn spoke a login failure."""
    global _last_claude_auth_heal_monotonic
    if not is_claude_auth_failure_text(speech_text):
        return
    now = time.monotonic()
    last = _last_claude_auth_heal_monotonic
    if last is not None and now - last < CLAUDE_AUTH_HEAL_DEBOUNCE_SECONDS:
        return
    _last_claude_auth_heal_monotonic = now

    def _heal() -> None:
        try:
            result = heal_claude_auth()
        except Exception:
            logger.exception("Claude auth auto-heal crashed")
            return
        log = logger.info if result.state_updated else logger.warning
        log(
            "Claude auth failure surfaced in a spoken answer; auto-heal %s: %s",
            "succeeded" if result.state_updated else "failed",
            result.message,
        )

    threading.Thread(target=_heal, name="claude-auth-heal", daemon=True).start()


def _model_name_for_role(
    path: Path | None = None,
    *,
    use_super_agent_model: bool = False,
) -> str | None:
    if use_super_agent_model:
        return super_agents_model(path)
    return dispatcher_model(path) or DEFAULT_CODEX_MODEL


class SuperAgentsLiveKitClient(
    SuperAgentsClientTurnsMixin,
    SuperAgentsClientThreadsMixin,
):
    """LiveKit voice client backed by the Super Agents Python interface."""

    def __init__(
        self,
        *,
        cwd: str,
        state_path: str | None = None,
        developer_instructions: str | None = None,
        approval_policy: str = DEFAULT_CODEX_APPROVAL_POLICY,
        sandbox: str = DEFAULT_CODEX_SANDBOX,
        model_name: str | None = None,
        service_tier: str = "standard",
        dispatcher_config_path: str | Path | None = None,
        persist_thread: bool = True,
        fresh_thread: bool = False,
        initial_thread_id: str | None = None,
        super_agent_name: str | None = None,
        super_agent_agent_name: str | None = None,
        use_super_agent_reasoning: bool = False,
        backend_client: Any | None = None,
    ) -> None:
        self._cwd = cwd
        self._state_path = (
            Path(state_path or Path.home() / ".openbase" / "livekit-voice-route.json")
            if persist_thread
            else None
        )
        self._developer_instructions = developer_instructions or None
        self._approval_policy = approval_policy
        self._sandbox = sandbox
        self._service_tier = service_tier
        self._super_agent_name = _super_agent_name(
            super_agent_name
            if super_agent_name is not None
            else DEFAULT_DISPATCHER_LABEL
            if persist_thread
            else None
        )
        self._super_agent_agent_name = _super_agent_name(super_agent_agent_name)
        self._use_super_agent_reasoning = use_super_agent_reasoning
        self._dispatcher_config_path = Path(
            dispatcher_config_path
            or os.getenv("LIVEKIT_DISPATCHER_CONFIG_PATH")
            or CODEX_DISPATCHER_CONFIG_PATH
        )
        self._model_name = model_name or _model_name_for_role(
            self._dispatcher_config_path,
            use_super_agent_model=self._use_super_agent_reasoning,
        )
        self._backend_client = backend_client or self._client_from_environment()
        self._register_backend_callback()
        self._fresh_thread = fresh_thread
        self._thread_id = initial_thread_id or (
            self._load_thread_id() if persist_thread else None
        )
        self._thread_loaded = False
        self._active_turn_id: str | None = None
        self._active_turn_started_at: float | None = None
        self._active_turn_dispatch_id: str | None = None
        self._active_turn_prompt_hash: str | None = None
        self._active_turn_wait_task: asyncio.Task[dict[str, Any]] | None = None
        self._active_turn_wait_task_turn_id: str | None = None
        self._active_turn_wait_waiters = 0
        self._on_orphaned_result: (
            Callable[[SuperAgentsLiveKitClient, str, str], None] | None
        ) = None
        self._claimed_speech_turns: set[str] = set()
        self._turn_prompt_hashes: dict[str, set[str]] = {}
        self._turn_spoken_at: dict[str, float] = {}
        self._state_lock = asyncio.Lock()
        self._turn_start_lock = asyncio.Lock()

    async def run_turn(
        self,
        prompt: str,
        *,
        developer_instructions: str | None = None,
    ) -> dict[str, Any]:
        dispatch_id = f"voice-{uuid.uuid4().hex[:12]}"
        dispatch_started = time.monotonic()
        prompt_debug = _prompt_debug_fields(prompt)
        turn_id: str | None = None
        preserve_active_turn = False

        async with self._turn_start_lock:
            thread_id = await self._ensure_thread()
            logger.info(
                "%s stage=voice_request_received dispatch_id=%s thread_id=%s "
                "cwd_basename=%s elapsed_ms=%d",
                DISPATCH_TIMING_LOG,
                dispatch_id,
                thread_id,
                Path(self._cwd).name,
                int((time.monotonic() - dispatch_started) * 1000),
            )

            if self._active_turn_id and self._active_turn_has_completed():
                completed_turn_id = self._active_turn_id
                if (
                    completed_turn_id in self._claimed_speech_turns
                    and self._is_duplicate_of_spoken_turn(
                        completed_turn_id, prompt_debug["hash"]
                    )
                ):
                    # The just-spoken answer already covered this content
                    # (an STT twin or a restated correction); a fresh turn
                    # would speak the same gist twice.
                    logger.info(
                        "%s stage=voice_request_suppressed_duplicate_of_spoken_turn "
                        "dispatch_id=%s thread_id=%s turn_id=%s prompt_hash=%s",
                        DISPATCH_TIMING_LOG,
                        dispatch_id,
                        thread_id,
                        completed_turn_id,
                        prompt_debug["hash"],
                    )
                    return {
                        "id": completed_turn_id,
                        "status": "completed",
                        "_livekit_speech_text": "",
                        "_livekit_turn_id": completed_turn_id,
                        "progress": {},
                    }
                if prompt_debug["hash"] not in self._turn_prompt_hashes.get(
                    completed_turn_id, set()
                ):
                    self._handoff_completed_active_turn()

            if self._active_turn_id:
                if self._active_turn_has_completed():
                    turn_id = self._active_turn_id
                    logger.info(
                        "%s stage=voice_request_joined_completed_active_turn "
                        "dispatch_id=%s thread_id=%s turn_id=%s prompt_hash=%s",
                        DISPATCH_TIMING_LOG,
                        dispatch_id,
                        thread_id,
                        turn_id,
                        prompt_debug["hash"],
                    )
                elif self._active_turn_prompt_hash == prompt_debug["hash"]:
                    turn_id = self._active_turn_id
                    logger.info(
                        "%s stage=voice_request_joined_active_turn dispatch_id=%s "
                        "thread_id=%s turn_id=%s prompt_hash=%s",
                        DISPATCH_TIMING_LOG,
                        dispatch_id,
                        thread_id,
                        turn_id,
                        prompt_debug["hash"],
                    )
                else:
                    turn_id = await self._steer_turn(thread_id, prompt)
            else:
                active_turn_id = await self._resolve_active_turn_id(thread_id)
                if active_turn_id:
                    self._active_turn_id = active_turn_id
                    self._active_turn_prompt_hash = None
                    turn_id = await self._steer_turn(thread_id, prompt)
                else:
                    start_task = asyncio.create_task(
                        self._start_turn(
                            thread_id,
                            prompt,
                            developer_instructions=developer_instructions,
                            dispatch_id=dispatch_id,
                        )
                    )
                    try:
                        turn_id = await asyncio.shield(start_task)
                    except asyncio.CancelledError:
                        turn_id = await start_task
                        self._active_turn_id = turn_id
                        self._active_turn_started_at = time.monotonic()
                        self._active_turn_dispatch_id = dispatch_id
                        self._active_turn_prompt_hash = prompt_debug["hash"]
                        self._record_turn_prompt(turn_id, prompt_debug["hash"])
                        preserve_active_turn = True
                        logger.info(
                            "%s stage=turn_start_cancelled_after_backend_start "
                            "dispatch_id=%s thread_id=%s turn_id=%s elapsed_ms=%d",
                            DISPATCH_TIMING_LOG,
                            dispatch_id,
                            thread_id,
                            turn_id,
                            int((time.monotonic() - dispatch_started) * 1000),
                        )
                        raise
            self._active_turn_id = turn_id
            self._active_turn_started_at = time.monotonic()
            self._active_turn_dispatch_id = dispatch_id
            self._active_turn_prompt_hash = prompt_debug["hash"]
            self._record_turn_prompt(turn_id, prompt_debug["hash"])

        logger.info(
            "%s stage=turn_wait_start dispatch_id=%s thread_id=%s turn_id=%s "
            "prompt_hash=%s prompt_len=%s",
            DISPATCH_TIMING_LOG,
            dispatch_id,
            thread_id,
            turn_id,
            prompt_debug["hash"],
            prompt_debug["length"],
        )
        try:
            result = await self._wait_for_turn(thread_id, turn_id)
            speech_text = _speech_text_from_progress(result)
            _maybe_schedule_claude_auth_heal(speech_text)
            completed_turn = {
                "id": turn_id,
                "status": result.get("status")
                or result.get("summary", {}).get("status"),
                "_livekit_speech_text": speech_text,
                "_livekit_turn_id": turn_id,
                "progress": result,
            }
            logger.info(
                "%s stage=voice_turn_result dispatch_id=%s turn_id=%s status=%s "
                "elapsed_ms=%d speech_chars=%d",
                DISPATCH_TIMING_LOG,
                dispatch_id,
                turn_id,
                completed_turn.get("status"),
                int((time.monotonic() - dispatch_started) * 1000),
                len(speech_text),
            )
            return completed_turn
        except asyncio.CancelledError:
            preserve_active_turn = True
            logger.info(
                "%s stage=voice_turn_cancelled dispatch_id=%s turn_id=%s elapsed_ms=%d",
                DISPATCH_TIMING_LOG,
                dispatch_id,
                turn_id,
                int((time.monotonic() - dispatch_started) * 1000),
            )
            raise
        except Exception as exc:
            # The backend turn is likely still running (for example the
            # progress polling gave up during an app-server stall); keep it
            # active so the user's next utterance can rejoin it and the
            # finished answer still gets spoken.
            preserve_active_turn = True
            logger.error(
                "%s stage=voice_turn_wait_failed dispatch_id=%s turn_id=%s "
                "elapsed_ms=%d error=%s",
                DISPATCH_TIMING_LOG,
                dispatch_id,
                turn_id,
                int((time.monotonic() - dispatch_started) * 1000),
                exc,
            )
            raise
        finally:
            if not preserve_active_turn and turn_id and self._active_turn_id == turn_id:
                self._clear_active_turn_state()

    def _handoff_completed_active_turn(self) -> None:
        turn_id = self._active_turn_id
        wait_task = self._active_turn_wait_task
        handler = self._on_orphaned_result
        if (
            turn_id
            and wait_task
            and wait_task.done()
            and not wait_task.cancelled()
            and wait_task.exception() is None
            and turn_id not in self._claimed_speech_turns
            and handler is not None
        ):
            speech_text = _speech_text_from_progress(wait_task.result())
            _maybe_schedule_claude_auth_heal(speech_text)
            if speech_text:
                logger.info(
                    "%s stage=completed_turn_handoff turn_id=%s speech_chars=%d",
                    DISPATCH_TIMING_LOG,
                    turn_id,
                    len(speech_text),
                )
                handler(self, turn_id, speech_text)
        self._clear_active_turn_state()

    async def _wait_for_queued_turn_to_start(
        self,
        thread_id: str,
        *,
        queued_id: str | None,
        blocked_by_turn_id: str | None,
        dispatch_id: str,
    ) -> str:
        while True:
            progress = await self._backend_client.progress_by_label(
                self._query(
                    thread_id=thread_id,
                    include_turn=True,
                    max_items=1,
                    max_output_chars=200,
                )
            )
            turn_id = _extract_turn_id(progress)
            status = str(
                progress.get("status")
                or progress.get("summary", {}).get("status")
                or ""
            ).lower()
            if (
                turn_id
                and not _is_queue_item_id(turn_id)
                and turn_id != blocked_by_turn_id
            ):
                return turn_id
            logger.info(
                "%s stage=queued_turn_wait dispatch_id=%s thread_id=%s "
                "queued_id=%s latest_turn_id=%s latest_status=%s",
                DISPATCH_TIMING_LOG,
                dispatch_id,
                thread_id,
                queued_id,
                turn_id,
                status,
            )
            await asyncio.sleep(TURN_POLL_INTERVAL_SECONDS)

    async def _deliver_orphaned_result_after_grace(
        self, wait_task: asyncio.Task[dict[str, Any]]
    ) -> None:
        await asyncio.sleep(ORPHANED_RESULT_GRACE_SECONDS)
        if self._active_turn_wait_task is not wait_task:
            return
        if self._active_turn_wait_waiters > 0:
            return
        turn_id = self._active_turn_wait_task_turn_id
        handler = self._on_orphaned_result
        if not turn_id or handler is None or turn_id in self._claimed_speech_turns:
            return
        speech_text = _speech_text_from_progress(wait_task.result())
        _maybe_schedule_claude_auth_heal(speech_text)
        if not speech_text:
            return
        logger.info(
            "%s stage=orphaned_turn_result_detected turn_id=%s speech_chars=%d",
            DISPATCH_TIMING_LOG,
            turn_id,
            len(speech_text),
        )
        handler(self, turn_id, speech_text)

    async def _poll_turn_until_ready(
        self,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        consecutive_failures = 0
        empty_answer_started_at: float | None = None
        while True:
            try:
                progress = await self._backend_client.progress_by_label(
                    self._query(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        include_items=True,
                        final_only=True,
                        max_items=5,
                        max_output_chars=4000,
                        include_turn=True,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures >= TURN_POLL_MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        "%s stage=turn_wait_poll_gave_up turn_id=%s "
                        "consecutive_failures=%d error=%s",
                        DISPATCH_TIMING_LOG,
                        turn_id,
                        consecutive_failures,
                        exc,
                    )
                    raise
                backoff = min(
                    TURN_POLL_INTERVAL_SECONDS * (2**consecutive_failures),
                    TURN_POLL_FAILURE_BACKOFF_MAX_SECONDS,
                )
                logger.warning(
                    "%s stage=turn_wait_poll_error turn_id=%s "
                    "consecutive_failures=%d retry_in_s=%.1f error=%s",
                    DISPATCH_TIMING_LOG,
                    turn_id,
                    consecutive_failures,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
                continue
            consecutive_failures = 0
            status = str(
                progress.get("status")
                or progress.get("summary", {}).get("status")
                or ""
            ).lower()
            has_pending_requests = _progress_has_pending_requests(progress)
            if status == "waiting" and not has_pending_requests:
                should_wait, empty_answer_started_at = _should_wait_for_speech_text(
                    progress,
                    turn_id=turn_id,
                    status=status,
                    empty_answer_started_at=empty_answer_started_at,
                )
                if should_wait:
                    await asyncio.sleep(TURN_POLL_INTERVAL_SECONDS)
                    continue
                return progress
            if status and status not in {
                "running",
                "waiting",
                "queued",
                "inprogress",
                "in_progress",
            }:
                if status in {"completed", "complete", "succeeded", "success"}:
                    should_wait, empty_answer_started_at = _should_wait_for_speech_text(
                        progress,
                        turn_id=turn_id,
                        status=status,
                        empty_answer_started_at=empty_answer_started_at,
                    )
                    if should_wait:
                        await asyncio.sleep(TURN_POLL_INTERVAL_SECONDS)
                        continue
                return progress
            empty_answer_started_at = None
            await asyncio.sleep(TURN_POLL_INTERVAL_SECONDS)

    async def _ensure_claude_auth_ready(self) -> None:
        """Heal a dead Openbase Claude login before the thread takes turns.

        A stranded refresh token can leave the scoped config dir fully logged
        out (Claude Code wipes the credential after a failed refresh), and in
        that state every turn answers "Not logged in · Please run /login".
        The spoken-answer heal only fires after a failed turn, so verify and
        re-bridge up front instead of letting the first turn fail.
        """
        if getattr(self._backend_client, "backend", None) != CLAUDE_CODE_BACKEND:
            return
        try:
            status = await asyncio.to_thread(verified_claude_auth_status)
            if status.logged_in:
                return
            logger.warning(
                "Openbase Claude Code login unavailable before thread start; "
                "attempting auto-heal: %s",
                status.raw_output,
            )
            result = await asyncio.to_thread(heal_claude_auth)
            log = logger.info if result.state_updated else logger.warning
            log(
                "Claude auth pre-flight heal %s: %s",
                "succeeded" if result.state_updated else "failed",
                result.message,
            )
        except Exception:
            logger.exception("Claude auth pre-flight heal crashed")


def _should_wait_for_speech_text(
    progress: dict[str, Any],
    *,
    turn_id: str,
    status: str,
    empty_answer_started_at: float | None,
) -> tuple[bool, float | None]:
    if _speech_text_from_progress(progress):
        return False, empty_answer_started_at
    now = time.monotonic()
    started_at = empty_answer_started_at if empty_answer_started_at is not None else now
    should_wait = now - started_at < TURN_POLL_COMPLETED_EMPTY_SPEECH_GRACE_SECONDS
    if should_wait:
        logger.info(
            "%s stage=turn_wait_completed_without_speech turn_id=%s "
            "status=%s retrying=True",
            DISPATCH_TIMING_LOG,
            turn_id,
            status,
        )
    return should_wait, started_at

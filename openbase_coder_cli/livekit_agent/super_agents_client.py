from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
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
    dispatcher_reasoning_effort,
    dispatcher_voice,
    super_agents_model,
    super_agents_reasoning_effort,
)
from openbase_coder_cli.livekit_agent.codex_thread_state import (
    load_thread_id,
    persist_thread_id,
    persist_voice_route_state,
    thread_state_file_lock,
)
from openbase_coder_cli.livekit_agent.codex_turns import (
    _active_turn_id_mismatch,
    _is_no_active_turn_error,
    _is_turn_cannot_accept_steering_error,
    _prompt_debug_fields,
    _speech_excerpt,
    _super_agent_name,
    _with_super_agent_identity_instructions,
)
from openbase_coder_cli.paths import CODEX_DISPATCHER_CONFIG_PATH

logger = logging.getLogger(__name__)
DISPATCH_TIMING_LOG = "dispatch_timing"
DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_DISPATCHER_LABEL = "dispatcher"
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
# A repeat of content a just-spoken turn already covered (an STT twin of a
# steered correction, or the user restating it) must not spawn a fresh
# backend turn that answers with the same gist again.
SPOKEN_TURN_DUPLICATE_SUPPRESSION_SECONDS = 10.0
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


def _is_super_agents_mcp_server(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return normalized in {"super-agents", "mcp-super-agents"}


def _execution_backend_for_configured_backend(backend: str) -> str:
    if backend == OPENBASE_CLOUD_BACKEND:
        return CLAUDE_CODE_BACKEND
    if backend == OPENBASE_CLOUD_CODEX_BACKEND:
        return CODEX_BACKEND
    return backend


def _configured_execution_backend(
    environment_backend: Callable[[], str],
) -> str:
    from openbase_coder_cli.cli.backend import read_backend
    from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH

    if DEFAULT_ENV_FILE_PATH.is_file():
        configured_backend = read_backend(DEFAULT_ENV_FILE_PATH)
        if not configured_backend.startswith("unsupported:"):
            return _execution_backend_for_configured_backend(configured_backend)
    return environment_backend()


class SuperAgentsLiveKitClient:
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

    @property
    def model_name(self) -> str:
        backend = getattr(self._backend_client, "backend", None)
        if isinstance(backend, str) and backend != "codex":
            return backend
        return self._model_name

    def set_super_agent_name(self, name: str | None) -> None:
        self._super_agent_name = _super_agent_name(name)

    def set_super_agent_agent_name(self, name: str | None) -> None:
        self._super_agent_agent_name = _super_agent_name(name)

    async def aclose(self) -> None:
        close = getattr(self._backend_client, "close", None)
        if close is not None:
            await close()

    async def prepare(self) -> str:
        return await self._ensure_thread()

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

    def _clear_active_turn_state(self) -> None:
        self._active_turn_id = None
        self._active_turn_started_at = None
        self._active_turn_dispatch_id = None
        self._active_turn_prompt_hash = None
        self._active_turn_wait_task = None
        self._active_turn_wait_task_turn_id = None

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

    def has_active_prompt(self, prompt: str) -> bool:
        prompt_debug = _prompt_debug_fields(prompt)
        return (
            bool(self._active_turn_id)
            and self._active_turn_prompt_hash == prompt_debug["hash"]
        )

    async def steer_active_turn(self, prompt: str) -> str | None:
        prompt = prompt.strip()
        if not prompt:
            return None

        prompt_debug = _prompt_debug_fields(prompt)
        async with self._turn_start_lock:
            thread_id = await self._ensure_thread()
            if self._active_turn_id:
                if self._active_turn_has_completed():
                    if prompt_debug["hash"] not in self._turn_prompt_hashes.get(
                        self._active_turn_id, set()
                    ):
                        logger.info(
                            "%s stage=proactive_steer_completed_active_turn "
                            "thread_id=%s turn_id=%s prompt_hash=%s accepted=false",
                            DISPATCH_TIMING_LOG,
                            thread_id,
                            self._active_turn_id,
                            prompt_debug["hash"],
                        )
                        return None
                    logger.info(
                        "%s stage=proactive_steer_completed_active_turn "
                        "thread_id=%s turn_id=%s prompt_hash=%s accepted=duplicate",
                        DISPATCH_TIMING_LOG,
                        thread_id,
                        self._active_turn_id,
                        prompt_debug["hash"],
                    )
                    return self._active_turn_id
                if self._active_turn_prompt_hash == prompt_debug["hash"]:
                    logger.info(
                        "%s stage=proactive_steer_joined_active_turn thread_id=%s "
                        "turn_id=%s prompt_hash=%s",
                        DISPATCH_TIMING_LOG,
                        thread_id,
                        self._active_turn_id,
                        prompt_debug["hash"],
                    )
                    return None
                return await self._steer_turn(
                    thread_id,
                    prompt,
                    start_when_inactive=False,
                )

            active_turn_id = await self._resolve_active_turn_id(thread_id)
            if not active_turn_id:
                logger.info(
                    "%s stage=proactive_steer_no_active_turn thread_id=%s "
                    "prompt_hash=%s prompt_len=%s",
                    DISPATCH_TIMING_LOG,
                    thread_id,
                    prompt_debug["hash"],
                    prompt_debug["length"],
                )
                return None

            self._active_turn_id = active_turn_id
            self._active_turn_prompt_hash = None
            return await self._steer_turn(
                thread_id,
                prompt,
                start_when_inactive=False,
            )

    async def _start_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        developer_instructions: str | None,
        dispatch_id: str,
    ) -> str:
        await self._ensure_claude_auth_ready()
        turn_input = self._turn_input(
            prompt,
            developer_instructions=developer_instructions,
            dispatch_id=dispatch_id,
        )

        previous_turn_id = None
        if not (
            self._backend_is_codex() and hasattr(self._backend_client, "start_turn")
        ):
            previous_turn_id = await self._latest_real_turn_id(thread_id)

        if self._backend_is_codex() and hasattr(self._backend_client, "start_turn"):
            result = await self._backend_client.start_turn(
                {"threadId": thread_id, **turn_input}
            )
        else:
            result = await self._backend_client.start_turn_by_label(
                self._query(thread_id=thread_id),
                turn_input,
            )
        if _response_is_queued(result):
            queued_id = _extract_queued_id(result)
            logger.info(
                "%s stage=turn_start_queued dispatch_id=%s thread_id=%s "
                "queued_id=%s blocked_by_turn_id=%s queue_depth=%s",
                DISPATCH_TIMING_LOG,
                dispatch_id,
                thread_id,
                queued_id,
                previous_turn_id,
                result.get("queueDepth") or result.get("position"),
            )
            turn_id = await self._wait_for_queued_turn_to_start(
                thread_id,
                queued_id=queued_id,
                blocked_by_turn_id=previous_turn_id,
                dispatch_id=dispatch_id,
            )
            logger.info(
                "%s stage=queued_turn_started dispatch_id=%s thread_id=%s "
                "queued_id=%s turn_id=%s",
                DISPATCH_TIMING_LOG,
                dispatch_id,
                thread_id,
                queued_id,
                turn_id,
            )
            return turn_id

        turn_id = _extract_turn_id(result)
        if not turn_id:
            raise RuntimeError("Super Agents did not return a turn id.")
        logger.info(
            "%s stage=turn_start_response dispatch_id=%s thread_id=%s turn_id=%s",
            DISPATCH_TIMING_LOG,
            dispatch_id,
            thread_id,
            turn_id,
        )
        return turn_id

    def _turn_input(
        self,
        prompt: str,
        *,
        developer_instructions: str | None,
        dispatch_id: str,
    ) -> dict[str, Any]:
        reasoning_effort = self._configured_reasoning_effort()
        turn_input: dict[str, Any] = {
            "prompt": prompt,
            "cwd": self._cwd,
            "label": self._super_agent_name,
            "agentName": self._super_agent_agent_name,
            "approvalPolicy": self._approval_policy,
            "sandbox": self._sandbox,
            "serviceTier": self._service_tier,
            "_mcpCallId": dispatch_id,
        }
        if self._backend_is_codex():
            turn_input["model"] = self._model_name
        elif self._model_name:
            turn_input["model"] = self._model_name
        if reasoning_effort:
            turn_input["reasoningEffort"] = reasoning_effort
        if effective_developer_instructions := self._turn_developer_instructions(
            developer_instructions
        ):
            turn_input["developerInstructions"] = effective_developer_instructions
        return turn_input

    async def _queue_rejected_steer(
        self,
        thread_id: str,
        prompt: str,
        *,
        blocked_by_turn_id: str,
    ) -> str:
        dispatch_id = f"voice-{uuid.uuid4().hex[:12]}"
        result = await self._backend_client.queue_turn_by_label(
            self._query(thread_id=thread_id),
            self._turn_input(
                prompt,
                developer_instructions=None,
                dispatch_id=dispatch_id,
            ),
        )
        if not _response_is_queued(result):
            turn_id = _extract_turn_id(result)
            if not turn_id:
                raise RuntimeError("Super Agents did not accept the follow-up turn.")
            return turn_id
        return await self._wait_for_queued_turn_to_start(
            thread_id,
            queued_id=_extract_queued_id(result),
            blocked_by_turn_id=blocked_by_turn_id,
            dispatch_id=dispatch_id,
        )

    async def _resolve_active_turn_id(self, thread_id: str) -> str | None:
        resolve_label = getattr(self._backend_client, "resolve_label", None)
        if resolve_label is None:
            return None
        try:
            result = await resolve_label(
                self._query(thread_id=thread_id, prefer="latest_active")
            )
        except Exception:
            logger.debug(
                "No active Super Agents turn found before voice follow-up",
                exc_info=True,
            )
            return None
        status = str(result.get("status") or "").lower()
        if status not in {"running", "waiting", "inprogress", "in_progress"}:
            return None
        turn_id = _extract_turn_id(result)
        if not turn_id:
            return None
        progress_status = await self._turn_status(thread_id, turn_id)
        if progress_status and progress_status not in {
            "running",
            "waiting",
            "queued",
            "inprogress",
            "in_progress",
        }:
            logger.info(
                "%s stage=active_turn_resolved_but_inactive thread_id=%s "
                "turn_id=%s resolved_status=%s progress_status=%s",
                DISPATCH_TIMING_LOG,
                thread_id,
                turn_id,
                status,
                progress_status,
            )
            return None
        logger.info(
            "%s stage=active_turn_resolved_for_steering thread_id=%s turn_id=%s status=%s",
            DISPATCH_TIMING_LOG,
            thread_id,
            turn_id,
            status,
        )
        return turn_id

    async def _turn_status(self, thread_id: str, turn_id: str) -> str | None:
        try:
            progress = await self._backend_client.progress_by_label(
                self._query(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    include_turn=True,
                    max_items=1,
                    max_output_chars=200,
                )
            )
        except Exception:
            logger.debug(
                "Could not validate Super Agents active turn before steering",
                exc_info=True,
            )
            return None
        return str(
            progress.get("status") or progress.get("summary", {}).get("status") or ""
        ).lower()

    async def _latest_real_turn_id(self, thread_id: str) -> str | None:
        try:
            progress = await self._backend_client.progress_by_label(
                self._query(
                    thread_id=thread_id,
                    include_turn=True,
                    max_items=1,
                    max_output_chars=200,
                )
            )
        except Exception:
            logger.debug(
                "Could not read latest Super Agents turn before queue wait",
                exc_info=True,
            )
            return None
        turn_id = _extract_turn_id(progress)
        return turn_id if turn_id and not _is_queue_item_id(turn_id) else None

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

    async def _steer_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        start_when_inactive: bool = True,
    ) -> str | None:
        assert self._active_turn_id is not None
        await self._ensure_claude_auth_ready()
        prompt_debug = _prompt_debug_fields(prompt)
        turn_input = {
            key: value
            for key, value in {
                "model": self._model_name,
                "reasoningEffort": self._configured_reasoning_effort(),
            }.items()
            if value is not None
        }
        try:
            result = await self._backend_client.steer_by_label(
                self._query(thread_id=thread_id, turn_id=self._active_turn_id),
                prompt,
                turn_input,
            )
        except RuntimeError as exc:
            actual_turn_id = _active_turn_id_mismatch(exc)
            if actual_turn_id:
                logger.warning(
                    "Super Agents active turn id drifted during steering; "
                    "expected_turn_id=%s actual_turn_id=%s prompt_hash=%s",
                    self._active_turn_id,
                    actual_turn_id,
                    prompt_debug["hash"],
                )
                self._active_turn_id = actual_turn_id
                result = await self._backend_client.steer_by_label(
                    self._query(thread_id=thread_id, turn_id=actual_turn_id),
                    prompt,
                    turn_input,
                )
            elif _is_turn_cannot_accept_steering_error(exc):
                logger.warning(
                    "Super Agents turn %s could not accept steering; queueing follow-up "
                    "prompt_hash=%s",
                    self._active_turn_id,
                    prompt_debug["hash"],
                )
                if not start_when_inactive:
                    return None
                return await self._queue_rejected_steer(
                    thread_id,
                    prompt,
                    blocked_by_turn_id=self._active_turn_id,
                )
            elif _is_no_active_turn_error(exc):
                logger.info(
                    "Super Agents turn %s was already inactive during steering prompt_hash=%s",
                    self._active_turn_id,
                    prompt_debug["hash"],
                )
                self._active_turn_id = None
                self._active_turn_prompt_hash = None
                if not start_when_inactive:
                    return None
                turn_id = await self._start_turn(
                    thread_id,
                    prompt,
                    developer_instructions=None,
                    dispatch_id=f"voice-{uuid.uuid4().hex[:12]}",
                )
                self._active_turn_id = turn_id
                self._active_turn_started_at = time.monotonic()
                self._active_turn_prompt_hash = prompt_debug["hash"]
                self._record_turn_prompt(turn_id, prompt_debug["hash"])
                return turn_id
            else:
                raise
        turn_id = _extract_turn_id(result) or self._active_turn_id
        self._active_turn_id = turn_id
        self._active_turn_prompt_hash = prompt_debug["hash"]
        self._record_turn_prompt(turn_id, prompt_debug["hash"])
        logger.info(
            "Submitted Super Agents turn steering turn_id=%s prompt_hash=%s prompt_len=%s",
            turn_id,
            prompt_debug["hash"],
            prompt_debug["length"],
        )
        return turn_id

    async def _wait_for_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        wait_task = self._active_turn_wait_task
        if (
            wait_task is None
            or self._active_turn_wait_task_turn_id != turn_id
            or (wait_task.done() and (wait_task.cancelled() or wait_task.exception()))
        ):
            wait_task = asyncio.create_task(
                self._poll_turn_until_ready(thread_id, turn_id),
                name=f"openbase-super-agents-turn-wait-{turn_id}",
            )
            self._active_turn_wait_task = wait_task
            self._active_turn_wait_task_turn_id = turn_id
            wait_task.add_done_callback(self._schedule_orphaned_result_check)
        self._active_turn_wait_waiters += 1
        try:
            return await asyncio.shield(wait_task)
        finally:
            self._active_turn_wait_waiters -= 1

    def set_orphaned_result_handler(
        self,
        handler: Callable[["SuperAgentsLiveKitClient", str, str], None] | None,
    ) -> None:
        self._on_orphaned_result = handler

    @property
    def orphaned_result_handler(
        self,
    ) -> Callable[["SuperAgentsLiveKitClient", str, str], None] | None:
        return self._on_orphaned_result

    def has_pending_voice_answer(self) -> bool:
        """Whether a backend turn still owes the user a spoken answer.

        True while the shared wait task is polling for a response, or when it
        finished with speech text nobody has claimed.
        """
        wait_task = self._active_turn_wait_task
        turn_id = self._active_turn_id
        if (
            not wait_task
            or not turn_id
            or self._active_turn_wait_task_turn_id != turn_id
        ):
            return False
        if not wait_task.done():
            return True
        if wait_task.cancelled() or wait_task.exception():
            return False
        if turn_id in self._claimed_speech_turns:
            return False
        return bool(_speech_text_from_progress(wait_task.result()))

    def _schedule_orphaned_result_check(
        self, wait_task: asyncio.Task[dict[str, Any]]
    ) -> None:
        if (
            wait_task.cancelled()
            or wait_task.exception()
            or self._on_orphaned_result is None
        ):
            return
        asyncio.create_task(
            self._deliver_orphaned_result_after_grace(wait_task),
            name="openbase-super-agents-orphaned-result-check",
        )

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

    def _active_turn_has_completed(self) -> bool:
        wait_task = self._active_turn_wait_task
        if (
            not wait_task
            or self._active_turn_wait_task_turn_id != self._active_turn_id
            or not wait_task.done()
            or wait_task.cancelled()
        ):
            return False
        return wait_task.exception() is None

    async def _ensure_thread(self) -> str:
        async with self._state_lock:
            if self._state_path is not None:
                with self._thread_state_file_lock():
                    canonical_thread_id = self._load_thread_id()
                    if canonical_thread_id and canonical_thread_id != self._thread_id:
                        logger.info(
                            "Adopting canonical LiveKit Super Agents thread from disk "
                            "previous_thread_id=%s canonical_thread_id=%s",
                            self._thread_id,
                            canonical_thread_id,
                        )
                        self._thread_id = canonical_thread_id
                        self._thread_loaded = False

                    if self._thread_loaded and self._thread_id:
                        return self._thread_id

                    if self._thread_id:
                        try:
                            return await self._resume_thread(self._thread_id)
                        except Exception:
                            logger.warning(
                                "Failed to resume persisted LiveKit Super Agents thread %s",
                                self._thread_id,
                                exc_info=True,
                            )
                            self._thread_id = None
                            self._thread_loaded = False

                    return await self._start_thread()

            if self._thread_loaded and self._thread_id:
                return self._thread_id
            if self._thread_id:
                try:
                    return await self._resume_thread(self._thread_id)
                except Exception:
                    logger.warning(
                        "Failed to resume LiveKit Super Agents thread %s; creating a new one",
                        self._thread_id,
                        exc_info=True,
                    )
                    self._thread_id = None
                    self._thread_loaded = False
            return await self._start_thread()

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

    async def _start_thread(self) -> str:
        params: dict[str, Any] = {
            "name": self._super_agent_name or DEFAULT_DISPATCHER_LABEL,
            "label": self._super_agent_name or DEFAULT_DISPATCHER_LABEL,
            "agentName": self._super_agent_agent_name,
            "cwd": self._cwd,
            "approvalPolicy": self._approval_policy,
            "sandbox": self._sandbox,
        }
        if self._fresh_thread:
            # Backends that reuse sessions by name (claude_code) must retire
            # the old dispatcher session so recreation yields a new
            # conversation, not a refresh of the previous one.
            params["fresh"] = True
        if self._backend_is_codex():
            params["model"] = self._model_name
        elif self._model_name:
            params["model"] = self._model_name
        if developer_instructions := self._thread_developer_instructions():
            params["developerInstructions"] = developer_instructions
        started = await self._backend_client.start_thread(params)
        thread_id = _extract_thread_id(started)
        if not thread_id:
            raise RuntimeError("Super Agents did not return a thread id.")
        self._thread_id = thread_id
        self._thread_loaded = True
        self._fresh_thread = False
        self._persist_thread_id(thread_id)
        logger.info("Started LiveKit Super Agents thread %s", thread_id)
        return thread_id

    async def _resume_thread(self, thread_id: str) -> str:
        if self._backend_is_codex() and hasattr(self._backend_client, "resume_thread"):
            resumed = await self._backend_client.resume_thread(
                thread_id,
                label=self._super_agent_name or DEFAULT_DISPATCHER_LABEL,
                agent_name=self._super_agent_agent_name,
                developer_instructions=self._thread_developer_instructions(),
            )
        else:
            resumed = await self._backend_client.resume_by_label(
                self._query(thread_id=thread_id, prefer="latest_any")
            )
        self._thread_id = _extract_thread_id(resumed) or thread_id
        if self._thread_id != thread_id:
            logger.warning(
                "LiveKit Super Agents resume returned a different thread id; "
                "requested_thread_id=%s resumed_thread_id=%s",
                thread_id,
                self._thread_id,
            )
        self._thread_loaded = True
        self._persist_thread_id(self._thread_id)
        return self._thread_id

    def _query(self, **overrides: Any) -> Any:
        from super_agents.app_models import LabelQueryInput

        values: dict[str, Any] = {
            "label": self._super_agent_name or DEFAULT_DISPATCHER_LABEL,
            "cwd": self._cwd,
            "prefer": "latest_any",
        }
        values.update(overrides)
        return LabelQueryInput(**values)

    def _client_from_environment(self) -> Any:
        from super_agents.app_server_client import CodexAppServerClient
        from super_agents.backend_clients import backend_from_environment

        try:
            from openbase_coder_cli.livekit_agent.config import _load_openbase_env

            _load_openbase_env(override=True)
        except Exception:
            logger.warning("Unable to refresh Openbase env before backend selection")

        execution_backend = _configured_execution_backend(backend_from_environment)
        if execution_backend == CLAUDE_CODE_BACKEND:
            from super_agents.claude_sdk import ClaudeAgentSdkClient

            return ClaudeAgentSdkClient()
        return CodexAppServerClient()

    def _register_backend_callback(self) -> None:
        register = getattr(self._backend_client, "register_permission_callback", None)
        if register is not None:
            register(self._answer_backend_callback)

    def _answer_backend_callback(self, request: Any) -> dict[str, Any] | None:
        method = str(getattr(request, "method", "") or "")
        params = getattr(request, "params", {}) or {}
        if not isinstance(params, dict):
            params = {}

        if method == "mcpServer/elicitation/request":
            server_name = str(
                params.get("serverName") or params.get("server_name") or ""
            )
            action = (
                "accept"
                if self._approval_policy == "never"
                and _is_super_agents_mcp_server(server_name)
                else "decline"
            )
            logger.warning(
                "Answering Super Agents backend MCP elicitation method=%s "
                "server=%s action=%s",
                method,
                server_name,
                action,
            )
            return {"action": action, "content": None, "_meta": None}

        if "requestApproval" in method:
            decision = "accept" if self._approval_policy == "never" else "decline"
            logger.warning(
                "Answering Super Agents backend approval callback method=%s "
                "decision=%s",
                method,
                decision,
            )
            return {"decision": decision}

        return None

    def _backend_is_codex(self) -> bool:
        return getattr(self._backend_client, "backend", "codex") == "codex"

    def _turn_developer_instructions(
        self,
        developer_instructions: str | None,
    ) -> str | None:
        parts = [
            part.strip()
            for part in (self._developer_instructions, developer_instructions)
            if part and part.strip()
        ]
        return _with_super_agent_identity_instructions(
            "\n\n".join(parts) if parts else None,
            self._super_agent_name,
            self._super_agent_agent_name,
        )

    def _thread_developer_instructions(self) -> str | None:
        return _with_super_agent_identity_instructions(
            self._developer_instructions,
            self._super_agent_name,
            self._super_agent_agent_name,
        )

    def _thread_state_file_lock(self):
        assert self._state_path is not None
        return thread_state_file_lock(self._state_path)

    def _dispatcher_reasoning_effort(self) -> str | None:
        return dispatcher_reasoning_effort(self._dispatcher_config_path)

    def _super_agents_reasoning_effort(self) -> str | None:
        return super_agents_reasoning_effort(self._dispatcher_config_path)

    def _configured_reasoning_effort(self) -> str | None:
        if self._use_super_agent_reasoning:
            return self._super_agents_reasoning_effort() or "high"
        return self._dispatcher_reasoning_effort()

    def _dispatcher_voice(self) -> dict[str, str]:
        return dispatcher_voice(self._dispatcher_config_path)

    def _load_thread_id(self) -> str | None:
        return load_thread_id(self._state_path)

    def _persist_thread_id(self, thread_id: str) -> None:
        persist_thread_id(self._state_path, thread_id)
        self._persist_voice_route_state(
            active_target_thread_id=None,
            active_target_kind=None,
            active_target_label=None,
            active_target_voice_id=None,
            active_target_voice_name=None,
        )

    def _persist_voice_route_state(
        self,
        *,
        active_target_thread_id: str | None,
        active_target_kind: str | None,
        active_target_label: str | None,
        active_target_voice_id: str | None,
        active_target_voice_name: str | None,
    ) -> None:
        persist_voice_route_state(
            self._state_path,
            dispatcher_thread_id=self._thread_id,
            dispatcher_voice=self._dispatcher_voice(),
            active_target_thread_id=active_target_thread_id,
            active_target_kind=active_target_kind,
            active_target_label=active_target_label,
            active_target_voice_id=active_target_voice_id,
            active_target_voice_name=active_target_voice_name,
        )

    def claim_speech(self, turn_id: str) -> bool:
        if turn_id in self._claimed_speech_turns:
            return False
        self._claimed_speech_turns.add(turn_id)
        self._turn_spoken_at[turn_id] = time.monotonic()
        self._prune_turn_prompt_records()
        return True

    def _record_turn_prompt(self, turn_id: str | None, prompt_hash: str) -> None:
        if not turn_id or not prompt_hash:
            return
        self._turn_prompt_hashes.setdefault(turn_id, set()).add(prompt_hash)
        self._prune_turn_prompt_records()

    def _prune_turn_prompt_records(self) -> None:
        cutoff = time.monotonic() - 4 * SPOKEN_TURN_DUPLICATE_SUPPRESSION_SECONDS
        for turn_id, spoken_at in list(self._turn_spoken_at.items()):
            if spoken_at < cutoff and turn_id != self._active_turn_id:
                self._turn_spoken_at.pop(turn_id, None)
                self._turn_prompt_hashes.pop(turn_id, None)
        while len(self._turn_prompt_hashes) > 16:
            oldest = next(iter(self._turn_prompt_hashes))
            if oldest == self._active_turn_id:
                break
            self._turn_prompt_hashes.pop(oldest, None)

    def _is_duplicate_of_spoken_turn(self, turn_id: str, prompt_hash: str) -> bool:
        spoken_at = self._turn_spoken_at.get(turn_id)
        if spoken_at is None:
            return False
        if time.monotonic() - spoken_at > SPOKEN_TURN_DUPLICATE_SUPPRESSION_SECONDS:
            return False
        return prompt_hash in self._turn_prompt_hashes.get(turn_id, set())

    def release_speech_claim(self, turn_id: str) -> None:
        self._claimed_speech_turns.discard(turn_id)

    def reset_voice_route_to_dispatcher(self) -> None:
        self.persist_voice_route(
            active_target_thread_id=None,
            active_target_kind=None,
            active_target_label=None,
            active_target_voice_id=None,
            active_target_voice_name=None,
        )

    def persist_voice_route(
        self,
        *,
        active_target_thread_id: str | None,
        active_target_kind: str | None,
        active_target_label: str | None,
        active_target_voice_id: str | None,
        active_target_voice_name: str | None,
    ) -> None:
        self._persist_voice_route_state(
            active_target_thread_id=active_target_thread_id,
            active_target_kind=active_target_kind,
            active_target_label=active_target_label,
            active_target_voice_id=active_target_voice_id,
            active_target_voice_name=active_target_voice_name,
        )


def _extract_thread_id(payload: dict[str, Any]) -> str | None:
    for key in ("threadId", "thread_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    thread = payload.get("thread")
    if isinstance(thread, dict):
        value = thread.get("id") or thread.get("threadId")
        if isinstance(value, str) and value:
            return value
    session = payload.get("session")
    if isinstance(session, dict):
        value = session.get("id") or session.get("threadId")
        if isinstance(value, str) and value:
            return value
    return None


def _extract_turn_id(payload: dict[str, Any]) -> str | None:
    if _response_is_queued(payload):
        return None
    for key in ("turnId", "turn_id"):
        value = payload.get(key)
        if isinstance(value, str) and value and not _is_queue_item_id(value):
            return value
    turn = payload.get("turn") or payload.get("item")
    if isinstance(turn, dict):
        value = turn.get("id") or turn.get("turnId")
        if isinstance(value, str) and value and not _is_queue_item_id(value):
            return value
    return None


def _response_is_queued(payload: dict[str, Any]) -> bool:
    if payload.get("queued") is True:
        return True
    for key in ("turnId", "turn_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and _is_queue_item_id(value):
            return True
    item = payload.get("item")
    if not isinstance(item, dict):
        return False
    status = str(item.get("status") or "").lower()
    item_id = item.get("id")
    return status in {"queued", "starting"} and (
        not isinstance(item_id, str) or _is_queue_item_id(item_id)
    )


def _extract_queued_id(payload: dict[str, Any]) -> str | None:
    for key in ("turnId", "turn_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and _is_queue_item_id(value):
            return value
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_id = item.get("id")
    return item_id if isinstance(item_id, str) and item_id else None


def _is_queue_item_id(value: str) -> bool:
    return value.startswith("q_")


def _speech_text_from_progress(progress: dict[str, Any]) -> str:
    from super_agents.app_formatting import find_turn_useful_text, find_useful_text

    summary = progress.get("summary")
    candidates: list[tuple[str, Any, bool]] = [
        (
            "summary.items",
            find_turn_useful_text(summary.get("items")) if isinstance(summary, dict) else None,
            True,
        ),
        ("progress.turn.lastUsefulMessage", _last_useful_message(progress.get("turn")), True),
        ("progress.turns.lastUsefulMessage", _last_useful_message(progress.get("turns")), True),
        (
            "progress.recentTurns.lastUsefulMessage",
            _last_useful_message(progress.get("recentTurns")),
            True,
        ),
        ("progress.turn", find_turn_useful_text(progress.get("turn")), True),
        ("progress.turns", find_turn_useful_text(progress.get("turns")), True),
        ("progress.recentTurns", find_turn_useful_text(progress.get("recentTurns")), True),
    ]
    candidates.extend(
        [
            ("progress.lastUsefulMessage", progress.get("lastUsefulMessage"), True),
        ]
    )
    if isinstance(summary, dict):
        candidates.extend(
            [
                ("summary.lastUsefulMessage", summary.get("lastUsefulMessage"), True),
            ]
        )
    for source, candidate, role_selected in candidates:
        text = str(candidate).strip() if role_selected and isinstance(candidate, str) else find_useful_text(candidate)
        if not text:
            continue
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        if _should_ignore_speech_text(text, progress):
            logger.info(
                "%s stage=speech_candidate_rejected source=%s text_len=%d text_hash=%s",
                DISPATCH_TIMING_LOG,
                source,
                len(text),
                text_hash,
            )
            continue
        logger.info(
            "%s stage=speech_candidate_selected source=%s text_len=%d text_hash=%s",
            DISPATCH_TIMING_LOG,
            source,
            len(text),
            text_hash,
        )
        return _speech_excerpt(text)
    return ""


def _last_useful_message(value: Any, depth: int = 0) -> str | None:
    if value is None or depth > 6:
        return None
    if isinstance(value, list):
        for item in reversed(value):
            if result := _last_useful_message(item, depth + 1):
                return result
        return None
    if not isinstance(value, dict):
        return None
    text = value.get("lastUsefulMessage")
    if isinstance(text, str) and text.strip():
        return text.strip()
    for key in ("turn", "turns", "recentTurns", "summary"):
        if result := _last_useful_message(value.get(key), depth + 1):
            return result
    return None


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
    should_wait = (
        now - started_at < TURN_POLL_COMPLETED_EMPTY_SPEECH_GRACE_SECONDS
    )
    if should_wait:
        logger.info(
            "%s stage=turn_wait_completed_without_speech turn_id=%s "
            "status=%s retrying=True",
            DISPATCH_TIMING_LOG,
            turn_id,
            status,
        )
    return should_wait, started_at


def _should_ignore_speech_text(text: str, progress: dict[str, Any]) -> bool:
    normalized = _normalize_speech_candidate(text)
    if _looks_like_schema_label(normalized):
        return True
    if _looks_like_metadata_identifier(normalized):
        return True
    if _looks_like_timestamp(normalized):
        return True
    return normalized in _user_message_texts(progress)


def _user_message_texts(value: Any, depth: int = 0) -> set[str]:
    if value is None or depth > 8:
        return set()
    if isinstance(value, list):
        texts: set[str] = set()
        for item in value:
            texts.update(_user_message_texts(item, depth + 1))
        return texts
    if not isinstance(value, dict):
        return set()

    item_type = str(value.get("type") or value.get("role") or "").lower()
    if item_type in {"user", "usermessage"}:
        if text := _text_content(value.get("text") or value.get("content")):
            return {_normalize_speech_candidate(text)}

    texts: set[str] = set()
    for key, child in value.items():
        normalized_key = "".join(char for char in str(key).lower() if char.isalnum())
        if normalized_key in {"prompt", "promptpreview"}:
            if text := _text_content(child):
                texts.add(_normalize_speech_candidate(text))
            continue
        texts.update(_user_message_texts(child, depth + 1))
    return texts


def _text_content(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [
            item.get("text", "")
            for item in value
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return " ".join(part for part in parts if part).strip() or None
    return None


def _normalize_speech_candidate(text: str) -> str:
    return text.strip().rstrip(".!?").strip().lower()


def _looks_like_metadata_identifier(text: str) -> bool:
    compact = text.replace("-", "").replace(" ", "")
    if len(compact) >= 16 and re.fullmatch(r"[0-9a-f]+", compact):
        return True
    return bool(re.fullmatch(r"(?:[0-9a-f]{4,}[-\s]+){2,}[0-9a-f]{4,}", text))


def _looks_like_timestamp(text: str) -> bool:
    lowered = text.replace(" dot ", ".").replace(" z", "z")
    compact = re.sub(r"\s+", "", lowered)
    return bool(
        re.fullmatch(
            r"\d{4}[-/]?\d{2}[-/]?\d{2}t?\d{2}:?\d{2}(?::?\d{2})?(?:\.\d+)?z?",
            compact,
        )
    )


def _looks_like_schema_label(text: str) -> bool:
    compact = text.replace(" ", "").replace("_", "").replace("-", "")
    return compact in {
        "agentmessage",
        "assistantmessage",
        "usermessage",
        "toolcall",
        "functioncall",
        "completed",
        "running",
        "waiting",
        "queued",
    }


def _progress_has_pending_requests(progress: dict[str, Any]) -> bool:
    if progress.get("pendingRequests"):
        return True
    summary = progress.get("summary")
    if isinstance(summary, dict) and summary.get("pendingRequestCount"):
        return True
    tracked = progress.get("trackedTurn")
    if isinstance(tracked, dict):
        if tracked.get("pendingRequestCount"):
            return True
        pending = tracked.get("pendingRequests")
        if isinstance(pending, list) and pending:
            return True
    return False

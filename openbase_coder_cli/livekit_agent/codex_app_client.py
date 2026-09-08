from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from super_agents.app_endpoint import parse_app_server_endpoint

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
from openbase_coder_cli.livekit_agent.codex_transport import CodexTransportMixin
from openbase_coder_cli.livekit_agent.codex_turns import (
    LIVEKIT_DUPLICATE_TURN_GRACE_SECONDS,
    _active_turn_id_mismatch,
    _ActiveTurn,
    _is_no_active_turn_error,
    _is_turn_cannot_accept_steering_error,
    _normalize_prompt,
    _prompt_debug_fields,
    _StartingTurn,
    _super_agent_name,
    _with_super_agent_identity_instructions,
)
from openbase_coder_cli.livekit_agent.codex_turns import (
    _speech_excerpt as _turn_speech_excerpt,
)
from openbase_coder_cli.livekit_agent.codex_turns import (
    _undelivered_suffix as _turn_undelivered_suffix,
)
from openbase_coder_cli.paths import CODEX_DISPATCHER_CONFIG_PATH

logger = logging.getLogger(__name__)
DISPATCH_TIMING_LOG = "dispatch_timing"

DEFAULT_CODEX_MODEL = "gpt-5.5"
_SANDBOX_POLICY_TYPES = {
    "read-only": "readOnly",
    "workspace-write": "workspaceWrite",
    "danger-full-access": "dangerFullAccess",
}

_undelivered_suffix = _turn_undelivered_suffix
_speech_excerpt = _turn_speech_excerpt


def _model_name_for_role(
    path: Path | None = None,
    *,
    use_super_agent_model: bool = False,
) -> str:
    if use_super_agent_model:
        return super_agents_model(path) or DEFAULT_CODEX_MODEL
    return dispatcher_model(path) or DEFAULT_CODEX_MODEL


class CodexAppServerClient(CodexTransportMixin):
    """Shared websocket client for a single long-lived Codex thread."""

    def __init__(
        self,
        *,
        ws_url: str,
        cwd: str,
        state_path: str | None = None,
        developer_instructions: str | None = None,
        approval_policy: str = DEFAULT_CODEX_APPROVAL_POLICY,
        sandbox: str = DEFAULT_CODEX_SANDBOX,
        model_name: str | None = None,
        service_tier: str = "standard",
        dispatcher_config_path: str | Path | None = None,
        persist_thread: bool = True,
        initial_thread_id: str | None = None,
        super_agent_name: str | None = None,
        super_agent_agent_name: str | None = None,
        use_super_agent_reasoning: bool = False,
    ) -> None:
        self._ws_url = ws_url
        self._endpoint = parse_app_server_endpoint(ws_url)
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
        self._super_agent_name = _super_agent_name(super_agent_name)
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

        self._ws: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._thread_id = initial_thread_id or (
            self._load_thread_id() if persist_thread else None
        )
        self._thread_loaded = False
        self._starting_turn: _StartingTurn | None = None
        self._active_turn: _ActiveTurn | None = None
        self._claimed_speech_turns: set[str] = set()

        self._request_id = 0
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._turn_start_lock = asyncio.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    def set_super_agent_name(self, name: str | None) -> None:
        self._super_agent_name = _super_agent_name(name)

    def set_super_agent_agent_name(self, name: str | None) -> None:
        self._super_agent_agent_name = _super_agent_name(name)

    async def aclose(self) -> None:
        async with self._state_lock:
            reader_task = self._reader_task
            self._reader_task = None
            ws = self._ws
            self._ws = None

        if reader_task is not None:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

        if ws is not None:
            await ws.close()

    async def run_turn(
        self,
        prompt: str,
        *,
        developer_instructions: str | None = None,
    ) -> dict[str, Any]:
        """Run a Codex turn, steering an active turn when the user speaks again."""
        dispatch_id = f"voice-{uuid.uuid4().hex[:12]}"
        dispatch_started = time.monotonic()
        joined_turn: _ActiveTurn | None = None
        wait_turn: _ActiveTurn | None = None

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

            existing_turn = self._active_turn
            incoming_debug = _prompt_debug_fields(prompt)
            if existing_turn is None:
                logger.info(
                    "LiveKit Codex prompt received with no active turn "
                    "prompt_hash=%s prompt_len=%s prompt_excerpt=%r",
                    incoming_debug["hash"],
                    incoming_debug["length"],
                    incoming_debug["excerpt"],
                )
            else:
                active_debug = _prompt_debug_fields(existing_turn.prompt)
                logger.info(
                    "LiveKit Codex prompt received with active turn "
                    "turn_id=%s turn_age_ms=%d delivered_chars=%d agent_messages=%d "
                    "incoming_hash=%s incoming_len=%s incoming_excerpt=%r "
                    "active_hash=%s active_len=%s active_excerpt=%r",
                    existing_turn.turn_id,
                    int((time.monotonic() - existing_turn.started_at) * 1000),
                    len(existing_turn.delivered_text),
                    len(existing_turn.agent_messages or []),
                    incoming_debug["hash"],
                    incoming_debug["length"],
                    incoming_debug["excerpt"],
                    active_debug["hash"],
                    active_debug["length"],
                    active_debug["excerpt"],
                )

            if existing_turn is None and self._starting_turn is not None:
                starting_turn = self._starting_turn
                if self._should_join_starting_turn(starting_turn, prompt):
                    logger.info(
                        "Joining in-flight Codex turn start instead of starting duplicate "
                        "during LiveKit duplicate-turn grace period prompt_hash=%s",
                        incoming_debug["hash"],
                    )
                    joined_turn = await self._materialize_starting_turn_locked(
                        starting_turn
                    )
                else:
                    existing_turn = await self._materialize_starting_turn_locked(
                        starting_turn
                    )

            if existing_turn is not None:
                if self._should_join_existing_turn(existing_turn, prompt):
                    logger.info(
                        "Joining active Codex turn %s instead of interrupting "
                        "during LiveKit duplicate-turn grace period prompt_hash=%s",
                        existing_turn.turn_id,
                        incoming_debug["hash"],
                    )
                    joined_turn = existing_turn
                else:
                    logger.info(
                        "Steering active Codex turn %s prompt_hash=%s",
                        existing_turn.turn_id,
                        incoming_debug["hash"],
                    )
                    if await self._steer_turn(thread_id, existing_turn, prompt):
                        joined_turn = existing_turn

            if joined_turn is None:
                reasoning_effort = self._configured_reasoning_effort()
                turn_params = {
                    "threadId": thread_id,
                    "cwd": self._cwd,
                    "model": self._model_name,
                    "approvalPolicy": self._approval_policy,
                    "sandboxPolicy": self._sandbox_policy(),
                    "serviceTier": self._service_tier,
                    "input": [{"type": "text", "text": prompt}],
                }
                if reasoning_effort:
                    turn_params["effort"] = reasoning_effort
                if (
                    effective_developer_instructions
                    := self._turn_developer_instructions(developer_instructions)
                ):
                    turn_params["collaborationMode"] = {
                        "mode": "default",
                        "settings": {
                            "model": self._model_name,
                            "reasoning_effort": reasoning_effort or "high",
                            "developer_instructions": effective_developer_instructions,
                        },
                    }
                logger.info(
                    "%s stage=turn_start_request dispatch_id=%s thread_id=%s "
                    "model=%s service_tier=%s reasoning_effort=%s prompt_hash=%s "
                    "prompt_len=%s elapsed_ms=%d",
                    DISPATCH_TIMING_LOG,
                    dispatch_id,
                    thread_id,
                    self._model_name,
                    self._service_tier,
                    reasoning_effort or "app-server-default",
                    incoming_debug["hash"],
                    incoming_debug["length"],
                    int((time.monotonic() - dispatch_started) * 1000),
                )
                self._starting_turn = _StartingTurn(
                    prompt=prompt,
                    started_at=time.monotonic(),
                    dispatch_id=dispatch_id,
                    task=asyncio.create_task(
                        self._send_request(
                            "turn/start",
                            turn_params,
                        )
                    ),
                )
                wait_turn = await self._materialize_starting_turn_locked(
                    self._starting_turn
                )

        if joined_turn is not None:
            wait_turn = joined_turn

        assert wait_turn is not None
        result = await asyncio.shield(wait_turn.completed)
        logger.info(
            "%s stage=voice_turn_result dispatch_id=%s turn_id=%s status=%s "
            "elapsed_ms=%d speech_chars=%d",
            DISPATCH_TIMING_LOG,
            wait_turn.dispatch_id or dispatch_id,
            wait_turn.turn_id,
            result.get("status"),
            int((time.monotonic() - wait_turn.started_at) * 1000),
            len(str(result.get("_livekit_speech_text") or "")),
        )
        return result

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

    async def _materialize_starting_turn_locked(
        self,
        starting_turn: _StartingTurn,
    ) -> _ActiveTurn:
        try:
            result = await asyncio.shield(starting_turn.task)
        except Exception:
            if self._starting_turn is starting_turn:
                self._starting_turn = None
            raise

        if self._active_turn is not None:
            if self._starting_turn is starting_turn:
                self._starting_turn = None
            return self._active_turn

        turn = result["turn"]
        turn_id = turn["id"]
        completed: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._active_turn = _ActiveTurn(
            turn_id=turn_id,
            completed=completed,
            prompt=starting_turn.prompt,
            started_at=starting_turn.started_at,
            dispatch_id=starting_turn.dispatch_id,
        )
        logger.info(
            "%s stage=turn_start_response dispatch_id=%s turn_id=%s elapsed_ms=%d",
            DISPATCH_TIMING_LOG,
            starting_turn.dispatch_id,
            turn_id,
            int((time.monotonic() - starting_turn.started_at) * 1000),
        )
        if self._starting_turn is starting_turn:
            self._starting_turn = None
        return self._active_turn

    def _should_join_existing_turn(self, turn: _ActiveTurn, prompt: str) -> bool:
        if turn.completed.done():
            return False

        turn_age = time.monotonic() - turn.started_at
        if turn_age > LIVEKIT_DUPLICATE_TURN_GRACE_SECONDS:
            return False

        return (
            _normalize_prompt(prompt) == _normalize_prompt(turn.prompt)
            and not turn.delivered_text
            and not turn.agent_messages
        )

    def _should_join_starting_turn(self, turn: _StartingTurn, prompt: str) -> bool:
        turn_age = time.monotonic() - turn.started_at
        if turn_age > LIVEKIT_DUPLICATE_TURN_GRACE_SECONDS:
            return False

        return _normalize_prompt(prompt) == _normalize_prompt(turn.prompt)

    async def _steer_turn(
        self,
        thread_id: str,
        turn: _ActiveTurn,
        prompt: str,
    ) -> bool:
        prompt_debug = _prompt_debug_fields(prompt)
        try:
            await self._send_request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": turn.turn_id,
                    "input": [{"type": "text", "text": prompt}],
                },
            )
        except RuntimeError as exc:
            actual_turn_id = _active_turn_id_mismatch(exc)
            if actual_turn_id:
                logger.warning(
                    "Codex active turn id drifted during steering; resyncing "
                    "expected_turn_id=%s actual_turn_id=%s prompt_hash=%s",
                    turn.turn_id,
                    actual_turn_id,
                    prompt_debug["hash"],
                )
                self._remap_active_turn(turn, actual_turn_id)
                await self._send_request(
                    "turn/steer",
                    {
                        "threadId": thread_id,
                        "expectedTurnId": actual_turn_id,
                        "input": [{"type": "text", "text": prompt}],
                    },
                )
                logger.info(
                    "Submitted Codex turn steering after resync turn_id=%s prompt_hash=%s prompt_len=%s",
                    actual_turn_id,
                    prompt_debug["hash"],
                    prompt_debug["length"],
                )
                return True

            if _is_turn_cannot_accept_steering_error(exc):
                logger.warning(
                    "Codex turn %s could not accept steering; leaving active turn running "
                    "prompt_hash=%s",
                    turn.turn_id,
                    prompt_debug["hash"],
                )
                return True

            if not _is_no_active_turn_error(exc):
                raise
            logger.info(
                "Codex turn %s was already inactive during steering prompt_hash=%s",
                turn.turn_id,
                prompt_debug["hash"],
            )
            if not turn.completed.done():
                turn.completed.set_result(
                    {
                        "id": turn.turn_id,
                        "status": "completed",
                        "error": None,
                    }
                )
            if self._active_turn is turn:
                self._active_turn = None
            return False

        logger.info(
            "Submitted Codex turn steering turn_id=%s prompt_hash=%s prompt_len=%s",
            turn.turn_id,
            prompt_debug["hash"],
            prompt_debug["length"],
        )
        return True

    def _remap_active_turn(self, turn: _ActiveTurn, turn_id: str) -> None:
        if turn.turn_id == turn_id:
            return

        turn.turn_id = turn_id

    async def prepare(self) -> str:
        """Open the Codex websocket and ensure the shared thread is ready."""
        return await self._ensure_thread()

    async def _ensure_thread(self) -> str:
        async with self._state_lock:
            await self._ensure_connected_locked()

            if self._state_path is None:
                return await self._ensure_thread_without_persistence_locked()

            with self._thread_state_file_lock():
                canonical_thread_id = self._load_thread_id()
                if canonical_thread_id and canonical_thread_id != self._thread_id:
                    logger.info(
                        "Adopting canonical Codex LiveKit thread from disk "
                        "previous_thread_id=%s canonical_thread_id=%s",
                        self._thread_id,
                        canonical_thread_id,
                    )
                    self._thread_id = canonical_thread_id
                    self._thread_loaded = False

                if self._thread_loaded and self._thread_id:
                    return self._thread_id

                if self._thread_id:
                    failed_thread_id = self._thread_id
                    try:
                        return await self._resume_thread_locked(failed_thread_id)
                    except Exception:
                        logger.warning(
                            "Failed to resume persisted Codex thread %s",
                            failed_thread_id,
                            exc_info=True,
                        )
                        self._thread_id = None
                        self._thread_loaded = False

                    canonical_thread_id = self._load_thread_id()
                    if canonical_thread_id and canonical_thread_id != failed_thread_id:
                        logger.info(
                            "Retrying newer canonical Codex LiveKit thread "
                            "failed_thread_id=%s canonical_thread_id=%s",
                            failed_thread_id,
                            canonical_thread_id,
                        )
                        try:
                            return await self._resume_thread_locked(canonical_thread_id)
                        except Exception:
                            logger.warning(
                                "Failed to resume canonical Codex thread %s; "
                                "creating a new dispatcher thread",
                                canonical_thread_id,
                                exc_info=True,
                            )
                            self._thread_id = None
                            self._thread_loaded = False

                started = await self._send_request_locked(
                    "thread/start",
                    self._thread_params(),
                )
                self._thread_id = started["thread"]["id"]
                self._thread_loaded = True
                self._persist_thread_id(self._thread_id)
                logger.info("Started Codex LiveKit thread %s", self._thread_id)
                return self._thread_id

    async def _ensure_thread_without_persistence_locked(self) -> str:
        if self._thread_loaded and self._thread_id:
            return self._thread_id

        if self._thread_id:
            try:
                return await self._resume_thread_locked(self._thread_id)
            except Exception:
                logger.warning(
                    "Failed to resume Codex thread %s; creating a new one",
                    self._thread_id,
                    exc_info=True,
                )
                self._thread_id = None
                self._thread_loaded = False

        started = await self._send_request_locked(
            "thread/start",
            self._thread_params(),
        )
        self._thread_id = started["thread"]["id"]
        self._thread_loaded = True
        logger.info("Started Codex LiveKit thread %s", self._thread_id)
        return self._thread_id

    async def _resume_thread_locked(self, thread_id: str) -> str:
        resumed = await self._send_request_locked(
            "thread/resume",
            self._thread_params(thread_id=thread_id),
        )
        self._thread_id = resumed["thread"]["id"]
        self._thread_loaded = True
        self._persist_thread_id(self._thread_id)
        return self._thread_id

    def _thread_state_file_lock(self):
        assert self._state_path is not None
        return thread_state_file_lock(self._state_path)

    def _thread_params(self, *, thread_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": self._cwd,
            "model": self._model_name,
            "approvalPolicy": self._approval_policy,
            "sandbox": self._sandbox,
        }
        if developer_instructions := _with_super_agent_identity_instructions(
            self._developer_instructions,
            self._super_agent_name,
            self._super_agent_agent_name,
        ):
            params["developerInstructions"] = developer_instructions
        if thread_id:
            params["threadId"] = thread_id
        return params

    def _sandbox_policy(self) -> dict[str, str]:
        sandbox_type = _SANDBOX_POLICY_TYPES.get(self._sandbox)
        if sandbox_type is None:
            raise ValueError(
                "Unsupported LiveKit Codex sandbox "
                f"{self._sandbox!r}; expected one of {sorted(_SANDBOX_POLICY_TYPES)}"
            )
        return {"type": sandbox_type}

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

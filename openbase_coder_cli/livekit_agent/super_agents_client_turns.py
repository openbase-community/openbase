"""Turn dispatch, steering, queueing and dedup mixin for the LiveKit client.

`SuperAgentsClientTurnsMixin` holds the methods that start, steer, queue, join
and reap backend turns, plus the spoken-turn dedup bookkeeping. It is a pure
structural extraction from `SuperAgentsLiveKitClient`; every method is unchanged
and reaches sibling state/behavior through ``self``. Methods that read the
test-patched ``TURN_POLL_*`` / ``ORPHANED_RESULT_GRACE_SECONDS`` constants or the
Claude auth-heal helpers stay in the top-level client module so those module
globals resolve (and monkeypatch) exactly as before.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from super_agents.app_protocol import (
    extract_queued_id as _extract_queued_id,
)
from super_agents.app_protocol import (
    extract_started_turn_id as _extract_turn_id,
)
from super_agents.app_protocol import (
    is_queue_item_id as _is_queue_item_id,
)
from super_agents.app_protocol import (
    response_is_queued as _response_is_queued,
)

from openbase_coder_cli.livekit_agent.codex_turns import (
    _active_turn_id_mismatch,
    _is_no_active_turn_error,
    _is_turn_cannot_accept_steering_error,
    _prompt_debug_fields,
)
from openbase_coder_cli.livekit_agent.super_agents_client_common import (
    DISPATCH_TIMING_LOG,
    SPOKEN_TURN_DUPLICATE_SUPPRESSION_SECONDS,
    logger,
)
from openbase_coder_cli.livekit_agent.super_agents_speech import (
    _speech_text_from_progress,
)

if TYPE_CHECKING:
    from openbase_coder_cli.livekit_agent.super_agents_client import (
        SuperAgentsLiveKitClient,
    )


class SuperAgentsClientTurnsMixin:
    """Turn dispatch, steering, queueing and spoken-turn dedup."""

    def _clear_active_turn_state(self) -> None:
        self._active_turn_id = None
        self._active_turn_started_at = None
        self._active_turn_dispatch_id = None
        self._active_turn_prompt_hash = None
        self._active_turn_wait_task = None
        self._active_turn_wait_task_turn_id = None

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

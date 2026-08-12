"""Turn dispatch, steering, interrupt and local-turn-state mixin.

`SessionManagerTurnsMixin` groups the start/queue/steer/interrupt turn methods
plus the locally tracked prompt/steer/queue bookkeeping. Pure structural
extraction; every method is unchanged and reaches sibling state through
``self``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from super_agents.app_models import LabelQueryInput
from super_agents.app_server_client import (
    extract_turn_id,
    find_latest_turn,
)

from .models import QueuedTurnInfo
from .models import ThreadInfo as SessionInfo
from .models import TurnSteerInfo as SteerInfo
from .session_manager_base import (
    _TRACKED_TURN_TEXT_LIMIT,
    _is_thread_unavailable_error,
    _runtime_error_message,
    _with_dispatcher_onboarding_reminder,
    logger,
)
from .thread_payloads import (
    _timestamp_to_datetime,
)


class SessionManagerTurnsMixin:
    """Turn dispatch, steering, interrupt and local-turn-state bookkeeping."""

    async def start_turn(self, thread_id: str, prompt: str) -> str:
        """Start a new Codex turn on an existing thread."""
        return await self.send_message(thread_id, prompt)

    async def queue_turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        """Queue a follow-up turn after the active turn, or start immediately if idle."""
        thread = await self.get_session_state(thread_id)
        if thread is None:
            raise ValueError(f"Thread {thread_id} not found")
        if not thread.directory:
            raise ValueError(f"Thread {thread_id} is missing its cwd")

        prompt = _with_dispatcher_onboarding_reminder(thread_id, prompt)

        turn_input = {
            "prompt": prompt,
            "cwd": thread.directory,
            **self._codex_permission_defaults(),
        }
        if model := self._model_for_thread(thread):
            turn_input["model"] = model

        result = await self._client.queue_turn_by_label(
            LabelQueryInput(thread_id=thread_id, cwd=thread.directory),
            turn_input,
        )
        if not result.get("queued"):
            turn_id = extract_turn_id(result)
            if turn_id:
                async with self._state_lock:
                    self._turn_to_session[turn_id] = thread_id
                    self._delivered_text[turn_id] = ""
                    self._remember_turn_prompt_locked(turn_id, prompt)
        await self._broadcast_thread_state(thread_id)
        return result

    async def steer_turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        """Send steering input to the active turn on a thread."""
        thread = await self.get_session_state(thread_id)
        if thread is None:
            raise ValueError(f"Thread {thread_id} not found")
        if not thread.directory:
            raise ValueError(f"Thread {thread_id} is missing its cwd")

        turn_id = await self._active_turn_id(thread_id)
        if turn_id is None:
            raise ValueError(f"Thread {thread_id} has no active turn to steer")

        result = await self._client.steer_by_label(
            LabelQueryInput(
                thread_id=thread_id,
                cwd=thread.directory,
                turn_id=turn_id,
                prefer="latest_active",
            ),
            prompt,
            {"cwd": thread.directory},
        )
        resolved_turn_id = extract_turn_id(result) or turn_id
        # steer_by_label can fall back to starting or queueing a fresh turn
        # when the resolved turn is no longer steerable.
        steered = not result.get("queued") and not result.get("startedImmediately")
        async with self._state_lock:
            self._turn_to_session[resolved_turn_id] = thread_id
            self._delivered_text.setdefault(resolved_turn_id, "")
            if steered:
                self._remember_turn_steer_locked(
                    resolved_turn_id,
                    SteerInfo(text=prompt, created_at=datetime.now(UTC)),
                )
            else:
                self._remember_turn_prompt_locked(resolved_turn_id, prompt)
        await self._broadcast_thread_state(thread_id)
        return {**result, "turn_id": resolved_turn_id, "steered": steered}

    async def interrupt_turn(self, thread_id: str) -> bool:
        """Interrupt the current turn on a thread."""
        return await self.interrupt_run(thread_id)

    async def interrupt_run(self, session_id: str) -> bool:
        """Interrupt the current turn in a thread."""
        if self._uses_backend_session_api():
            try:
                result = await self._client.cancel_by_label(
                    LabelQueryInput(thread_id=session_id)
                )
            except RuntimeError as exc:
                message = _runtime_error_message(exc).lower()
                if _is_thread_unavailable_error(exc) or "no active" in message:
                    return False
                raise
            return bool(result.get("cancelled", True))

        turn_id = await self._active_turn_id(session_id)
        if turn_id is None:
            return False
        try:
            await self._client.cancel_turn(session_id, turn_id)
        except RuntimeError as exc:
            message = _runtime_error_message(exc).lower()
            if _is_thread_unavailable_error(exc) or "no active" in message:
                return False
            raise
        return True

    async def _active_turn_id(self, session_id: str) -> str | None:
        local_turn_id: str | None = None
        async with self._state_lock:
            for turn_id, candidate_session_id in self._turn_to_session.items():
                if candidate_session_id == session_id:
                    local_turn_id = turn_id
                    break

        thread = await self._read_thread(session_id, include_turns=True)
        if thread is not None:
            turn = find_latest_turn(thread, active_only=True)
            if turn and isinstance(turn.get("id"), str):
                return turn["id"]
            if local_turn_id is not None:
                async with self._state_lock:
                    self._forget_turn_locked(local_turn_id)
            return None
        return local_turn_id

    async def _apply_local_turn_state(
        self, session_id: str, session: SessionInfo
    ) -> None:
        """Overlay locally tracked prompts, steers, and the pending turn queue.

        The app-server can lag behind locally initiated actions — a turn's
        userMessage items (including steering input) may not be readable while
        the turn is in flight — so locally tracked state fills those gaps.
        """
        async with self._state_lock:
            for run in [session.current_run, *session.run_history]:
                if run is None:
                    continue
                if not run.message:
                    run.message = self._turn_prompt.get(run.run_id, "")
                tracked_steers = self._turn_steers.get(run.run_id)
                if tracked_steers:
                    known = {steer.text.strip() for steer in run.steers}
                    run.steers = run.steers + [
                        steer
                        for steer in tracked_steers
                        if steer.text.strip() not in known
                    ]
        session.queued_turns = self._queued_turns_for_thread(session_id)

    def _queued_turns_for_thread(self, thread_id: str) -> list[QueuedTurnInfo]:
        summary_method = getattr(self._client, "queued_turn_summary", None)
        if not callable(summary_method):
            return []
        try:
            summaries = summary_method()
        except Exception:
            logger.debug(
                "Unable to read queued turns for thread %s", thread_id, exc_info=True
            )
            return []
        queued: list[QueuedTurnInfo] = []
        for summary in summaries:
            if not isinstance(summary, dict) or summary.get("threadId") != thread_id:
                continue
            for item in summary.get("items", []):
                if not isinstance(item, dict):
                    continue
                input_data = (
                    item.get("inputData")
                    if isinstance(item.get("inputData"), dict)
                    else {}
                )
                prompt = str(
                    input_data.get("prompt") or item.get("promptPreview") or ""
                )
                if not prompt:
                    continue
                queued_at_raw = item.get("queuedAt")
                queued.append(
                    QueuedTurnInfo(
                        queue_id=str(item.get("id")) if item.get("id") else None,
                        prompt=prompt,
                        queued_at=(
                            _timestamp_to_datetime(queued_at_raw)
                            if queued_at_raw
                            else None
                        ),
                    )
                )
        return queued

    def _remember_turn_prompt_locked(self, turn_id: str, prompt: str) -> None:
        self._turn_prompt.setdefault(turn_id, prompt)
        while len(self._turn_prompt) > _TRACKED_TURN_TEXT_LIMIT:
            self._turn_prompt.pop(next(iter(self._turn_prompt)))

    def _remember_turn_steer_locked(self, turn_id: str, steer: SteerInfo) -> None:
        self._turn_steers.setdefault(turn_id, []).append(steer)
        while len(self._turn_steers) > _TRACKED_TURN_TEXT_LIMIT:
            self._turn_steers.pop(next(iter(self._turn_steers)))

    def _forget_turn_locked(self, turn_id: str) -> None:
        # _turn_prompt and _turn_steers survive turn completion on purpose:
        # they keep history display correct while the app-server payload still
        # lacks the turn's userMessage items. They are capped instead.
        self._turn_to_session.pop(turn_id, None)
        self._delivered_text.pop(turn_id, None)
        self._turn_current_item.pop(turn_id, None)
        for item_key in [
            item_key for item_key in self._delivered_item_text if item_key[0] == turn_id
        ]:
            self._delivered_item_text.pop(item_key, None)

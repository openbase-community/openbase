"""Compatibility delivery for Claude clients predating turn notifications."""

from __future__ import annotations

import asyncio

from .models import ThreadStatus


class ClaudeEventsMixin:
    def _watch_legacy_claude_thread(self, thread_id: str) -> None:
        if self._execution_backend != "claude_code" or getattr(
            self._client, "supports_turn_notifications", False
        ):
            return
        existing = self._claude_watchers.get(thread_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._watch_claude_state(thread_id))
        self._claude_watchers[thread_id] = task
        task.add_done_callback(lambda done: self._claude_watchers.pop(thread_id, None))

    async def _watch_claude_state(self, thread_id: str) -> None:
        from .session_manager import _broadcast

        previous = None
        terminal_turns: set[str] = set()
        # Compatibility only, bounded even if a legacy client stays waiting.
        deadline = asyncio.get_running_loop().time() + 7200
        while asyncio.get_running_loop().time() < deadline:
            state = await self.get_session_state(thread_id)
            if state is None:
                return
            payload = state.model_dump(mode="json")
            if payload != previous:
                await _broadcast(thread_id, {"type": "thread_state", "data": payload})
                previous = payload
            run = state.current_run or (
                state.run_history[-1] if state.run_history else None
            )
            if (
                run is not None
                and run.status not in {ThreadStatus.running, ThreadStatus.waiting}
                and run.run_id not in terminal_turns
            ):
                terminal_turns.add(run.run_id)
                await self._handle_client_event(
                    "turn/failed"
                    if run.status == ThreadStatus.error
                    else "turn/completed",
                    {
                        "threadId": thread_id,
                        "turnId": run.run_id,
                        "error": {
                            "message": run.accumulated_stderr
                            or "The agent turn failed."
                        },
                    },
                )
            if (
                run is None
                or run.status not in {ThreadStatus.running, ThreadStatus.waiting}
            ) and not state.queued_turns:
                return
            await asyncio.sleep(0.5)

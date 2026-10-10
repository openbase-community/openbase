"""Require worker intent and receipts instead of hoping for optional tool calls.

The model decides whether the user's task is quiet and writes its own truthful
completion. Hooks enforce ordering; only the terminal-turn callback may submit
the completion. No text classifier guesses quiet intent or fabricates a result.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

from openbase_coder_cli.livekit_announcer import resolve_announcer_room

from .ledger import AnnouncementLedger, turn_revision
from .speech import submit_speech

TOOL_NAME = "mcp__openbase_agent__task_announcement"
MAX_CORRECTIONS = 3
PROTOCOL_INSTRUCTIONS = (
    "Only turns started or steered through the delegation tools use this managed "
    "task_announcement protocol. Direct conversation is exempt; the tool reports "
    "direct_reply for those turns. For delegated work: "
    "Before work, call it with phase=begin and delivery=audible. If the user "
    "explicitly requests silence, text only, or no notifications, use delivery=quiet "
    "and quote that instruction in quiet_request. Preserve quiet intent through "
    "follow-up steering. Do not infer silence from read-only work. The tool owns "
    "one introduction; do not also run user say for the default hello. After all "
    "task tools return, call phase=finish with a brief, truthful summary of their "
    "actual result, including your name. The runtime submits it only after the "
    "turn's work completes. Quiet tasks still call finish but never speak. Do not "
    "claim submission means playback. Explicit requests for separate speech may "
    "still use user say. Direct <voice> turns use normal replies and are exempt."
)


@dataclass(frozen=True)
class TaskContext:
    session: object
    turn: object
    revision: str
    prompts: tuple[str, ...]
    direct: bool


class AnnouncementProtocol:
    def __init__(
        self,
        store,
        ledger: AnnouncementLedger,
        *,
        publish=submit_speech,
        resolve_room=resolve_announcer_room,
    ):
        self.store = store
        self.ledger = ledger
        self.publish = publish
        self.resolve_room = resolve_room
        self._locks: dict[str, asyncio.Lock] = {}

    async def context(self, thread_id: str) -> TaskContext:
        session = await asyncio.to_thread(self.store.get_session, thread_id)
        if not session.active_turn_id:
            raise RuntimeError("No active worker task; announcement suppressed.")
        turn = await asyncio.to_thread(self.store.get_turn, session.active_turn_id)
        if turn.status not in {"running", "waiting"}:
            raise RuntimeError(
                "Worker task is no longer active; announcement suppressed."
            )
        prompts = (turn.prompt, *(str(s.get("text", "")) for s in turn.steers))
        revision = turn_revision(turn)
        # This is the product's explicit voice envelope, not a natural-language
        # matcher for questions, names, or silence instructions.
        latest = prompts[-1].strip()
        direct = (
            "<voice>" in latest and "</voice>" in latest
        ) or not await asyncio.to_thread(self.ledger.is_delegated, turn.id)
        return TaskContext(session, turn, revision, prompts, direct)

    def _task(self, state, context):
        task = state["turn"]
        if task.get("id") != context.turn.id:
            task = state["turn"] = {
                "id": context.turn.id,
                "revision": context.revision,
                "delivery": None,
                "pending": [],
                "corrections": 0,
            }
        elif task["revision"] != context.revision:
            # A steer invalidates an earlier completion, even when it arrived
            # after finish was proposed but before the terminal result.
            task.update(revision=context.revision, delivery=None, corrections=0)
            task.pop("finish", None)
            task.pop("completion", None)
        if context.direct:
            task["delivery"] = "direct"
            task.pop("finish", None)
        return task

    async def _edit(self, context, operation):
        def guarded(state):
            session = self.store.get_session(context.session.id)
            turn = self.store.get_turn(context.turn.id)
            if (
                session.active_turn_id != turn.id
                or turn.status not in {"running", "waiting"}
                or turn_revision(turn) != context.revision
            ):
                raise RuntimeError(
                    "Task changed; stale announcement operation suppressed."
                )
            return operation(state, self._task(state, context))

        return await asyncio.to_thread(self.ledger.edit, context.session.id, guarded)

    async def announce(self, thread_id: str, arguments: dict) -> dict:
        async with self._locks.setdefault(thread_id, asyncio.Lock()):
            context = await self.context(thread_id)
            if context.direct:
                return {
                    "status": "direct_reply",
                    "detail": "Use your normal voice response.",
                }
            phase = arguments.get("phase")
            if phase == "begin":
                return await self._begin(context, arguments)
            if phase == "finish":
                return await self._finish(context, arguments)
            raise ValueError("phase must be begin or finish")

    async def _begin(self, context, arguments):
        delivery = arguments.get("delivery")
        quiet = str(arguments.get("quiet_request") or "").strip()
        if delivery not in {"audible", "quiet"}:
            raise ValueError("Choose audible or quiet delivery before starting work.")

        room_name = await self.resolve_room() if delivery == "audible" else None

        def decide(state, task):
            if task.get("fatal"):
                raise RuntimeError(task["fatal"])
            if delivery == "quiet":
                inherited = task.get("quiet_request") or state.get("last_quiet_request")
                if not quiet or not (
                    any(quiet in prompt for prompt in context.prompts)
                    or quiet == inherited
                ):
                    raise ValueError("Quote the user's explicit quiet request exactly.")
                task["quiet_request"] = quiet
                state["last_quiet_request"] = quiet
            elif task.get("quiet_request"):
                resume = str(arguments.get("resume_request") or "").strip()
                if not resume or resume not in context.prompts[-1]:
                    raise ValueError(
                        "Quiet intent persists; quote the user's request to resume speech."
                    )
                task.pop("quiet_request")
                state.pop("last_quiet_request", None)
            if task.get("delivery") == delivery and (
                delivery == "quiet" or state["intro"].get("status") == "submitted"
            ):
                return False
            task["delivery"] = delivery
            if delivery == "audible":
                task.setdefault("room_name", room_name)
            task.pop("finish", None)
            intro = state["intro"]
            if delivery == "quiet" or intro.get("status") == "submitted":
                return False
            if intro.get("status") in {"submitting", "failed"}:
                raise RuntimeError(
                    "Earlier introduction submission is uncertain or failed; no automatic repeat."
                )
            intro.update(
                status="submitting", message_id=_message_id(context.session.id, "intro")
            )
            return True

        should_introduce = await self._edit(context, decide)
        if should_introduce:
            await self._submit(
                context,
                "intro",
                f"Hi, I'm {context.session.agent_name}.",
                _message_id(context.session.id, "intro"),
            )
        return {
            "status": "ready",
            "delivery": delivery,
            "agent_name": context.session.agent_name,
            "playback_verified": False,
        }

    async def _finish(self, context, arguments):
        summary = str(arguments.get("summary") or "").strip()
        if not summary or len(summary) > 2000:
            raise ValueError(
                "Provide a truthful completion summary of at most 2000 characters."
            )

        def finish(state, task):
            if task.get("delivery") not in {"audible", "quiet"}:
                raise ValueError(
                    "Call begin first and honor any explicit quiet request."
                )
            if (
                task.get("delivery") == "audible"
                and state["intro"].get("status") != "submitted"
            ):
                raise RuntimeError(
                    "The introduction has no submission receipt; completion cannot proceed."
                )
            if task["pending"]:
                raise ValueError(
                    "Work is still running. Wait for every tool result before finish."
                )
            if task.get("fatal"):
                raise RuntimeError(task["fatal"])
            task["finish"] = {"summary": summary, "revision": context.revision}

        await self._edit(context, finish)
        return {
            "status": "completion_prepared",
            "detail": "Not yet submitted; waits for terminal work.",
        }

    async def before_tool(self, thread_id, event):
        context = await self.context(thread_id)
        if event.get("tool_name") == TOOL_NAME:
            return {}

        def check(state, task):
            if task.get("delivery") not in {"audible", "quiet", "direct"}:
                return self._correction(
                    task,
                    "Call task_announcement with phase=begin before work; choose quiet only for an explicit user request.",
                )
            if (
                task["delivery"] == "audible"
                and state["intro"].get("status") != "submitted"
            ):
                return self._correction(
                    task,
                    "The introduction was not submitted. Do not claim it succeeded.",
                )
            task.pop("finish", None)
            tool_id = event["tool_use_id"]
            if tool_id not in task["pending"]:
                task["pending"].append(tool_id)
            return None

        failure = await self._edit(context, check)
        if failure:
            return self._hook_failure(failure, "PreToolUse")
        return {}

    async def after_tool(self, thread_id, event):
        context = await self.context(thread_id)

        def settle(state, task):
            if event.get("tool_use_id") in task["pending"]:
                task["pending"].remove(event["tool_use_id"])

        await self._edit(context, settle)
        return {}

    async def stop(self, thread_id):
        context = await self.context(thread_id)

        def check(state, task):
            if task.get("delivery") == "direct":
                return None
            if not task.get("finish") or task["pending"]:
                return self._correction(
                    task,
                    "Complete the task_announcement begin/finish protocol. Wait for work and report its actual result; honor quiet requests.",
                )
            return None

        failure = await self._edit(context, check)
        return self._hook_failure(failure, "Stop") if failure else {}

    @staticmethod
    def _correction(task, reason):
        task["corrections"] += 1
        if task["corrections"] >= MAX_CORRECTIONS:
            task["fatal"] = (
                "Worker announcement protocol was not completed after bounded corrections."
            )
        return (reason, task.get("fatal"))

    @staticmethod
    def _hook_failure(failure, event):
        reason, fatal = failure
        if fatal:
            return {"continue_": False, "stopReason": fatal}
        if event == "Stop":
            return {"decision": "block", "reason": reason}
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    async def validate_turn_result(self, session, turn, result):
        async with self._locks.setdefault(session.id, asyncio.Lock()):
            context = await self.context(session.id)
            if context.turn.id != turn.id:
                raise RuntimeError(
                    "Worker task was superseded; no completion submitted."
                )
            if context.direct:
                return

            def validate(state, task):
                if task.get("fatal"):
                    raise RuntimeError(task["fatal"])
                if task["pending"] or not task.get("finish"):
                    raise RuntimeError(
                        "Work returned without a verified announcement protocol receipt."
                    )
                if task.get("delivery") == "quiet":
                    task["completion"] = {"status": "suppressed_quiet"}
                    return None
                if task.get("completion", {}).get("status") == "submitted":
                    return None
                if task.get("completion", {}).get("status") in {"submitting", "failed"}:
                    raise RuntimeError(
                        "Completion submission is uncertain or failed; no automatic repeat."
                    )
                task["completion"] = {"status": "submitting"}
                return task["finish"]["summary"]

            summary = await self._edit(context, validate)
            if summary is not None:
                await self._submit(
                    context,
                    "completion",
                    summary,
                    _message_id(turn.id, context.revision),
                )

    async def _submit(self, context, phase, text, message_id):
        def room(state, task):
            return task.get("room_name")

        attempted = False
        try:
            room_name = await self._edit(context, room)
            await asyncio.to_thread(
                self.ledger.edit,
                "delivery:" + message_id,
                lambda state: state.update(
                    store_path=str(self.store.path),
                    thread_id=context.session.id,
                    turn_id=context.turn.id,
                    revision=context.revision,
                    cancelled=False,
                ),
            )
            current = await self.context(context.session.id)
            if (
                current.turn.id != context.turn.id
                or current.revision != context.revision
            ):
                raise RuntimeError(
                    "Task changed before speech submission; stale announcement suppressed."
                )
            attempted = True
            receipt = await self.publish(context.session, text, message_id, room_name)
            if receipt.get("status") not in {"published", "notification_submitted"}:
                raise RuntimeError(
                    "Announcement publisher returned no submission receipt."
                )
        except (Exception, asyncio.CancelledError) as exc:
            error = str(exc) or "cancelled"

            await self._record_submission(
                context,
                phase,
                message_id,
                status="failed" if attempted else "not_submitted",
                error=error,
            )
            await asyncio.to_thread(
                self.ledger.edit,
                "delivery:" + message_id,
                lambda state: state.update(cancelled=True),
            )
            raise

        await self._record_submission(
            context, phase, message_id, status="submitted", receipt=receipt
        )

    async def _record_submission(self, context, phase, message_id, **fields):
        # Recording a late acknowledgement must never roll the task back to an
        # old turn/revision after a steer or cancellation.
        def record(state):
            task = state["turn"]
            if phase == "intro":
                target = state["intro"]
            elif (
                task.get("id") == context.turn.id
                and task.get("revision") == context.revision
            ):
                target = task.setdefault("completion", {})
            else:
                return
            target.update(message_id=message_id, **fields)

        await asyncio.to_thread(self.ledger.edit, context.session.id, record)


def _message_id(key, phase):
    return (
        "announcer-managed-"
        + hashlib.sha256(f"{key}:{phase}".encode()).hexdigest()[:32]
    )

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from super_agents.app_endpoint import open_app_server_connection

from openbase_coder_cli.livekit_agent.codex_turns import _ActiveTurn, _speech_excerpt

logger = logging.getLogger("openbase_coder_cli.livekit_agent.codex_app_client")
DISPATCH_TIMING_LOG = "dispatch_timing"


class CodexTransportMixin:
    async def _ensure_connected_locked(self) -> None:
        if self._ws is not None:
            return

        self._ws = await open_app_server_connection(
            self._endpoint,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            **({"open_timeout": 5} if self._endpoint.is_unix else {}),
        )
        self._reader_task = asyncio.create_task(self._reader_loop())

        await self._send_request_locked(
            "initialize",
            {
                "clientInfo": {
                    "name": "openbase-livekit",
                    "title": "Openbase LiveKit",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._send_notification_locked("initialized", {})
        logger.info(
            "Connected to Codex app-server over %s at %s",
            self._endpoint.transport,
            self._endpoint.description,
        )

    async def _reader_loop(self) -> None:
        assert self._ws is not None

        try:
            async for raw_message in self._ws:
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning(
                        "Ignoring non-JSON Codex app-server message: %s", raw_message
                    )
                    continue

                request_id = message.get("id")
                method = message.get("method")
                if request_id is not None and method is None:
                    future = self._pending_requests.pop(int(request_id), None)
                    if future is None:
                        continue
                    if future.done():
                        logger.debug(
                            "Dropping Codex app-server response for completed request id=%s",
                            request_id,
                        )
                        continue
                    error = message.get("error")
                    if error:
                        future.set_exception(RuntimeError(json.dumps(error)))
                    else:
                        future.set_result(message["result"])
                    continue

                params = message.get("params", {})
                if method is not None:
                    if request_id is not None:
                        await self._handle_server_request(request_id, method, params)
                    else:
                        await self._handle_notification(method, params)
        except Exception as exc:
            logger.error("Codex app-server reader failed", exc_info=True)
            self._fail_outstanding(exc)
        finally:
            async with self._state_lock:
                self._ws = None
                self._reader_task = None
                self._thread_loaded = False

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "item/agentMessage/delta":
            return

        if method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage":
                turn_id = params.get("turnId")
                text = item.get("text", "")
                if turn_id and text:
                    self._record_agent_message(turn_id, text)
            elif item.get("type"):
                turn_id = params.get("turnId")
                active_turn = self._active_turn
                if active_turn is not None and active_turn.turn_id == turn_id:
                    logger.info(
                        "%s stage=item_completed dispatch_id=%s turn_id=%s "
                        "item_type=%s elapsed_ms=%d",
                        DISPATCH_TIMING_LOG,
                        active_turn.dispatch_id,
                        active_turn.turn_id,
                        item.get("type"),
                        int((time.monotonic() - active_turn.started_at) * 1000),
                    )
            return

        if method == "turn/completed":
            turn = params.get("turn", {})
            turn_id = turn.get("id")
            if not turn_id:
                return

            active_turn = self._active_turn
            if active_turn is not None and active_turn.turn_id == turn_id:
                completed_turn = dict(turn)
                spoken_text = self._speech_text_for_turn(active_turn)
                if spoken_text:
                    completed_turn["_livekit_speech_text"] = spoken_text
                    completed_turn["_livekit_turn_id"] = active_turn.turn_id
                    active_turn.delivered_text = spoken_text
                if not active_turn.completed.done():
                    active_turn.completed.set_result(completed_turn)
                logger.info(
                    "%s stage=turn_completed dispatch_id=%s turn_id=%s status=%s "
                    "elapsed_ms=%d agent_messages=%d speech_chars=%d",
                    DISPATCH_TIMING_LOG,
                    active_turn.dispatch_id,
                    active_turn.turn_id,
                    turn.get("status"),
                    int((time.monotonic() - active_turn.started_at) * 1000),
                    len(active_turn.agent_messages or []),
                    len(spoken_text),
                )
                self._active_turn = None

    async def _handle_server_request(
        self,
        request_id: Any,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if method == "mcpServer/elicitation/request":
            server_name = params.get("serverName") or params.get("server_name")
            action = (
                "accept"
                if self._approval_policy == "never" and server_name == "bedside-alarm"
                else "decline"
            )
            logger.warning(
                "Answering Codex app-server MCP elicitation method=%s server=%s action=%s",
                method,
                server_name,
                action,
            )
            await self._send_response(
                request_id,
                {"action": action, "content": None, "_meta": None},
            )
            return

        if "requestApproval" in method:
            decision = "accept" if self._approval_policy == "never" else "decline"
            logger.warning(
                "Answering Codex app-server approval callback method=%s decision=%s",
                method,
                decision,
            )
            await self._send_response(request_id, {"decision": decision})
            return

        logger.warning(
            "Rejecting unsupported Codex app-server request method=%s", method
        )
        await self._send_error_response(
            request_id, -32601, f"method not found: {method}"
        )

    def _record_agent_message(self, turn_id: str, text: str) -> None:
        active_turn = self._active_turn
        if active_turn is None or active_turn.turn_id != turn_id:
            return

        if active_turn.agent_messages is None:
            active_turn.agent_messages = []
            logger.info(
                "%s stage=first_agent_message dispatch_id=%s turn_id=%s "
                "elapsed_ms=%d chars=%d",
                DISPATCH_TIMING_LOG,
                active_turn.dispatch_id,
                active_turn.turn_id,
                int((time.monotonic() - active_turn.started_at) * 1000),
                len(text),
            )
        active_turn.agent_messages.append(text)

    def _speech_text_for_turn(self, active_turn: _ActiveTurn) -> str:
        if not active_turn.agent_messages:
            return ""

        return _speech_excerpt(active_turn.agent_messages[-1])

    def claim_speech(self, turn_id: str) -> bool:
        if turn_id in self._claimed_speech_turns:
            return False
        self._claimed_speech_turns.add(turn_id)
        return True

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

    def _fail_outstanding(self, exc: Exception) -> None:
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(exc)
        self._pending_requests.clear()

        active_turn = self._active_turn
        if active_turn is not None and not active_turn.completed.done():
            active_turn.completed.set_exception(exc)
        self._active_turn = None

    async def _send_request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._state_lock:
            await self._ensure_connected_locked()
            return await self._send_request_locked(method, params)

    async def _send_request_locked(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        assert self._ws is not None

        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        async with self._send_lock:
            self._request_id += 1
            request_id = self._request_id
            self._pending_requests[request_id] = future
            await self._ws.send(
                json.dumps(
                    {
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
            )

        return await future

    async def _send_notification_locked(
        self, method: str, params: dict[str, Any]
    ) -> None:
        assert self._ws is not None
        async with self._send_lock:
            await self._ws.send(json.dumps({"method": method, "params": params}))

    async def _send_response(self, request_id: Any, result: dict[str, Any]) -> None:
        assert self._ws is not None
        async with self._send_lock:
            await self._ws.send(json.dumps({"id": request_id, "result": result}))

    async def _send_error_response(
        self, request_id: Any, code: int, message: str
    ) -> None:
        assert self._ws is not None
        async with self._send_lock:
            await self._ws.send(
                json.dumps(
                    {
                        "id": request_id,
                        "error": {"code": code, "message": message},
                    }
                )
            )

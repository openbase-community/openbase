"""Thread lifecycle, backend selection, config and persistence mixin.

`SuperAgentsClientThreadsMixin` groups the connection/lifecycle side of the
LiveKit client: starting and resuming the backend thread, choosing the backend
client, answering backend permission callbacks, reading dispatcher config, and
persisting the voice-route state. Pure structural extraction from
`SuperAgentsLiveKitClient`; every method is unchanged and reaches sibling state
through ``self``.
"""

from __future__ import annotations

import re
from typing import Any

from super_agents.app_protocol import (
    extract_thread_id as _extract_thread_id,
)

from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
)
from openbase_coder_cli.backend_config import (
    configured_execution_backend as _configured_execution_backend,
)
from openbase_coder_cli.dispatcher_config import (
    dispatcher_reasoning_effort,
    dispatcher_voice,
    super_agents_reasoning_effort,
)
from openbase_coder_cli.livekit_agent.codex_thread_state import (
    load_thread_id,
    persist_thread_id,
    persist_voice_route_state,
    thread_state_file_lock,
)
from openbase_coder_cli.livekit_agent.codex_turns import (
    _super_agent_name,
    _with_super_agent_identity_instructions,
)
from openbase_coder_cli.livekit_agent.super_agents_client_common import (
    DEFAULT_DISPATCHER_LABEL,
    logger,
)


def _is_super_agents_mcp_server(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return normalized in {"super-agents", "mcp-super-agents"}


class SuperAgentsClientThreadsMixin:
    """Thread lifecycle, backend selection, config and persistence."""

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
            resume_kwargs: dict[str, Any] = {}
            if developer_instructions := self._thread_developer_instructions():
                # The dispatcher instruction file is the single source of
                # truth; replace on resume so template updates propagate
                # instead of the session keeping stale instructions forever.
                resume_kwargs = {
                    "developer_instructions": developer_instructions,
                    "replace_developer_instructions": True,
                }
            resumed = await self._backend_client.resume_by_label(
                self._query(thread_id=thread_id, prefer="latest_any"),
                **resume_kwargs,
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
        if not self._backend_is_codex():
            # Reasoning levels are Codex-only; Claude effort follows the
            # service tier (Fast mode), so never forward the stored setting.
            return None
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

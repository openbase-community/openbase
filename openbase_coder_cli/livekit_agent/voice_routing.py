"""Routing of the LiveKit voice session between the dispatcher and Super Agents."""

import logging
import time
import uuid
from pathlib import Path

from openbase_coder_cli.dispatcher_config import super_agents_service_tier
from openbase_coder_cli.livekit_agent.config import (
    LIVEKIT_CODEX_APPROVAL_POLICY,
    LIVEKIT_CODEX_SANDBOX,
    LIVEKIT_DISPATCHER_CONFIG_PATH,
    PROACTIVE_STEER_PROMPT_CACHE_SECONDS,
)
from openbase_coder_cli.livekit_agent.packets import (
    AnnouncerMessage,
    VoiceRouteCommand,
)
from openbase_coder_cli.livekit_agent.speech_queue import AnnouncerSpeechQueue
from openbase_coder_cli.livekit_agent.super_agents_client import (
    SuperAgentsLiveKitClient,
)
from openbase_coder_cli.livekit_agent.text_normalization import normalized_text_hash
from openbase_coder_cli.livekit_agent.voice_delivery import (
    VoiceDeliveryLedger,
    VoiceRouteSnapshot,
)
from openbase_coder_cli.livekit_agent.voices import stable_super_agent_voice

logger = logging.getLogger(__name__)


class LiveKitVoiceRouter:
    def __init__(
        self,
        dispatcher_client,
        *,
        delivery_ledger: VoiceDeliveryLedger | None = None,
    ) -> None:
        self._dispatcher_client = dispatcher_client
        self._active_client = dispatcher_client
        self._target_clients: dict[str, SuperAgentsLiveKitClient] = {}
        self._active_target_voice_id: str | None = None
        self._active_target_voice_name: str | None = None
        self._proactive_steer_prompt_hashes: dict[str, float] = {}
        self._orphaned_result_handler = None
        self._route_version = 0
        self.delivery_ledger = delivery_ledger

    def set_orphaned_result_handler(self, handler) -> None:
        """Deliver completed turn answers that no voice dispatch consumed."""
        self._orphaned_result_handler = handler
        for client in (self._dispatcher_client, *self._target_clients.values()):
            set_handler = getattr(client, "set_orphaned_result_handler", None)
            if callable(set_handler):
                set_handler(handler)

    @property
    def active_client(self):
        return self._active_client

    @property
    def is_dispatcher_active(self) -> bool:
        return self._active_client is self._dispatcher_client

    @property
    def active_target_voice_id(self) -> str | None:
        return self._active_target_voice_id

    @property
    def active_target_voice_name(self) -> str | None:
        return self._active_target_voice_name

    def route_snapshot(self) -> VoiceRouteSnapshot:
        thread_id = getattr(self._active_client, "_thread_id", "") or ""
        active_route = "dispatcher" if self.is_dispatcher_active else "codex_thread"
        return VoiceRouteSnapshot(
            route_version=self._route_version,
            active_thread_id=thread_id,
            active_voice_id=self._active_target_voice_id,
            active_voice_name=self._active_target_voice_name,
            active_route=active_route,
        )

    def exit_to_dispatch(self) -> None:
        self._active_client = self._dispatcher_client
        self._active_target_voice_id = None
        self._active_target_voice_name = None
        self._route_version += 1
        self._dispatcher_client.reset_voice_route_to_dispatcher()
        logger.info(
            "dispatch_timing stage=voice_route_changed action=exit_to_dispatch "
            "route_version=%d active_thread_id=%s",
            self._route_version,
            getattr(self._active_client, "_thread_id", "") or "",
        )

    async def transfer_to_thread(
        self,
        *,
        thread_id: str,
        cwd: str,
        label: str | None,
        voice_id: str | None = None,
        voice_name: str | None = None,
    ) -> None:
        target_voice = stable_super_agent_voice(thread_id, label)
        target_voice_id = voice_id or (target_voice.voice_id if target_voice else None)
        target_voice_name = voice_name or (target_voice.name if target_voice else None)
        target_client = self._target_clients.get(thread_id)
        if target_client is None:
            target_client = SuperAgentsLiveKitClient(
                cwd=cwd,
                state_path=None,
                approval_policy=LIVEKIT_CODEX_APPROVAL_POLICY,
                sandbox=LIVEKIT_CODEX_SANDBOX,
                service_tier=super_agents_service_tier(
                    Path(LIVEKIT_DISPATCHER_CONFIG_PATH)
                ),
                persist_thread=False,
                initial_thread_id=thread_id,
                super_agent_name=label,
                super_agent_agent_name=target_voice_name,
                use_super_agent_reasoning=True,
            )
            if self._orphaned_result_handler is not None:
                target_client.set_orphaned_result_handler(self._orphaned_result_handler)
            self._target_clients[thread_id] = target_client
        else:
            target_client.set_super_agent_name(label)
            target_client.set_super_agent_agent_name(target_voice_name)
        await target_client.prepare()
        self._active_client = target_client
        self._active_target_voice_id = target_voice_id
        self._active_target_voice_name = target_voice_name
        self._route_version += 1
        self._dispatcher_client.persist_voice_route(
            active_target_thread_id=thread_id,
            active_target_kind="codex_thread",
            active_target_label=label,
            active_target_voice_id=target_voice_id,
            active_target_voice_name=target_voice_name,
        )
        logger.info(
            "dispatch_timing stage=voice_route_changed action=transfer_to_thread "
            "route_version=%d active_thread_id=%s active_voice_id=%s "
            "active_voice_name=%s",
            self._route_version,
            thread_id,
            target_voice_id or "",
            target_voice_name or "",
        )

    def claim_speech(self, client, turn_id: str) -> bool:
        return self._active_client is client and client.claim_speech(turn_id)

    def can_deliver_for_snapshot(self, snapshot: VoiceRouteSnapshot) -> bool:
        return snapshot.same_route(self.route_snapshot())

    def mark_proactive_steer(self, prompt: str) -> None:
        # Normalized hashing so a steered transcript also matches its
        # formatted/unformatted STT twin, not just the exact same text.
        self._prune_proactive_steer_prompt_hashes()
        self._proactive_steer_prompt_hashes[normalized_text_hash(prompt)] = (
            time.monotonic()
        )

    def should_skip_proactively_steered_prompt(self, prompt: str) -> bool:
        prompt = prompt.strip()
        if not prompt:
            return False
        has_active_prompt = getattr(self._active_client, "has_active_prompt", None)
        if callable(has_active_prompt) and has_active_prompt(prompt):
            return False
        self._prune_proactive_steer_prompt_hashes()
        prompt_hash = normalized_text_hash(prompt)
        if not prompt_hash:
            return False
        return self._proactive_steer_prompt_hashes.pop(prompt_hash, None) is not None

    def _prune_proactive_steer_prompt_hashes(self) -> None:
        cutoff = time.monotonic() - PROACTIVE_STEER_PROMPT_CACHE_SECONDS
        stale_hashes = [
            prompt_hash
            for prompt_hash, created_at in self._proactive_steer_prompt_hashes.items()
            if created_at < cutoff
        ]
        for prompt_hash in stale_hashes:
            self._proactive_steer_prompt_hashes.pop(prompt_hash, None)

    async def close(self) -> None:
        for client in self._target_clients.values():
            await client.aclose()
        # The dispatcher client must close too: the claude_code backend keeps
        # a Claude CLI subprocess attached to the shared dispatcher session,
        # and leaving it connected past the room lets it flush buffered
        # transcript entries after another worker's turns — the next resume
        # then forks the dispatcher conversation from a stale leaf.
        await self._dispatcher_client.aclose()


async def _transfer_voice_route(
    voice_router: LiveKitVoiceRouter,
    route_command: VoiceRouteCommand,
    announcer_queue: AnnouncerSpeechQueue,
) -> None:
    assert route_command.thread_id is not None
    assert route_command.cwd is not None
    try:
        await voice_router.transfer_to_thread(
            thread_id=route_command.thread_id,
            cwd=route_command.cwd,
            label=route_command.label,
            voice_id=route_command.active_target_voice_id,
            voice_name=route_command.active_target_voice_name,
        )
    except Exception:
        logger.warning("Unable to transfer LiveKit voice route", exc_info=True)
        voice_router.exit_to_dispatch()
        announcer_queue.enqueue(
            AnnouncerMessage(
                message_id=f"voice-route-{uuid.uuid4().hex}",
                text="Unable to transfer voice route.",
            )
        )
        return

    announcer_queue.enqueue(
        AnnouncerMessage(
            message_id=f"voice-route-{uuid.uuid4().hex}",
            text="Voice route transferred.",
            voice_id=voice_router.active_target_voice_id,
        )
    )

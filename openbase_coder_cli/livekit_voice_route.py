from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import livekit.api as livekit_api

from openbase_coder_cli.cli.utils import get_data_dir
from openbase_coder_cli.codex_session_defaults import codex_permission_defaults
from openbase_coder_cli.dispatcher_config import (
    dispatcher_voice,
    selected_tts_provider_id,
)
from openbase_coder_cli.livekit_agent.codex_thread_state import thread_state_file_lock
from openbase_coder_cli.livekit_announcer import (
    AnnouncerError,
    _build_livekit_client,
    _resolve_target_room,
)
from openbase_coder_cli.livekit_voice_history import (
    get_voice_history_entry,
    record_voice_assignment,
)
from openbase_coder_cli.paths import (
    CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH,
    CODEX_DISPATCHER_INSTRUCTIONS_PATH,
)
from openbase_coder_cli.tts_providers import (
    CARTESIA_PROVIDER_ID,
    DEFAULT_CARTESIA_ANNOUNCER_VOICE_ID,
    DEFAULT_CARTESIA_VOICE_ID,
    get_tts_provider,
    voice_name_for_id,
)

VOICE_ROUTE_TOPIC = "openbase.voice.route"
VOICE_ROUTE_STATE_FILE = "livekit-voice-route.json"
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", DEFAULT_CARTESIA_VOICE_ID)
CARTESIA_ANNOUNCER_VOICE_ID = os.getenv(
    "CARTESIA_ANNOUNCER_VOICE_ID", DEFAULT_CARTESIA_ANNOUNCER_VOICE_ID
)
DISPATCHER_THREAD_LABELS = frozenset({"dispatcher"})

DIRECT_LIVEKIT_INSTRUCTIONS_PATH_ENV = (
    "LIVEKIT_DIRECT_CODEX_DEVELOPER_INSTRUCTIONS_PATH"
)
DIRECT_LIVEKIT_INSTRUCTIONS_TEXT_ENV = "LIVEKIT_DIRECT_CODEX_DEVELOPER_INSTRUCTIONS"
DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS = """
You are receiving direct user speech from a LiveKit voice session.
Keep final spoken responses concise and directly useful.
If a transcript clearly appears to be background conversation and the user is not addressing Openbase Coder, immediately run `openbase-coder user ios mute` and do not otherwise respond to the transcript. Do not auto-mute ambiguous transcripts.
Do not read code, logs, stack traces, JSON, diffs, or long file paths aloud unless explicitly asked.
When code or logs matter, summarize their practical meaning in plain English.
If transcription is unclear, ask the user to confirm the intended request before acting.
When the user asks to return to dispatch, or you need to hand the voice session
back to dispatch, run:
openbase-coder exit-to-dispatch
Do not assume dispatcher responsibilities, delegation policy, or Super Agents coordination rules from these instructions.
""".strip()
DISPATCHER_BUILTIN_DEVELOPER_INSTRUCTIONS = """
You are the Openbase Coder LiveKit dispatcher for a private voice session.
Route voice sessions when the user asks to speak with an agent.
When creating or referring to a Super Agent for a thread name, derive the
agent's speaking name with:
openbase-coder super-agent-name "<thread name>"
When creating a Super Agent, pass that speaking name as the thread's agentName.
When the user asks to transfer to an agent by name, run:
openbase-coder user transfer-to-agent "<agent name>"
When the user asks to transfer by thread id, run:
openbase-coder user transfer-to-thread "<thread id>"
Keep spoken confirmations concise.
""".strip()


class VoiceRouteError(AnnouncerError):
    """Base error for LiveKit voice route commands."""


class VoiceRouteBlockedError(VoiceRouteError):
    """The requested route change would violate instruction isolation."""


@dataclass(frozen=True)
class CartesiaVoice:
    voice_id: str
    name: str
    provider: str = CARTESIA_PROVIDER_ID


@dataclass(frozen=True)
class VoiceRouteState:
    dispatcher_thread_id: str | None
    dispatcher_voice_id: str
    dispatcher_voice_name: str
    active_target_thread_id: str | None
    active_target_kind: str | None
    active_target_label: str | None
    active_target_voice_id: str | None
    active_target_voice_name: str | None
    updated_at: float | None
    instruction_override_supported: bool = False

    @property
    def active_route(self) -> str:
        return "target" if self.active_target_thread_id else "dispatcher"


@dataclass(frozen=True)
class VoiceRoutePublishResult:
    command_id: str
    room_name: str
    agent_identities: tuple[str, ...]
    state: VoiceRouteState


def live_target_transfer_blocker() -> str:
    return ""


def instruction_override_supported() -> bool:
    return True


def _voices_from_ids(voice_ids) -> tuple[CartesiaVoice, ...]:
    provider = get_tts_provider(CARTESIA_PROVIDER_ID)
    return tuple(
        CartesiaVoice(
            voice_id=voice_id,
            name=provider.voice_for_id(voice_id).name
            if provider.voice_for_id(voice_id)
            else f"Voice {index + 1}",
        )
        for index, voice_id in enumerate(voice_ids)
    )


SUPER_AGENT_VOICE_IDS = tuple(
    voice.id for voice in get_tts_provider(CARTESIA_PROVIDER_ID).super_agent_voices()
)
SUPER_AGENT_VOICES = _voices_from_ids(SUPER_AGENT_VOICE_IDS)


def _current_super_agent_voices() -> tuple[CartesiaVoice, ...]:
    provider = get_tts_provider(selected_tts_provider_id())
    if provider.provider_id != CARTESIA_PROVIDER_ID:
        return tuple(
            CartesiaVoice(
                voice_id=voice.id,
                name=voice.name,
                provider=provider.provider_id,
            )
            for voice in provider.super_agent_voices()
        )
    voice_ids = tuple(voice.voice_id for voice in SUPER_AGENT_VOICES)
    if voice_ids == tuple(SUPER_AGENT_VOICE_IDS):
        return SUPER_AGENT_VOICES
    return tuple(
        CartesiaVoice(
            voice_id=voice.id,
            name=voice.name,
            provider=provider.provider_id,
        )
        for voice in provider.super_agent_voices()
    )


def stable_super_agent_voice(
    thread_id: str | None,
    label: str | None = None,
) -> CartesiaVoice | None:
    voices = _current_super_agent_voices()
    if not voices:
        return None
    key = (thread_id or label or "").strip()
    if not key:
        return None
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "big") % len(voices)
    return voices[index]


def stable_super_agent_voice_id(
    thread_id: str | None,
    label: str | None = None,
) -> str | None:
    voice = stable_super_agent_voice(thread_id, label)
    return voice.voice_id if voice else None


def super_agent_voice_for_agent_name(agent_name: str | None) -> CartesiaVoice | None:
    normalized = _normalize_voice_name(agent_name)
    if not normalized:
        return None

    for voice in _current_super_agent_voices():
        if _normalize_voice_name(voice.name) == normalized:
            return voice

    provider = get_tts_provider(selected_tts_provider_id())
    provider_voice = (
        provider.super_agent_voice_for_name(agent_name)
        if provider.provider_id != CARTESIA_PROVIDER_ID
        else provider.voice_for_name(agent_name)
    )
    if provider_voice:
        return CartesiaVoice(
            voice_id=provider_voice.id,
            name=provider_voice.name,
            provider=provider.provider_id,
        )
    return None


def super_agent_voice_for_context(
    thread_id: str | None,
    label: str | None = None,
    agent_name: str | None = None,
) -> CartesiaVoice | None:
    if is_dispatcher_identity(thread_id, label, agent_name):
        voice = dispatcher_voice()
        return CartesiaVoice(
            voice_id=voice["id"],
            name=voice["name"],
            provider=voice.get("provider", CARTESIA_PROVIDER_ID),
        )
    return super_agent_voice_for_agent_name(agent_name) or stable_super_agent_voice(
        thread_id,
        label,
    )


def super_agent_voice_id_for_context(
    thread_id: str | None,
    label: str | None = None,
    agent_name: str | None = None,
) -> str | None:
    voice = super_agent_voice_for_context(thread_id, label, agent_name)
    return voice.voice_id if voice else None


def _normalize_voice_name(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.casefold().split())


def is_dispatcher_identity(
    thread_id: str | None = None,
    label: str | None = None,
    agent_name: str | None = None,
    route_state: VoiceRouteState | None = None,
) -> bool:
    route_state = route_state or get_livekit_voice_route_state()
    if thread_id and route_state.dispatcher_thread_id == thread_id:
        return True
    return (
        _normalize_voice_name(label) in DISPATCHER_THREAD_LABELS
        or _normalize_voice_name(agent_name) == "dispatcher"
    )


def load_direct_livekit_developer_instructions(
    *,
    env: dict[str, str] | None = None,
    default_path: Path | None = None,
) -> str:
    values = env if env is not None else os.environ
    explicit_path = values.get(DIRECT_LIVEKIT_INSTRUCTIONS_PATH_ENV, "").strip()
    if explicit_path:
        loaded = _read_instruction_file(Path(explicit_path).expanduser())
        if loaded:
            return loaded

    loaded = _read_instruction_file(
        default_path or CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH
    )
    if loaded:
        return loaded

    text = values.get(DIRECT_LIVEKIT_INSTRUCTIONS_TEXT_ENV, "").strip()
    if text:
        return text

    return DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS


async def prepare_target_thread_for_direct_livekit(
    *,
    thread_id: str,
    directory: str,
) -> None:
    from openbase_coder_cli.mcp.session_manager import get_session_manager

    await get_session_manager().resume_thread_with_developer_instructions(
        thread_id,
        directory,
        load_direct_livekit_developer_instructions(),
    )


def get_livekit_voice_route_state() -> VoiceRouteState:
    return _state_from_payload(_read_json(_route_state_path()) or {})


def set_dispatcher_thread_id(thread_id: str) -> VoiceRouteState:
    state = get_livekit_voice_route_state()
    next_state = VoiceRouteState(
        dispatcher_thread_id=thread_id,
        dispatcher_voice_id=state.dispatcher_voice_id,
        dispatcher_voice_name=state.dispatcher_voice_name,
        active_target_thread_id=state.active_target_thread_id,
        active_target_kind=state.active_target_kind,
        active_target_label=state.active_target_label,
        active_target_voice_id=state.active_target_voice_id,
        active_target_voice_name=state.active_target_voice_name,
        updated_at=time.time(),
        instruction_override_supported=instruction_override_supported(),
    )
    _write_state(next_state)
    record_voice_assignment(
        thread_id=thread_id,
        agent_name="dispatcher",
        cwd=None,
        voice_id=next_state.dispatcher_voice_id,
        voice_name=next_state.dispatcher_voice_name,
        kind="dispatcher",
        source="route_state",
        seen_at=next_state.updated_at,
    )
    return next_state


def reset_voice_route_to_dispatcher() -> VoiceRouteState:
    state = get_livekit_voice_route_state()
    voice = dispatcher_voice()
    next_state = VoiceRouteState(
        dispatcher_thread_id=state.dispatcher_thread_id,
        dispatcher_voice_id=voice["id"],
        dispatcher_voice_name=voice["name"],
        active_target_thread_id=None,
        active_target_kind=None,
        active_target_label=None,
        active_target_voice_id=None,
        active_target_voice_name=None,
        updated_at=time.time(),
        instruction_override_supported=instruction_override_supported(),
    )
    _write_state(next_state)
    record_voice_assignment(
        thread_id=next_state.dispatcher_thread_id,
        agent_name="dispatcher",
        cwd=None,
        voice_id=next_state.dispatcher_voice_id,
        voice_name=next_state.dispatcher_voice_name,
        kind="dispatcher",
        source="route_state",
        seen_at=next_state.updated_at,
    )
    return next_state


def clear_livekit_thread_state() -> dict:
    """Remove persisted LiveKit dispatcher thread state.

    The running LiveKit agent keeps the dispatcher Codex client in memory, so
    callers that need a truly fresh dispatcher should restart the agent after
    clearing these files.
    """
    previous_state = get_livekit_voice_route_state()
    paths = (_route_state_path(), _stale_legacy_thread_state_path())
    removed_paths: list[str] = []

    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        removed_paths.append(str(path))

    return {
        "previous_dispatcher_thread_id": previous_state.dispatcher_thread_id,
        "previous_active_target_thread_id": previous_state.active_target_thread_id,
        "removed_paths": removed_paths,
    }


def prepare_livekit_dispatcher_recreation() -> dict:
    """Drop the persisted dispatcher thread id while preserving voice config."""
    route_path = _route_state_path()
    stale_legacy_path = _stale_legacy_thread_state_path()
    removed_paths: list[str] = []

    with thread_state_file_lock(route_path):
        previous_state = get_livekit_voice_route_state()
        if stale_legacy_path.exists():
            try:
                stale_legacy_path.unlink()
            except OSError:
                pass
            else:
                removed_paths.append(str(stale_legacy_path))
        voice = dispatcher_voice()
        preserved_state = VoiceRouteState(
            dispatcher_thread_id=None,
            dispatcher_voice_id=voice["id"],
            dispatcher_voice_name=voice["name"],
            active_target_thread_id=None,
            active_target_kind=None,
            active_target_label=None,
            active_target_voice_id=None,
            active_target_voice_name=None,
            updated_at=time.time(),
            instruction_override_supported=instruction_override_supported(),
        )
        _write_state(preserved_state)

    return {
        "previous_dispatcher_thread_id": previous_state.dispatcher_thread_id,
        "previous_active_target_thread_id": previous_state.active_target_thread_id,
        "removed_paths": removed_paths,
        "reset_route_path": str(_route_state_path()),
    }


async def warm_livekit_dispatcher_thread(
    *,
    timeout_seconds: float = 15.0,
    retry_interval_seconds: float = 0.5,
    fresh: bool = False,
) -> str:
    """Ensure the configured dispatcher backend thread exists and route state has its id.

    With ``fresh=True`` (dispatcher recreation), backends that reuse sessions
    by name are told to retire the existing dispatcher session so the warmed
    thread starts a new conversation.
    """
    from openbase_coder_cli.livekit_agent.super_agents_client import (
        SuperAgentsLiveKitClient,
    )

    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        permissions = codex_permission_defaults()
        client = SuperAgentsLiveKitClient(
            cwd=os.path.expanduser(
                os.getenv("LIVEKIT_CODEX_THREAD_CWD", str(Path.home()))
            ),
            state_path=os.getenv("LIVEKIT_CODEX_THREAD_STATE_PATH") or None,
            developer_instructions=_dispatcher_developer_instructions(),
            approval_policy=permissions["approvalPolicy"],
            sandbox=permissions["sandbox"],
            fresh_thread=fresh,
        )
        try:
            return await client.prepare()
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError("Unable to warm LiveKit dispatcher thread.") from exc
            await asyncio.sleep(max(retry_interval_seconds, 0.0))
        finally:
            await client.aclose()


def _dispatcher_developer_instructions() -> str | None:
    loaded = _read_instruction_file(CODEX_DISPATCHER_INSTRUCTIONS_PATH)
    if loaded:
        return loaded

    return DISPATCHER_BUILTIN_DEVELOPER_INSTRUCTIONS


async def publish_exit_to_dispatch(
    *,
    room_name: str | None = None,
    livekit_client: livekit_api.LiveKitAPI | None = None,
) -> VoiceRoutePublishResult:
    current_state = get_livekit_voice_route_state()
    state = VoiceRouteState(
        dispatcher_thread_id=current_state.dispatcher_thread_id,
        dispatcher_voice_id=current_state.dispatcher_voice_id,
        dispatcher_voice_name=current_state.dispatcher_voice_name,
        active_target_thread_id=None,
        active_target_kind=None,
        active_target_label=None,
        active_target_voice_id=None,
        active_target_voice_name=None,
        updated_at=time.time(),
        instruction_override_supported=instruction_override_supported(),
    )
    result = await _publish_route_command(
        {"action": "exit_to_dispatch", "state": asdict(state)},
        state=state,
        room_name=room_name,
        livekit_client=livekit_client,
    )
    _write_state(state)
    return result


async def publish_transfer_to_thread(
    thread_id: str,
    *,
    directory: str,
    label: str | None = None,
    agent_name: str | None = None,
    room_name: str | None = None,
    livekit_client: livekit_api.LiveKitAPI | None = None,
) -> VoiceRoutePublishResult:
    if not instruction_override_supported():
        raise VoiceRouteBlockedError(
            "Live voice transfer to target threads is blocked because this client "
            "cannot supply direct-LiveKit developer instructions without prompt wrapping."
        )

    route_state = get_livekit_voice_route_state()
    if is_dispatcher_identity(thread_id, label, route_state=route_state):
        raise VoiceRouteBlockedError(
            "The dispatcher thread cannot be used as a Super Agent voice transfer "
            "target. Use exit-to-dispatch to return to the dispatcher."
        )

    await prepare_target_thread_for_direct_livekit(
        thread_id=thread_id,
        directory=directory,
    )
    history_entry = get_voice_history_entry(thread_id)
    resolved_agent_name = (
        _optional_str(agent_name)
        or (history_entry.agent_name if history_entry else None)
        or (history_entry.voice_name if history_entry else None)
    )
    history_named_voice = (
        super_agent_voice_for_agent_name(
            history_entry.voice_name or history_entry.agent_name
        )
        if history_entry
        else None
    )
    if history_named_voice:
        voice = history_named_voice
    elif history_entry and _current_super_agent_voice_for_id(history_entry.voice_id):
        voice = CartesiaVoice(
            voice_id=history_entry.voice_id,
            name=history_entry.voice_name
            or history_entry.agent_name
            or resolved_agent_name
            or "voice",
        )
    else:
        voice = super_agent_voice_for_context(thread_id, label, resolved_agent_name)
    active_target_voice_name = resolved_agent_name if resolved_agent_name else None
    state = VoiceRouteState(
        dispatcher_thread_id=route_state.dispatcher_thread_id,
        dispatcher_voice_id=route_state.dispatcher_voice_id,
        dispatcher_voice_name=route_state.dispatcher_voice_name,
        active_target_thread_id=thread_id,
        active_target_kind="codex_thread",
        active_target_label=label,
        active_target_voice_id=voice.voice_id if voice else None,
        active_target_voice_name=active_target_voice_name,
        updated_at=time.time(),
        instruction_override_supported=True,
    )
    result = await _publish_route_command(
        {
            "action": "transfer_to_thread",
            "thread_id": thread_id,
            "cwd": directory,
            "label": label,
            "agent_name": resolved_agent_name,
            "state": asdict(state),
        },
        state=state,
        room_name=room_name,
        livekit_client=livekit_client,
    )
    _write_state(state)
    record_voice_assignment(
        thread_id=thread_id,
        agent_name=resolved_agent_name,
        cwd=directory,
        voice_id=state.active_target_voice_id,
        voice_name=voice.name if voice else state.active_target_voice_name,
        kind="codex_thread",
        source="route_transfer",
        seen_at=state.updated_at,
    )
    return result


async def _publish_route_command(
    payload: dict,
    *,
    state: VoiceRouteState,
    room_name: str | None,
    livekit_client: livekit_api.LiveKitAPI | None,
) -> VoiceRoutePublishResult:
    owns_client = livekit_client is None
    client = livekit_client or _build_livekit_client()
    try:
        target = await _resolve_target_room(client, room_name=room_name)
        command_id = f"voice-route-{uuid.uuid4().hex}"
        await client.room.send_data(
            livekit_api.SendDataRequest(
                room=target.room_name,
                data=json.dumps({"command_id": command_id, **payload}).encode("utf-8"),
                kind=livekit_api.DataPacket.Kind.RELIABLE,
                destination_identities=list(target.agent_identities),
                topic=VOICE_ROUTE_TOPIC,
            )
        )
        return VoiceRoutePublishResult(
            command_id=command_id,
            room_name=target.room_name,
            agent_identities=target.agent_identities,
            state=state,
        )
    finally:
        if owns_client:
            await client.aclose()


def _current_super_agent_voice_for_id(voice_id: str | None) -> CartesiaVoice | None:
    if not voice_id:
        return None
    return next(
        (
            voice
            for voice in _current_super_agent_voices()
            if voice.voice_id == voice_id
        ),
        None,
    )


def _route_state_path() -> Path:
    return get_data_dir() / VOICE_ROUTE_STATE_FILE


def _stale_legacy_thread_state_path() -> Path:
    return get_data_dir() / "livekit-codex-thread.json"


def _state_from_payload(payload: dict) -> VoiceRouteState:
    dispatcher_thread_id = payload.get("dispatcher_thread_id")
    active_target_thread_id = payload.get("active_target_thread_id")
    current_dispatcher_voice = dispatcher_voice()
    dispatcher_voice_id = (
        _optional_str(payload.get("dispatcher_voice_id"))
        or current_dispatcher_voice["id"]
    )
    active_target_voice_id = _optional_str(payload.get("active_target_voice_id"))
    return VoiceRouteState(
        dispatcher_thread_id=dispatcher_thread_id
        if isinstance(dispatcher_thread_id, str)
        else None,
        dispatcher_voice_id=dispatcher_voice_id,
        dispatcher_voice_name=_optional_str(payload.get("dispatcher_voice_name"))
        or _configured_voice_name(dispatcher_voice_id)
        or current_dispatcher_voice["name"],
        active_target_thread_id=active_target_thread_id
        if isinstance(active_target_thread_id, str)
        else None,
        active_target_kind=_optional_str(payload.get("active_target_kind")),
        active_target_label=_optional_str(payload.get("active_target_label")),
        active_target_voice_id=active_target_voice_id,
        active_target_voice_name=_optional_str(payload.get("active_target_voice_name"))
        or _configured_voice_name(active_target_voice_id),
        updated_at=payload.get("updated_at")
        if isinstance(payload.get("updated_at"), (int, float))
        else None,
        instruction_override_supported=instruction_override_supported(),
    )


def _configured_voice_name(voice_id: str | None) -> str | None:
    return voice_name_for_id(selected_tts_provider_id(), voice_id)


def _optional_str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_instruction_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return content or None


def _write_state(state: VoiceRouteState) -> None:
    path = _route_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8")

"""Environment loading and configuration constants for the LiveKit agent."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from openbase_coder_cli.brain_score import brain_score_token_file
from openbase_coder_cli.codex_control_plane import managed_codex_app_server_endpoint
from openbase_coder_cli.codex_session_defaults import (
    CODEX_APPROVAL_POLICY_ENV,
    CODEX_SANDBOX_ENV,
    DEFAULT_CODEX_APPROVAL_POLICY,
    DEFAULT_CODEX_SANDBOX,
)
from openbase_coder_cli.config.token_manager import DEFAULT_WEB_BACKEND_URL
from openbase_coder_cli.direct_voice_instructions import (
    DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS,
)
from openbase_coder_cli.dispatcher_instructions import with_dispatcher_rules
from openbase_coder_cli.host_kind import with_host_section
from openbase_coder_cli.paths import (
    CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH,
    CODEX_DISPATCHER_CONFIG_PATH,
    CODEX_DISPATCHER_INSTRUCTIONS_PATH,
    OPENBASE_BASE_DIR,
)
from openbase_coder_cli.tts_providers import (
    DEFAULT_CARTESIA_ANNOUNCER_VOICE_ID,
    DEFAULT_CARTESIA_VOICE_ID,
)

logger = logging.getLogger(__name__)


def _canonical_env_path() -> Path:
    """Path to the installed Openbase env file (the one the launchd/systemd
    wrapper sources). The agent's cwd is ``{workspace}/cli`` which has no
    ``.env``, so relying on a cwd-relative load silently picks up nothing."""
    try:
        from openbase_coder_cli.services.installation import InstallationConfig

        if InstallationConfig.exists():
            return Path(InstallationConfig.load().env_file).expanduser()
    except Exception:
        pass
    return OPENBASE_BASE_DIR / ".env"


def _load_openbase_env(*, override: bool = False) -> None:
    """Load env vars from the cwd ``.env`` (legacy) and the canonical installed
    env file. With ``override=True`` the on-disk values win, so a worker that
    started before a key was written to ``.env`` can self-heal on the next job
    instead of crash-looping on a now-stale environment."""
    load_dotenv(".env", override=override)
    load_dotenv(_canonical_env_path(), override=override)
    # The legacy loopback WebSocket endpoint is no longer the managed Codex
    # owner on Unix. Per-job refreshes must preserve the same migration as
    # setup; otherwise a stale on-disk value silently replaces the live Unix
    # control socket and voice dispatcher warm-up fails at call start.
    os.environ["CODEX_APP_SERVER_URL"] = managed_codex_app_server_endpoint().value


_load_openbase_env()

os.environ.setdefault("LIVEKIT_URL", "ws://localhost:7880")
os.environ.setdefault("LIVEKIT_CODEX_THREAD_CWD", str(Path.home()))

CODEX_APP_SERVER_URL = os.environ["CODEX_APP_SERVER_URL"]
LIVEKIT_CODEX_THREAD_CWD = os.environ["LIVEKIT_CODEX_THREAD_CWD"]

CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", DEFAULT_CARTESIA_VOICE_ID)
CARTESIA_ANNOUNCER_VOICE_ID = os.getenv(
    "CARTESIA_ANNOUNCER_VOICE_ID", DEFAULT_CARTESIA_ANNOUNCER_VOICE_ID
)
WEB_BACKEND_URL = os.getenv(
    "OPENBASE_CODER_CLI_WEB_BACKEND_URL",
    DEFAULT_WEB_BACKEND_URL,
).rstrip("/")
OPENBASE_CLOUD_AUDIO_BASE_URL = os.getenv(
    "OPENBASE_CLOUD_AUDIO_BASE_URL",
    f"{WEB_BACKEND_URL}/api/openbase/audio",
).rstrip("/")
OPENBASE_CLOUD_AUDIO_CARTESIA_VERSION = os.getenv(
    "OPENBASE_CLOUD_AUDIO_CARTESIA_VERSION",
    "2026-03-01",
)
# Live Voice engine (GPT-Live client delegation; see dev-docs/live-voice.md).
# The Openbase Cloud gateway relays GPT-Live under the account's token; the
# LiveKit plugin appends ``/live/sessions`` to this base URL. Override it the
# way OPENBASE_CLOUD_AUDIO_BASE_URL points the audio proxies at staging.
OPENBASE_CLOUD_LIVE_BASE_URL = os.getenv(
    "OPENBASE_CLOUD_LIVE_BASE_URL",
    f"{WEB_BACKEND_URL}/api/openbase/live/openai/v1",
).rstrip("/")
LIVE_VOICE_MODEL = os.getenv("LIVEKIT_LIVE_VOICE_MODEL", "gpt-live-1")
# One voice per call in phase 1 (the voice is fixed at session start); agents
# are named in speech instead of getting their own voice.
LIVE_VOICE_DEFAULT_VOICE = os.getenv("LIVEKIT_LIVE_VOICE_VOICE", "marin")
# Pre-start websocket handshake probe of the live endpoint. A refused
# connection, an HTTP 401/403/404 handshake, or an immediate 4401/4403 close
# means the live engine cannot start and the call falls back to the pipeline.
LIVE_VOICE_PREFLIGHT_TIMEOUT_SECONDS = float(
    os.getenv("LIVEKIT_LIVE_VOICE_PREFLIGHT_TIMEOUT_SECONDS", "5") or 5
)
LIVE_VOICE_PREFLIGHT_CLOSE_WAIT_SECONDS = float(
    os.getenv("LIVEKIT_LIVE_VOICE_PREFLIGHT_CLOSE_WAIT_SECONDS", "0.75") or 0.75
)
# Start-up latency (forensics F6, 2026-10-09: 7 s from room token to GPT-Live
# session on a cloud workspace). The agent job process probes the live engine
# (entitlement + handshake) at prewarm and every REFRESH seconds, and a call
# reuses a result younger than TTL instead of probing; a failed probe retries
# after RETRY seconds and is never reused.
LIVE_VOICE_READINESS_TTL_SECONDS = float(
    os.getenv("LIVEKIT_LIVE_VOICE_READINESS_TTL_SECONDS", "900") or 900
)
LIVE_VOICE_READINESS_REFRESH_SECONDS = float(
    os.getenv("LIVEKIT_LIVE_VOICE_READINESS_REFRESH_SECONDS", "600") or 600
)
LIVE_VOICE_READINESS_RETRY_SECONDS = float(
    os.getenv("LIVEKIT_LIVE_VOICE_READINESS_RETRY_SECONDS", "60") or 60
)
# Whether the job process keeps the readiness cache warm in the background.
LIVE_VOICE_READINESS_PREWARM = os.getenv(
    "LIVEKIT_LIVE_VOICE_READINESS_PREWARM", "1"
).strip().lower() not in {"0", "false", "no", "off"}
# Open the GPT-Live websocket while the room connects and the start route is
# applied, so AgentSession.start finds it open instead of waiting for it.
LIVE_VOICE_PRECONNECT = os.getenv(
    "LIVEKIT_LIVE_VOICE_PRECONNECT", "1"
).strip().lower() not in {"0", "false", "no", "off"}
# The room token view dispatches the agent to the room when the token is
# issued (explicit dispatch) instead of when the phone joins, so the agent's
# start-up overlaps the phone's join. A job started that way may sit in the
# room before anyone joins: it ends itself after this many seconds without a
# caller so an abandoned token does not keep a billed live session open.
LIVEKIT_PARTICIPANT_JOIN_TIMEOUT_SECONDS = float(
    os.getenv("LIVEKIT_PARTICIPANT_JOIN_TIMEOUT_SECONDS", "90") or 90
)

ANNOUNCER_TOPIC = "openbase.announcer.say"
VOICE_ROUTE_TOPIC = "openbase.voice.route"
AGENT_STATUS_TOPIC = "openbase.agent.status"
VOICE_LIFECYCLE_TOPIC = "openbase.voice.lifecycle"
# Participant-attribute mirror of the latest lifecycle event. Data packets can
# be silently lost in transit; attributes are state-synced by LiveKit, so the
# client always converges on the latest lifecycle state.
VOICE_LIFECYCLE_ATTRIBUTE = "openbase.voice.lifecycle"
# Which voice engine serves this call: ``live`` (GPT-Live full duplex; clients
# keep the microphone open and disable lifecycle auto-mute) or ``pipeline``
# (STT -> turn -> TTS; clients keep today's auto-mute). Published on join;
# clients that do not understand it keep today's half-duplex behaviour.
VOICE_ENGINE_ATTRIBUTE = "openbase.voice.engine"
ANNOUNCER_AUDIO_KIND = "audio_file"
SUPPORTED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg"}
ANNOUNCER_MAX_QUEUE_SIZE = int(os.getenv("LIVEKIT_ANNOUNCER_MAX_QUEUE_SIZE", "20"))
ANNOUNCER_SILENCE_GRACE_SECONDS = float(
    os.getenv("LIVEKIT_ANNOUNCER_SILENCE_GRACE_SECONDS", "0.5")
)
ANNOUNCER_STATE_WAIT_TIMEOUT_SECONDS = 0.1
LIVEKIT_DISPATCH_AGENT_NAME = os.environ.get(
    "LIVEKIT_DISPATCH_AGENT_NAME", "livekit-agent"
)
LIVEKIT_AGENT_HOST = os.getenv("LIVEKIT_AGENT_HOST", "127.0.0.1")
LIVEKIT_AGENT_PORT = int(os.getenv("LIVEKIT_AGENT_PORT", "8081"))
LIVEKIT_AGENT_LOAD_THRESHOLD_ENV = "LIVEKIT_AGENT_LOAD_THRESHOLD"
LIVEKIT_AGENT_NUM_IDLE_PROCESSES_ENV = "LIVEKIT_AGENT_NUM_IDLE_PROCESSES"
DEFAULT_LIVEKIT_DISPATCHER_CONFIG_PATH = CODEX_DISPATCHER_CONFIG_PATH
LIVEKIT_DISPATCHER_CONFIG_PATH = os.getenv(
    "LIVEKIT_DISPATCHER_CONFIG_PATH",
    str(DEFAULT_LIVEKIT_DISPATCHER_CONFIG_PATH),
)
DIRECT_LIVEKIT_INSTRUCTIONS_PATH_ENV = (
    "LIVEKIT_DIRECT_CODEX_DEVELOPER_INSTRUCTIONS_PATH"
)
DIRECT_LIVEKIT_INSTRUCTIONS_TEXT_ENV = "LIVEKIT_DIRECT_CODEX_DEVELOPER_INSTRUCTIONS"
DEFAULT_DIRECT_LIVEKIT_INSTRUCTIONS_PATH = CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH
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
# Startup persona of the GPT-Live voice model. Fixed for the whole call (the
# plugin cannot change instructions after session.start); route changes are
# appended as thinking/commentary by the delegation bridge instead.
# The voice model is the voice of the call, never its brain: the delegation
# bridge sends every caller utterance to the active Super Agent thread, which
# has the caller's tools, and the model only voices what comes back.
LIVE_VOICE_STARTUP_INSTRUCTIONS = """
You are the voice of a private Openbase coding call, not its brain. Everything
the caller says goes automatically to their coding agent (the dispatcher
first, or a specific agent after a transfer), which has their computer, files,
projects, tools and accounts. Only the agent answers.
Never answer a question or request yourself: no facts, no general knowledge,
no advice, and no guesses about the caller's computer, desktop, files,
projects, accounts, calendar, messages or anything else, even when you think
you know. When the caller asks for something, say a brief acknowledgement
such as "checking" or "one moment", delegate it as usual, and wait: the
agent's answer reaches you as commentary. Relay commentary faithfully and
concisely without adding facts of your own; thinking is context, not
something to say. You may greet the caller, answer thanks or small talk in a
few words, and ask them to repeat when you could not understand them. Mention
the agent by name when a transfer or announcement names one. Speak naturally,
stop when interrupted, and never read code, paths or identifiers character
by character.
""".strip()


def live_voice_startup_instructions(
    host: str | None = None, *, agent_label: str | None = None
) -> str:
    """The GPT-Live persona plus one line on where the caller's agent runs.

    ``host`` is a ``host_kind`` value; None detects this install's. On a cloud
    workspace the model otherwise acknowledges "checking your desktop" for a
    computer that has none (staging demo, 2026-10-08).

    ``agent_label`` names the agent a call started from a project thread is
    routed to from its first word, so the voice never presents itself as the
    dispatcher on such a call.
    """
    from openbase_coder_cli.host_kind import live_voice_host_note

    text = f"{LIVE_VOICE_STARTUP_INSTRUCTIONS}\n{live_voice_host_note(host)}"
    label = (agent_label or "").strip()
    if label:
        text += f"\n{live_voice_start_route_note(label)}"
    return text


def live_voice_start_route_note(agent_label: str) -> str:
    return (
        f"This call started inside {agent_label}'s thread: from the first word, "
        f"everything the caller says goes to {agent_label}, which answers. "
        f"Refer to it by that name and do not mention the dispatcher unless "
        f"the caller moves the call back to it."
    )


LIVEKIT_CODEX_THREAD_STATE_PATH = os.getenv("LIVEKIT_CODEX_THREAD_STATE_PATH")
LIVEKIT_CODEX_FRESH_THREAD_PER_SESSION = os.getenv(
    "LIVEKIT_CODEX_FRESH_THREAD_PER_SESSION", ""
).strip().lower() in {"1", "true", "yes", "on"}
LIVEKIT_CODEX_APPROVAL_POLICY = os.getenv(
    CODEX_APPROVAL_POLICY_ENV,
    DEFAULT_CODEX_APPROVAL_POLICY,
)
LIVEKIT_CODEX_SANDBOX = os.getenv(CODEX_SANDBOX_ENV, DEFAULT_CODEX_SANDBOX)
LIVEKIT_STT_PROVIDER = os.getenv("LIVEKIT_STT_PROVIDER", "assemblyai").lower()
LIVEKIT_VERBOSE_LOGGING = os.getenv("LIVEKIT_VERBOSE_LOGGING", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BRAIN_SCORE_ENABLED = os.getenv(
    "OPENBASE_BRAIN_SCORE_ENABLED", "1"
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BRAIN_SCORE_ENDPOINT = os.getenv(
    "OPENBASE_BRAIN_SCORE_ENDPOINT",
    "http://uat.api.getvibes.ai/api/v1/score/hackathon",
)
BRAIN_SCORE_INTERVAL_SECONDS = float(
    os.getenv("OPENBASE_BRAIN_SCORE_INTERVAL_SECONDS", "60") or 60
)
BRAIN_SCORE_MIN_DURATION_SECONDS = float(
    os.getenv("OPENBASE_BRAIN_SCORE_MIN_DURATION_SECONDS", "20") or 20
)
BRAIN_SCORE_COOLDOWN_SECONDS = float(
    os.getenv("OPENBASE_BRAIN_SCORE_COOLDOWN_SECONDS", "1800") or 1800
)
BRAIN_SCORE_OUTPUT_PATH = Path(
    os.getenv(
        "OPENBASE_BRAIN_SCORE_OUTPUT_PATH",
        str(Path.home() / ".openbase" / "brain_score.json"),
    )
).expanduser()
BRAIN_SCORE_TOKEN_FILE = brain_score_token_file()
BRAIN_SCORE_LATITUDE = os.getenv("OPENBASE_BRAIN_SCORE_LATITUDE", "").strip()
BRAIN_SCORE_LONGITUDE = os.getenv("OPENBASE_BRAIN_SCORE_LONGITUDE", "").strip()

LIVEKIT_AUDIO_FRAME_LOG_FIRST = int(os.getenv("LIVEKIT_AUDIO_FRAME_LOG_FIRST", "10"))
LIVEKIT_AUDIO_FRAME_LOG_EVERY = int(os.getenv("LIVEKIT_AUDIO_FRAME_LOG_EVERY", "10"))
PROACTIVE_STEER_PROMPT_CACHE_SECONDS = 120.0


def _optional_float_env(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return None
    if value <= 0:
        logger.warning("Ignoring non-positive %s=%r", name, raw)
        return None
    return value


def _optional_int_env(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return None
    if value < 0:
        logger.warning("Ignoring negative %s=%r", name, raw)
        return None
    return value


def _load_dispatcher_developer_instructions() -> str | None:
    try:
        loaded = CODEX_DISPATCHER_INSTRUCTIONS_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning(
            "Unable to read dispatcher instruction file %s",
            CODEX_DISPATCHER_INSTRUCTIONS_PATH,
            exc_info=True,
        )
    else:
        if loaded:
            return with_dispatcher_rules(loaded)

    return with_dispatcher_rules(DISPATCHER_BUILTIN_DEVELOPER_INSTRUCTIONS)


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
            return with_host_section(loaded)

    loaded = _read_instruction_file(
        default_path or DEFAULT_DIRECT_LIVEKIT_INSTRUCTIONS_PATH
    )
    if loaded:
        return with_host_section(loaded)

    text = values.get(DIRECT_LIVEKIT_INSTRUCTIONS_TEXT_ENV, "").strip()
    if text:
        return with_host_section(text)

    return with_host_section(DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS)


def _read_instruction_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning(
            "Unable to read direct LiveKit instruction file %s", path, exc_info=True
        )
        return None
    return content or None

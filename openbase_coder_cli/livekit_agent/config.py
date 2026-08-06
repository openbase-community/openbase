"""Environment loading and configuration constants for the LiveKit agent."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from openbase_coder_cli.brain_score import brain_score_token_file
from openbase_coder_cli.codex_session_defaults import (
    CODEX_APPROVAL_POLICY_ENV,
    CODEX_SANDBOX_ENV,
    DEFAULT_CODEX_APPROVAL_POLICY,
    DEFAULT_CODEX_SANDBOX,
)
from openbase_coder_cli.config.token_manager import DEFAULT_WEB_BACKEND_URL
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


_load_openbase_env()

os.environ.setdefault("LIVEKIT_URL", "ws://localhost:7880")
os.environ.setdefault("CODEX_APP_SERVER_URL", "ws://127.0.0.1:4500")
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

ANNOUNCER_TOPIC = "openbase.announcer.say"
VOICE_ROUTE_TOPIC = "openbase.voice.route"
AGENT_STATUS_TOPIC = "openbase.agent.status"
VOICE_LIFECYCLE_TOPIC = "openbase.voice.lifecycle"
# Participant-attribute mirror of the latest lifecycle event. Data packets can
# be silently lost in transit; attributes are state-synced by LiveKit, so the
# client always converges on the latest lifecycle state.
VOICE_LIFECYCLE_ATTRIBUTE = "openbase.voice.lifecycle"
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
DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS = """
You are receiving direct user speech from a LiveKit voice session.
Keep final spoken responses concise and directly useful.
Avoid bulleted or itemized lists in spoken responses because text-to-speech reads repeated item markers badly. Prefer brief plain prose. When a list is genuinely clearer, use a short numbered list instead of bullets.
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
LIVEKIT_CODEX_ACK_DELAY_SECONDS = float(
    os.getenv("LIVEKIT_CODEX_ACK_DELAY_SECONDS", "0") or 0
)
LIVEKIT_CODEX_ACK_MESSAGE = os.getenv("LIVEKIT_CODEX_ACK_MESSAGE", "Okay.").strip()
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
            return loaded

    return DISPATCHER_BUILTIN_DEVELOPER_INSTRUCTIONS


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
        default_path or DEFAULT_DIRECT_LIVEKIT_INSTRUCTIONS_PATH
    )
    if loaded:
        return loaded

    text = values.get(DIRECT_LIVEKIT_INSTRUCTIONS_TEXT_ENV, "").strip()
    if text:
        return text

    return DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS


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

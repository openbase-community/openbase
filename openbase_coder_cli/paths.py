import os
from pathlib import Path

OPENBASE_BASE_DIR = Path(
    os.environ.get("OPENBASE_CODER_CLI_DATA_DIR", Path.home() / ".openbase")
).expanduser()
# Backend CLI binaries (codex, claude) installed on demand by setup.
OPENBASE_BIN_DIR = OPENBASE_BASE_DIR / "bin"
# Standalone runtime package releases activated by the desktop app/install.sh.
STANDALONE_PACKAGES_DIR = OPENBASE_BASE_DIR / "packages" / "standalone"
STANDALONE_RELEASES_DIR = STANDALONE_PACKAGES_DIR / "releases"
STANDALONE_CURRENT_DIR = STANDALONE_PACKAGES_DIR / "current"
OPENBASE_SOUNDS_DIR = OPENBASE_BASE_DIR / "sounds"
OPENBASE_INSTRUCTIONS_DIR = OPENBASE_BASE_DIR / "instructions"
# Openbase shares the user's real agent homes: sessions started from the
# terminal, the desktop apps, and Openbase voice all live in one store per
# backend. Openbase-specific posture (permissions, instructions) is passed
# per session, never written into these homes' defaults.
CODEX_HOME_DIR = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
CODEX_AGENTS_MD_PATH = CODEX_HOME_DIR / "AGENTS.md"
CODEX_CONFIG_PATH = CODEX_HOME_DIR / "config.toml"
CLAUDE_CONFIG_DIR = Path(
    os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")
).expanduser()
CLAUDE_SETTINGS_PATH = CLAUDE_CONFIG_DIR / "settings.json"
CLAUDE_STATE_PATH = Path.home() / ".claude.json"
# Rendered Openbase base agent instructions, delivered per session (Codex
# developerInstructions / Claude system prompt), not via the shared homes.
OPENBASE_AGENTS_MD_PATH = OPENBASE_INSTRUCTIONS_DIR / "AGENTS.md"
OPENBASE_DIRECT_LIVEKIT_INSTRUCTIONS_PATH = (
    OPENBASE_INSTRUCTIONS_DIR / "VOICE_INSTRUCTIONS.md"
)
OPENBASE_DISPATCHER_INSTRUCTIONS_PATH = (
    OPENBASE_INSTRUCTIONS_DIR / "DISPATCHER_INSTRUCTIONS.md"
)
OPENBASE_SUPER_AGENT_INSTRUCTIONS_PATH = (
    OPENBASE_INSTRUCTIONS_DIR / "SUPER_AGENT_INSTRUCTIONS.md"
)
CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH = OPENBASE_DIRECT_LIVEKIT_INSTRUCTIONS_PATH
CODEX_DISPATCHER_INSTRUCTIONS_PATH = OPENBASE_DISPATCHER_INSTRUCTIONS_PATH
OPENBASE_DISPATCHER_CONFIG_PATH = OPENBASE_BASE_DIR / "dispatcher-config.json"
CODEX_DISPATCHER_CONFIG_PATH = OPENBASE_DISPATCHER_CONFIG_PATH
CODEX_SUPER_AGENT_INSTRUCTIONS_PATH = OPENBASE_SUPER_AGENT_INSTRUCTIONS_PATH
INSTALLATION_JSON_PATH = OPENBASE_BASE_DIR / "installation.json"
DEFAULT_ENV_FILE_PATH = OPENBASE_BASE_DIR / ".env"
DEFAULT_LOG_DIR = OPENBASE_BASE_DIR / "logs"
LAUNCHD_WRAPPER_DIR = OPENBASE_BASE_DIR / "launchd"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
SYSTEMD_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
# Windows Task Scheduler backend: XML task-definition source files live here
# (filesystem, mirrors PLIST_DIR/SYSTEMD_UNIT_DIR); the task database itself
# is registered under TASK_SCHEDULER_FOLDER, a Task Scheduler namespace path,
# not a filesystem path.
TASK_SCHEDULER_DIR = OPENBASE_BASE_DIR / "tasks"
TASK_SCHEDULER_FOLDER = r"\OpenbaseCoder"
LAUNCHD_DOMAIN = "com.openbase.coder"
AUTH_JSON_PATH = OPENBASE_BASE_DIR / "auth.json"
OWNER_IDENTITY_JSON_PATH = OPENBASE_BASE_DIR / "owner-identity.json"
SYNC_CONFIG_PATH = OPENBASE_BASE_DIR / "sync-config.json"
CODE_SYNC_DIR = OPENBASE_BASE_DIR / "code-sync"
CODE_SYNC_CONFLICTS_PATH = CODE_SYNC_DIR / "conflicts.json"
SYNC_VERSIONS_DIR = OPENBASE_BASE_DIR / "sync-versions"
MACHINE_TOKEN_JSON_PATH = OPENBASE_BASE_DIR / "machine-token.json"
CONSOLE_SETTINGS_JSON_PATH = OPENBASE_BASE_DIR / "console-settings.json"
ONBOARDING_JSON_PATH = OPENBASE_BASE_DIR / "onboarding.json"
# Created by the bundled openbase-onboarding skill once an agent has read it.
ONBOARDING_SKILL_READ_MARKER_PATH = OPENBASE_BASE_DIR / "onboarding-skill-read"
DESKTOP_CONTROL_JSON_PATH = OPENBASE_BASE_DIR / "desktop-control.json"
# Agent-home hook scripts managed by openbase-coder setup.
OPENBASE_HOOKS_DIR = OPENBASE_BASE_DIR / "hooks"
INJECT_SESSION_ID_HOOK_PATH = OPENBASE_HOOKS_DIR / "inject-session-id.sh"

PLUGIN_BASE_DIR = OPENBASE_BASE_DIR / "plugins"
# Stable site dir for plugin Python packages in standalone installs; lives
# outside the versioned runtime package so upgrades don't drop plugins.
PLUGIN_SITE_DIR = PLUGIN_BASE_DIR / "site"
PLUGIN_REGISTRY_PATH = PLUGIN_BASE_DIR / "plugins.json"
PLUGIN_REQUIREMENTS_PATH = PLUGIN_BASE_DIR / "plugin_requirements.txt"
PLUGIN_SOURCES_DIR = PLUGIN_BASE_DIR / "sources"
PLUGIN_CONSOLE_REGISTRY_PATH = PLUGIN_BASE_DIR / "console" / "registry.json"
PLUGIN_CONSOLE_ASSETS_DIR = PLUGIN_BASE_DIR / "console-assets"
PLUGIN_SKILLS_OWNERSHIP_PATH = PLUGIN_BASE_DIR / "skills_ownership.json"

# User-published, tailnet-only development services. The registry contains
# names and local ports, never credentials.
PUBLISHED_SERVICES_PATH = OPENBASE_BASE_DIR / "published-services.json"

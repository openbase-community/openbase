# Files and Paths

This page lists the key files Openbase CLI creates or consumes. App-side
storage is much smaller: the [desktop app](desktop-app.md) keeps Electron
state under `~/Library/Application Support/@openbase/coder-desktop`, and the
[iOS app](ios-tabs.md#how-the-app-connects) keeps only its auth token
(Keychain) and backend host list (UserDefaults) on the phone.

## Base Directories

- Openbase data root: `~/.openbase`
- Standalone runtime packages: `~/.openbase/packages/standalone/`
- Development workspace checkout: wherever you cloned
  `openbase-coder-workspace` (recorded in `~/.openbase/installation.json`)
- Launchd plists (macOS): `~/Library/LaunchAgents`
- systemd user units (Linux): `~/.config/systemd/user`

## Desktop App Storage

The Electron desktop app keeps its own persistent state (window data,
renderer storage) at:

- macOS: `~/Library/Application Support/@openbase/coder-desktop`

This survives app reinstalls; machine-onboarding progress lives in
`~/.openbase/desktop-onboarding.json` instead so wiping the Openbase home
resets onboarding. Remove both when fully uninstalling (see
[Uninstall](uninstall.md)).

## Setup-Time Artifacts

| Path | Created By | Purpose |
|---|---|---|
| `~/.openbase/installation.json` | `openbase-coder setup` | Stores `workspace_path` + `env_file` |
| `~/.openbase/.env` | `openbase-coder setup` | Shared env config and generated secrets |
| `~/.openbase/codex_home/auth.json` | `openbase-coder setup` | Symlink to `~/.codex/auth.json` for launchd Codex services |
| `~/.codex/AGENTS.md` | User, `openbase-coder setup` | Normal/general-purpose Codex instructions; setup creates it if needed so normal Claude can link to it |
| `~/.claude/CLAUDE.md` | User, `openbase-coder setup` | Symlink to normal `~/.codex/AGENTS.md` |
| `~/.openbase/codex_home/AGENTS.md` | `openbase-coder setup`, CLI launch, settings API | Generated Openbase Codex-home instructions from `instructions/AGENTS.md`; records the source template path and can include normal Codex instructions |
| `~/.openbase/claude_config/CLAUDE.md` | `openbase-coder setup` | Symlink to Openbase's generated `~/.openbase/codex_home/AGENTS.md` for Claude Code sessions using Openbase's managed `CLAUDE_CONFIG_DIR` |
| `~/.openbase/claude_config/.claude.json` | `openbase-coder setup`, `openbase-coder claude sync-state` | Claude Code user config/state for the Openbase-managed Claude config dir (the file Claude Code reads under `CLAUDE_CONFIG_DIR`), including the Super Agents MCP server; normal `~/.claude.json` state is merged in with existing Openbase values winning |
| `~/.codex/config.toml` | User, `openbase-coder setup` | Normal Codex config; setup registers a `[mcp_servers.super-agents]` table whose child-thread default is `codex`. The optional `--install-normal-agent-hooks` also adds the trusted session provenance hook. With `--link-codex-config`, this is the target of the Openbase service Codex config symlink and receives Openbase permission overrides |
| `~/.claude.json` | Claude Code, `openbase-coder setup` | Normal Claude Code state; setup registers an `mcpServers.super-agents` entry whose child-thread default is `claude_code` (MCP entry only) |
| `~/.claude/settings.json` | User, `openbase-coder setup` | Normal Claude Code settings; untouched by default except when explicitly linked or when `--install-normal-agent-hooks` adds the session provenance hook |
| `~/.openbase/instructions/VOICE_INSTRUCTIONS.md` | `openbase-coder setup` | Generated default direct voice-session instructions |
| `~/.openbase/instructions/DISPATCHER_INSTRUCTIONS.md` | `openbase-coder setup` | Generated default dispatcher-only instructions |
| `~/.openbase/instructions/SUPER_AGENT_INSTRUCTIONS.md` | `openbase-coder setup` | Generated default Super Agent thread instructions |
| `~/.openbase/dispatcher-config.json` | `openbase-coder setup`, `openbase-coder defaults`, settings API | Dispatcher runtime settings, including default reasoning and backend-specific model defaults |
| `~/.openbase/hooks/inject-session-id.sh` | `openbase-coder setup` | Bundled SessionStart hook script, registered in both Openbase agent homes; injects the session's thread/session ID into the conversation, along with the instructions to stamp commits with it as the `Agent-Thread-Id` trailer |
| `~/.openbase/packages/standalone/previous` | `openbase-coder self-update` | Symlink to the prior release, kept for rollback |
| `~/.openbase/update-check.json` | `openbase-coder self-update` / update API | Cached result of the last update check (no-network status reads) |
| `~/.openbase/logs/self-update.log` | `POST /api/update/apply/` | Output of UI-triggered detached self-updates |
| `~/.openbase/codex_home/config.toml` | `openbase-coder setup` | Openbase service Codex config, including broad local access and the Super Agents MCP server. With `--link-codex-config`, this is a symlink to `~/.codex/config.toml` |
| `~/.openbase/codex_home/skills/<skill>/` | `openbase-coder setup`, skills auto-link | Symlink to a workspace-owned skill source under `skills/skills/<skill>/`, or (with auto-link enabled) to a personal skill under `~/.agents/skills/<skill>/` |
| `~/.openbase/claude_config/skills/<skill>/` | `openbase-coder setup`, skills auto-link | Symlink to a workspace-owned skill source under `skills/skills/<skill>/`, or (with auto-link enabled) to a personal skill under `~/.agents/skills/<skill>/` |
| `<workspace>/cli/.venv/` | `openbase-coder setup` (development mode) | CLI and bundled LiveKit worker environment |
| `~/.openbase/bin/codex` | `openbase-coder setup` | Codex CLI installed on demand from GitHub release binaries |
| `~/.local/bin/openbase-coder` | `openbase-coder setup` | User CLI shim; points at the standalone package launcher or the workspace CLI venv (never overwrites a `uv tool install`-managed script) |

Generated instruction files are rendered from the workspace or bundled
`instructions/` directory, record their source template path, and interpolate
template variables such as `${dangerous_confirmation_phrase}`. Setup rewrites
`~/.openbase/codex_home/AGENTS.md`;
shared files under `~/.openbase/instructions` are updated when they are already
managed/generated and left alone if they appear to be unrelated custom files.
The dispatcher config is created when missing with default dispatcher reasoning
effort `low` and default Super Agents reasoning effort `high`; setup does not
overwrite an existing dispatcher config.
Workspace skills are symlink-installed, not copied, so edits to source skills
are visible to the Openbase Codex home immediately.
When the skills auto-link setting is enabled (default off; toggled from the
console skills settings), personal skills under `~/.agents/skills` are also
symlinked into both `~/.openbase/codex_home/skills` and
`~/.openbase/claude_config/skills`, and the `openbase-routines` service
re-syncs the links roughly every five minutes so newly added personal skills
appear without a restart.
On macOS, Claude Code OAuth credentials live in a per-`CLAUDE_CONFIG_DIR`
keychain service: setup and `openbase-coder claude sync-state` copy the normal
"Claude Code-credentials" keychain item to Openbase's config-dir-specific
service so the managed Claude config inherits the normal Claude login.
The Codex home config grants full local sandbox access, disables permission
prompts, and uses the workspace venv Super Agents MCP executable when available;
otherwise setup records the resolved absolute `uv` path for the current machine.
By default this config is separate from the normal Codex config. Passing
`--link-codex-config` links it to `~/.codex/config.toml` before setup writes the
Super Agents MCP table.

## Service Artifacts

| Path Pattern | Created By | Purpose |
|---|---|---|
| `~/.openbase/launchd/<service>.sh` | `services install/regenerate` | Launch wrappers |
| `~/Library/LaunchAgents/com.openbase.coder.<service>.plist` | `services install/regenerate` (macOS) | launchd job definitions |
| `~/.config/systemd/user/com.openbase.coder.<service>.service` | `services install/regenerate` (Linux) | systemd user unit definitions |
| `~/.openbase/logs/<service>.stdout.log` | launchd services | Service stdout logs |
| `~/.openbase/logs/<service>.stderr.log` | launchd services | Service stderr logs |

Wrappers for `codex-app-server`, `livekit-agent`, and `django-cli` prefer binaries from
`<workspace>/.venv/bin/`, then `<workspace>/cli/.venv/bin/`, then
`<workspace>/agent/.venv/bin/`
so launchd follows the configured workspace checkout.

Managed services:

- `livekit-server`
- `codex-app-server`
- `livekit-agent`
- `django-cli`

## Runtime Data

| Path | Written By | Purpose |
|---|---|---|
| `~/.openbase/db.sqlite3` | Django migrations/runtime | App DB for local CLI state |
| `~/.openbase/staticfiles/` | `collectstatic` | Served static assets |
| `~/.openbase/coder-projects.json` | Session/project APIs | Recent project tracking |
| `~/.openbase/auth.json` | `openbase-coder login` | Access/refresh tokens |

## Plugin Data

| Path | Written By | Purpose |
|---|---|---|
| `~/.openbase/plugins/plugins.json` | `openbase-coder plugins add/update/remove` | Installed plugin registry |
| `~/.openbase/plugins/plugin_requirements.txt` | plugin lifecycle commands | Untracked plugin pip requirements ledger |
| `~/.openbase/plugins/sources/` | `plugins add/update` (GitHub sources) | Local clones used for pinned installs |
| `~/.openbase/plugins/console/registry.json` | plugin lifecycle commands | Generated console registry metadata |
| `~/.openbase/plugins/console-assets/<plugin>/<page>/` | plugin lifecycle commands | Prebuilt static assets for iframe console pages, served at `/openbase-plugin-assets/...` |
| `~/.openbase/plugins/site/` | plugin lifecycle commands (standalone installs) | Stable plugin Python package site dir added to `sys.path`; survives runtime package upgrades |
| `~/.openbase/plugins/skills_ownership.json` | plugin lifecycle commands | Ownership map for globally synced skills |
| `${CLAUDE_CONFIG_DIR:-~/.claude}/skills/<plugin_id>__<skill_name>/SKILL.md` | plugin lifecycle commands | Plugin-declared global agent skills |

## Console and API Routes (Used by iOS)

| Route | Used By |
|---|---|
| `/api/threads/` | Threads tab |
| `/api/projects/recent/` | Threads tab |
| `/api/git/diff/` and `/dashboard/diff` | Diff tab |
| `/ws/threads/` | Threads tab global turn updates |
| `/ws/threads/<thread_id>/` | Thread detail realtime updates |

## Plugin API Routes

| Route | Purpose |
|---|---|
| `/api/plugins/` | List installed plugins and capabilities |
| `/api/plugins/<plugin_id>/` | Show one plugin |
| `/api/plugins/console-registry/` | Return generated console registry metadata |
| `/api/bootstrap/<bootstrapper_name>/` | Run bootstrapper by name |
| `/api/plugins/<plugin_id>/...` | Plugin-declared Django URL modules (if provided) |

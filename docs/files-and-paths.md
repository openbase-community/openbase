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
| `~/.openbase/.env` | `openbase-coder setup` | Shared env config and generated secrets, including Openbase's per-session Super Agents posture (`SUPER_AGENTS_CODEX_APPROVAL_POLICY=never`, `SUPER_AGENTS_CODEX_SANDBOX_POLICY=danger-full-access`, `SUPER_AGENTS_BASE_INSTRUCTIONS_PATH`) |
| `~/.codex/auth.json` | `codex login` | Your Codex login; used directly by Openbase Codex sessions and services |
| `~/.codex/AGENTS.md` | User | Your own Codex instructions; setup leaves this file unchanged |
| `~/.claude/CLAUDE.md` | User | Your own Claude instructions; setup leaves this file or symlink unchanged |
| `~/.openbase/instructions/AGENTS.md` | `openbase-coder setup`, settings API | Generated Openbase base instructions from `instructions/AGENTS.md`; delivered per session by super-agents (Codex developer instructions / Claude system prompt), never written into the shared homes. Editable via the console "Openbase base instructions" target |
| `~/.codex/config.toml` | User | Normal Codex defaults; setup only removes identifiable legacy Openbase MCP and hook entries, with a backup |
| `~/.codex/openbase.config.toml` | Setup, user | Openbase Codex profile: model, reasoning, permissions, MCP, and session-ID hook; loaded per thread and usable with `codex -p openbase` |
| `~/.codex/openbase-cloud.config.toml` | Setup, user | Thread-scoped internal Cloud Codex profile and provider routing |
| `~/.claude.json` | Claude Code | Shared login and session state; setup migrates only identifiable legacy Openbase MCP entries |
| `~/.claude/settings.json` | User | Normal Claude settings; setup migrates only the legacy Openbase session-ID hook |
| `~/.openbase/profiles/claude/settings.json` | Setup, user | Openbase Claude session settings and hooks, supplied through `--settings` |
| `~/.openbase/profiles/claude/mcp.json` | Setup | Openbase Claude MCP configuration, supplied per session |
| `~/.openbase/instructions/VOICE_INSTRUCTIONS.md` | `openbase-coder setup` | Generated default direct voice-session instructions |
| `~/.openbase/instructions/DISPATCHER_INSTRUCTIONS.md` | `openbase-coder setup` | Generated default dispatcher-only instructions |
| `~/.openbase/instructions/SUPER_AGENT_INSTRUCTIONS.md` | `openbase-coder setup` | Generated default Super Agent thread instructions |
| `~/.openbase/dispatcher-config.json` | `openbase-coder setup`, `openbase-coder defaults`, settings API | Dispatcher runtime settings, including default reasoning and backend-specific model defaults |
| `~/.openbase/hooks/inject-session-id.sh` | `openbase-coder setup` | Bundled SessionStart hook script, registered in the Openbase profiles; supplies session attribution for commits and agent-aware commands |
| `~/.openbase/packages/standalone/previous` | `openbase-coder self-update` | Symlink to the prior release, kept for rollback |
| `~/.openbase/update-check.json` | `openbase-coder self-update` / update API | Cached result of the last update check (no-network status reads) |
| `~/.openbase/logs/self-update.log` | `POST /api/update/apply/` | Output of UI-triggered detached self-updates |
| `~/.codex/skills/<skill>/` | `openbase-coder setup`, skills auto-link | Symlink to a workspace-owned skill source under `skills/skills/<skill>/`, or (with auto-link enabled) to a personal skill under `~/.agents/skills/<skill>/` |
| `~/.claude/skills/<skill>/` | `openbase-coder setup`, skills auto-link | Symlink to a workspace-owned skill source under `skills/skills/<skill>/`, or (with auto-link enabled) to a personal skill under `~/.agents/skills/<skill>/` |
| `~/.openbase/claude-app-index-ledger.json` | `sync-workers` (Claude app index sync, macOS) | Ledger of Openbase Claude sessions injected into the Claude desktop app's private session index so they appear in the app (best-effort) |
| `<workspace>/cli/.venv/` | `openbase-coder setup` (development mode) | CLI and bundled LiveKit worker environment |
| `~/.openbase/bin/codex` | `openbase-coder setup` | Codex CLI installed on demand from GitHub release binaries |
| `~/.local/bin/openbase-coder` | `openbase-coder setup` | User CLI shim; points at the standalone package launcher or the workspace CLI venv (never overwrites a `uv tool install`-managed script) |

Generated instruction files are rendered from the workspace or bundled
`instructions/` directory, record their source template path, and interpolate
template variables such as `${dangerous_confirmation_phrase}`. Setup rewrites
`~/.openbase/instructions/AGENTS.md` (the Openbase base instructions);
shared files under `~/.openbase/instructions` are updated when they are already
managed/generated and left alone if they appear to be unrelated custom files.
The dispatcher config is created when missing with default dispatcher reasoning
effort `low` and default Super Agents reasoning effort `high`; setup does not
overwrite an existing dispatcher config.
Workspace skills are symlink-installed, not copied, so edits to source skills
are visible to agents immediately. In the console and skills API, skill scopes
are `home` (personal skills under `~/.agents/skills`), `codex`
(`~/.codex/skills`), and `claude` (`~/.claude/skills`).
When the skills auto-link setting is enabled (default off; toggled from the
console skills settings), personal skills under `~/.agents/skills` are also
symlinked into both `~/.codex/skills` and
`~/.claude/skills`, and the `openbase-routines` service
re-syncs the links roughly every five minutes so newly added personal skills
appear without a restart.
Openbase never keeps a separate agent credential: Codex sessions read
`~/.codex/auth.json` and Claude Code sessions use your own Claude Code login.
The Super Agents MCP entries use the workspace venv MCP executable when
available; otherwise setup records the resolved absolute `uv` path for the
current machine. Openbase's permission posture is not written into
`~/.codex/config.toml` or `~/.claude/settings.json`; super-agents passes it
per session via profiles and role overrides. The shared Codex app-server has no Openbase model or provider launch overrides.

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

On macOS and Linux, `codex-app-server` listens at
`$CODEX_HOME/app-server-control/app-server-control.sock`. Openbase pins its
managed server and clients to the shared `~/.codex` home; the socket is runtime
state and is never committed. A stale socket is removed only after it is
confirmed not to have a live owner.

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
| `~/.claude/skills/<plugin_id>__<skill_name>/SKILL.md` | plugin lifecycle commands | Plugin-declared global agent skills |

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

# setup

Run the full Openbase local installation flow.

In the apps: the [desktop app's](../desktop-app.md#install-and-first-run-setup)
guided setup runs this command for you (streaming its output via
`--json-progress`); the [iOS app](../ios-tabs.md#onboarding) waits on its
completion during pairing.

## Usage

```bash
openbase-coder setup [OPTIONS]
```

## Deployment Modes

Setup supports exactly two deployment modes and picks one automatically:

- **Standalone (production)**: a bundled runtime package — shipped inside the
  desktop app — containing Python, the CLI, LiveKit server, a prebuilt
  console, agent instructions, and skills. Detected automatically via the
  package's `openbase-coder-package.json`.
- **Development**: no runtime package is present, so setup runs against a
  developer's `openbase-coder-workspace` checkout. Setup **never clones or
  git-updates a workspace**. With `--workspace-dir` omitted it discovers the
  checkout from, in order:
  1. the workspace recorded in `~/.openbase/installation.json` by a prior
     install, then
  2. the checkout behind an editable CLI install
     (`uv tool install -e ./cli`, via the package's `direct_url.json`).

  If neither is found, setup errors and asks you to clone
  `openbase-coder-workspace` (and run its `./scripts/setup`), pass
  `--workspace-dir`, or use the standalone install instead.

For normal macOS installs, prefer the desktop app: it activates the bundled
runtime automatically, and its guided flow runs `openbase-coder setup` for
you. To run setup yourself from a terminal instead, follow
[Manual Setup for the Desktop App](../manual-installation.md).

For development, run the workspace script from your checkout root; it runs
`multi sync --install-set default` and then
`openbase-coder setup --workspace-dir <workspace-root>`:

```bash
./scripts/setup
```

The workspace script is for a clean source-workspace install. If it finds an
existing standalone install or a different development workspace install, it
stops and directs you to [Uninstall](../uninstall.md) before making changes.

## Backend Selection

Setup configures the default coding backend:

```bash
openbase-coder setup --backend codex
openbase-coder setup --backend claude-code
openbase-coder setup --backend openbase-cloud
```

- `codex`: native Codex app-server with OpenAI models.
- `claude-code`: Claude Code backend using local Claude auth/billing for Super
  Agents UI-driver sessions.
- `openbase-cloud`: Cloud-proxied Claude Code through Openbase Cloud with
  Openbase login and no personal Anthropic account requirement.

Codex and Claude Code are peers; there is no silent default. When creating a
new `~/.openbase/.env` with `--backend` omitted, setup prompts interactively
for the backend, and errors in non-interactive runs (including
`--json-progress`) asking for an explicit `--backend`. Existing env files keep
their configured backend and are only changed when `--backend` is passed.

Setup installs the selected backend's CLI on demand if it is missing: `codex`
from its GitHub release binaries into `~/.openbase/bin`, `claude` via
Anthropic's official native installer. Backend-specific services (such as
`codex-app-server`) are only installed for the backends that use them;
`openbase-coder services status` shows `not used (<backend> backend)` for
gated-out services.

With `--audio-provider local`, setup installs the optional Kokoro/MLX local
audio dependencies and downloads the required models. This path requires an
Apple Silicon Mac (MLX) and a Python 3.12 runtime — Kokoro currently declares
Python `<3.13`, and setup refuses local audio on newer runtimes. Standalone
packages should be built with Python 3.12 to keep local audio available.

## Options

| Option | Default | Description |
|---|---|---|
| `--workspace-dir PATH` | discovered | Development workspace checkout. When omitted, discovered from the recorded installation, then an editable CLI install; ignored in standalone mode |
| `--env-file PATH` | `~/.openbase/.env` | Shared environment file path |
| `--assembly-ai-api-key TEXT` | env `ASSEMBLY_AI_API_KEY` | Optional STT key |
| `--cartesia-api-key TEXT` | env `CARTESIA_API_KEY` | Optional TTS key |
| `--skip-services` | `false` | Skip background service installation |
| `--link-codex-config` | `false` | Symlink Openbase's service Codex config to `~/.codex/config.toml`. **Warning:** setup then writes Openbase's permission overrides (`sandbox_mode = "danger-full-access"` and a no-prompt approval policy) into that shared normal config |
| `--link-claude-config` | `false` | Symlink Openbase's Claude settings to the normal `~/.claude/settings.json` |
| `--fast-mode/--no-fast-mode` | `true` | Use the fast service tier for the voice dispatcher. Super Agents stay on the standard tier; both are adjustable in console settings |
| `--backend NAME` | prompted for new env files | Default coding backend: `codex`, `claude-code`, or `openbase-cloud`. Existing env files are only changed when provided |
| `--audio-provider NAME` | `openbase-cloud` for new dispatcher configs | Voice audio provider. Existing configs are only changed when provided |
| `--json-progress` | `false` | Emit NDJSON step events on stdout for UI-driven setup; human-readable output moves to stderr |

## Behavior Details

`setup` runs on macOS (launchd) and Linux (systemd user services) and performs these phases:

1. Ensures `~/.openbase` exists, plus the thread-sync exchange folder and bundled sounds.
2. Detects the bundled runtime package (standalone mode), or resolves the development workspace checkout as described above. Never clones or updates a workspace.
3. Writes `installation.json` with the active runtime paths and `env_file`.
4. Creates `.env` with generated secrets if missing, recording the selected backend (prompted for when `--backend` is omitted).
5. Installs the selected backend's CLI binary on demand if missing (codex → `~/.openbase/bin`, claude → Anthropic's installer). This is best-effort: on failure setup prints manual install instructions and continues.
6. For the codex backend, symlinks `~/.openbase/codex_home/auth.json` to `~/.codex/auth.json` so service Codex sessions use the normal Codex login.
7. Links normal `~/.claude/CLAUDE.md` to `~/.codex/AGENTS.md`, preserving an existing real Claude instructions file by copying it into Codex AGENTS when Codex AGENTS is missing or backing it up when both files differ.
8. Regenerates `~/.openbase/codex_home/AGENTS.md` from the bundled package or workspace `instructions/AGENTS.md`. The generated file records its source template path and, by default, includes normal `~/.codex/AGENTS.md` content above the Openbase section.
9. Links `~/.openbase/claude_config/CLAUDE.md` to Openbase's generated `~/.openbase/codex_home/AGENTS.md`.
10. Renders shared default instruction files from the bundled package or workspace `instructions/` into `~/.openbase/instructions/`.
11. Creates missing `~/.openbase/dispatcher-config.json` with default dispatcher reasoning effort `low`, default Super Agents reasoning effort `high`, and backend-specific default model settings.
12. Symlinks bundled or workspace skills into `~/.openbase/codex_home/skills` and `~/.openbase/claude_config/skills`.
13. Initializes runtime assets: in development mode runs `uv sync` in `cli`; in **both** modes downloads the LiveKit agent model files (VAD, turn detector) so the first voice session does not stall on downloads.
14. Installs the bundled `inject-session-id.sh` SessionStart hook script into `~/.openbase/hooks/` and registers it in both Openbase agent homes: a `hooks.SessionStart` entry in `~/.openbase/claude_config/settings.json` and a trusted `[[hooks.SessionStart]]` hook (with its `[hooks.state]` trust entry) in `~/.openbase/codex_home/config.toml`. The hook feeds each session's thread/session ID back into the conversation together with the instructions for using it, so agents stamp commits with the `Agent-Thread-Id` trailer without needing any standing `AGENTS.md` rule.
15. Configures `~/.openbase/codex_home/config.toml` with full Codex local access (`sandbox_mode = "danger-full-access"`), disabled permission prompts, and the Super Agents MCP server. With `--link-codex-config`, this path is first linked to `~/.codex/config.toml`. The MCP command prefers the selected workspace's venv executable and falls back to the resolved local `uv` path.
16. Configures `~/.openbase/claude_config/.claude.json` with the Super Agents MCP server and writes `CLAUDE_CONFIG_DIR=~/.openbase/claude_config` into the shared `.env`.
17. Merges normal Claude Code state from `~/.claude.json` into `~/.openbase/claude_config/.claude.json` (the file Claude Code reads under Openbase's `CLAUDE_CONFIG_DIR`) when available; existing Openbase values win and `mcpServers` entries are unioned. Claude Code OAuth uses config-dir-scoped credentials, so on macOS setup also copies the normal "Claude Code-credentials" keychain item to Openbase's config-dir-specific keychain service, inheriting the normal Claude login without a second browser OAuth. When `--backend claude-code` is selected and no login could be bridged, setup runs `openbase-coder claude login`. When `--backend openbase-cloud` is selected, setup prepares Claude Code config but relies on Openbase login and a Cloud proxy machine token rather than a personal Claude login.
18. Registers the Super Agents MCP server in the user's **normal** agent homes — a `[mcp_servers.super-agents]` table in `~/.codex/config.toml` and an `mcpServers.super-agents` entry in `~/.claude.json` — regardless of `--link-codex-config`. Only the MCP entry is written; normal permissions and settings are never touched. You may remove the entry; an explicit setup re-run restores it.
19. Installs or updates the `~/.local/bin/openbase-coder` shim: never overwrites a `uv tool install`-managed script; in standalone mode it points at the `current/` package launcher so it survives package upgrades; in development mode it execs the workspace `cli/.venv/bin/openbase-coder`.
20. Writes Codex app-server defaults like `CODEX_MODEL=gpt-5.5`, `CODEX_MODEL_REASONING_EFFORT=high`, `CODEX_SERVICE_TIER=standard`, `CODEX_APP_SERVER_URL`, and `LIVEKIT_CODEX_THREAD_CWD` into the shared `.env` for direct Codex compatibility. The visible `openbase_cloud` backend bypasses `codex-app-server`; the legacy Codex proxy path remains internal as `openbase_cloud_codex`.
21. Uses the bundled console build, or builds `console` in development mode.
22. Installs background services (launchd on macOS, systemd user units on Linux) unless skipped. Services gated to other backends (e.g. `codex-app-server` under `claude-code` or `openbase-cloud`) are not installed.
23. Configures Tailscale Serve routes for the iOS app:
    - `tailscale serve --bg --http=18080 http://127.0.0.1:7999`
    - `tailscale serve --bg --tcp=7880 tcp://127.0.0.1:7880`
24. Leaves Openbase Cloud registration to the later login/pairing flow. Use
    `openbase-coder onboarding report` after `openbase-coder login` when you
    need to register this device for iOS pairing. See
    [`onboarding`](onboarding.md).

## JSON Progress

With `--json-progress`, setup emits one NDJSON event per line on stdout so a
UI (e.g. the Mac app's one-click setup) can render a live checklist; all
human-readable output — including subprocess output — is redirected to
stderr. Step ids, in order: `workspace`, `installation_config`, `env`,
`agent_config`, `services`, `tailscale_serve`.

```jsonc
{"event": "step", "id": "services", "status": "start", "detail": null}
{"event": "step", "id": "services", "status": "ok", "detail": null}
{"event": "step", "id": "tailscale_serve", "status": "warn", "detail": "tailscale was not found on PATH."}
{"event": "result", "ok": true, "cli_configured": true, "tailscale_serve_healthy": false}
```

`warn` steps are non-fatal. A hard failure emits a final `error` step event
and `{"event": "result", "ok": false, ...}`, and exits nonzero. The full
protocol is specified in the workspace `specs/onboarding/README.md`.

The generated env file records the selected backend as `OPENBASE_CODING_BACKEND`.

## Example

Development-mode setup against an explicit checkout:

```bash
openbase-coder setup \
  --workspace-dir ~/Projects/openbase-coder-workspace \
  --env-file ~/.openbase/.env
```

## Notes

- If `.env` already exists, setup leaves it unchanged (including the backend,
  unless `--backend` is passed).
- `~/.openbase/codex_home/AGENTS.md` is a generated regular file from `instructions/AGENTS.md`; setup rewrites it and records the source template path. A console setting controls whether normal `~/.codex/AGENTS.md` content is included above the Openbase section; the default is enabled.
- `~/.openbase/claude_config/CLAUDE.md` is a symlink to `~/.openbase/codex_home/AGENTS.md`. Normal `~/.claude/CLAUDE.md` is kept symlinked to `~/.codex/AGENTS.md`.
- Shared default instruction files under `~/.openbase/instructions` are generated regular files with source-template comments.
- If `dispatcher-config.json` already exists, setup preserves it.
- Existing skill symlinks in `~/.openbase/codex_home/skills` and `~/.openbase/claude_config/skills` are updated to the bundled or workspace source. Real skill directories or files are left unchanged.
- Existing `~/.openbase/codex_home/config.toml` content is preserved, except setup enforces the root permission keys and creates or replaces the `[mcp_servers.super-agents]` table. Passing `--link-codex-config` makes `~/.openbase/codex_home/config.toml` a symlink to `~/.codex/config.toml`; if the normal Codex config is missing, setup seeds it from the existing Openbase config before linking. **Warning:** once linked, the enforced Openbase permission keys (`sandbox_mode = "danger-full-access"` and the no-prompt approval policy) are written into that shared normal Codex config.
- Independent of `--link-codex-config`, setup always registers the `super-agents` MCP server in the normal `~/.codex/config.toml` and `~/.claude.json`. This writes only the MCP entry — never Openbase permission overrides — and re-running setup restores the entry if it was removed.
- If `npm` or `uv` are missing in development mode, related steps are skipped with messages.
- If Tailscale is missing or disconnected, setup prints the manual Serve
  commands and continues. `openbase-coder doctor` and `openbase-coder services
  status` fail until the Tailscale Serve routes and external Openbase health
  check pass.

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

For a production macOS install with no terminal, use the desktop app: it
activates the bundled runtime automatically, and its guided flow runs
`openbase-coder setup` for you. To operate that same standalone install from a
terminal, follow [Manual Setup for the Desktop App](../manual-installation.md).
When a terminal and source checkout are available, the workspace developer
setup below is the recommended path.

For development, run the workspace script from your checkout root. It preserves
the checkout's Multi shape (`dev` when a dev-only repo is already present,
otherwise `default`) and then runs
`openbase-coder setup --workspace-dir <workspace-root>`:

```bash
./scripts/setup
```

The workspace script is for a clean source-workspace install. If it finds an
existing standalone install or a different development workspace install, it
stops and directs you to [Uninstall](../uninstall.md) before making changes.

## Interactive Mode

Setup is only interactive when run with no flags at all on a terminal, or
when `--interactive` is passed explicitly. Passing **any** other flag implies
`--non-interactive`, so scripted and AI-agent invocations never block on a
prompt: fresh non-interactive installs require `--backend` (setup errors
otherwise) and default the audio provider to `openbase-cloud`.
`./scripts/setup` passes `--interactive` for you when you give it no flags on
a terminal, since it always injects `--workspace-dir` itself.

After the phases complete, interactive runs also offer to run
`openbase-coder login` (browser OAuth; skipped if already logged in), then
verify the device registered with Openbase Cloud and that the selected
private-network transport exposes the local API and LiveKit, and print a
terminal QR code linking the phone app downloads page. Non-interactive runs —
including the desktop app's `--json-progress` onboarding, which renders its own
sign-in step — end with the plain login hint, unchanged.

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
new `~/.openbase/.env` with `--backend` omitted, interactive runs show a
numbered picker for the backend, and non-interactive runs (including
`--json-progress`) error asking for an explicit `--backend`. Existing env
files keep their configured backend and are only changed when `--backend` is
passed.

Setup installs the selected backend's CLI on demand if it is missing: `codex`
from its GitHub release binaries into `~/.openbase/bin`, `claude` via
Anthropic's official native installer. Backend-specific services (such as
`codex-app-server`) are only installed for the backends that use them;
`openbase-coder services status` shows `not used (<backend> backend)` for
gated-out services.

## Voice Audio Selection

Setup also configures the voice audio provider. On a fresh install (no
`~/.openbase/dispatcher-config.json` yet) with `--audio-provider` omitted,
interactive runs show a numbered picker:

- `openbase-cloud` (Cloud TTS/STT): managed speech-to-text and text-to-speech
  through Openbase Cloud — the recommended default.
- `cartesia` (bring your own keys): AssemblyAI speech-to-text and Cartesia
  text-to-speech with your own API keys. Picking this interactively also
  prompts for the AssemblyAI and Cartesia keys when they were not provided
  via options or environment variables, and writes them into the freshly
  generated `.env`.
- `local` (not recommended): on-device Kokoro TTS and MLX Whisper STT.

Non-interactive runs keep the `openbase-cloud` default, and existing
dispatcher configs are only changed when `--audio-provider` is passed.

With `--audio-provider local` (or the local picker choice), setup installs
the optional Kokoro/MLX local audio dependencies and downloads the required
models. This path requires an Apple Silicon Mac (MLX) and a Python 3.12
runtime — Kokoro currently declares Python `<3.13`, and setup refuses local
audio on newer runtimes. Standalone packages should be built with Python 3.12
to keep local audio available.

## Options

| Option | Default | Description |
|---|---|---|
| `--workspace-dir PATH` | discovered | Development workspace checkout. When omitted, discovered from the recorded installation, then an editable CLI install; ignored in standalone mode |
| `--env-file PATH` | `~/.openbase/.env` | Shared environment file path |
| `--assembly-ai-api-key TEXT` | env `ASSEMBLY_AI_API_KEY` | Optional STT key |
| `--cartesia-api-key TEXT` | env `CARTESIA_API_KEY` | Optional TTS key |
| `--skip-services` | `false` | Skip background service installation |
| `--fast-mode/--no-fast-mode` | `true` | Use the fast service tier for the voice dispatcher. Super Agents stay on the standard tier; both are adjustable in console settings (Codex backend only — Claude Code turns always run at the standard tier) |
| `--backend NAME` | prompted for new env files | Default coding backend: `codex`, `claude-code`, or `openbase-cloud`. Existing env files are only changed when provided |
| `--audio-provider NAME` | picker on fresh interactive installs, else `openbase-cloud` for new dispatcher configs | Voice audio provider. Existing configs are only changed when provided |
| `--interactive/--non-interactive` | interactive only for flagless terminal runs | Force or forbid the first-run pickers. Passing any other flag implies `--non-interactive` |
| `--json-progress` | `false` | Emit NDJSON step events on stdout for UI-driven setup; human-readable output moves to stderr |

## Behavior Details

`setup` runs on macOS (launchd) and Linux (systemd user services) and performs these phases:

1. Ensures `~/.openbase` exists, plus the thread-sync exchange folder and bundled sounds.
2. Detects the bundled runtime package (standalone mode), or resolves the development workspace checkout as described above. Never clones or updates a workspace.
3. Writes `installation.json` with the active runtime paths and `env_file`.
4. Creates `.env` with generated secrets if missing, recording the selected backend (prompted for when `--backend` is omitted) and Openbase's per-session posture for Super Agents: `SUPER_AGENTS_CODEX_APPROVAL_POLICY=never`, `SUPER_AGENTS_CODEX_SANDBOX_POLICY=danger-full-access`, and `SUPER_AGENTS_BASE_INSTRUCTIONS_PATH=~/.openbase/instructions/AGENTS.md`.
5. Installs the selected backend's CLI binary on demand if missing (codex → `~/.openbase/bin`, claude → Anthropic's installer). This is best-effort: on failure setup prints manual install instructions and continues.
6. Links `~/.claude/CLAUDE.md` to `~/.codex/AGENTS.md`, preserving an existing real Claude instructions file by copying it into Codex AGENTS when Codex AGENTS is missing or backing it up when both files differ.
7. Renders default instruction files from the bundled package or workspace `instructions/` into `~/.openbase/instructions/`, including the Openbase base instructions at `~/.openbase/instructions/AGENTS.md`. These are delivered to each Openbase session by super-agents (as Codex developer instructions or the Claude system prompt) — never written into the shared agent homes.
8. Creates missing `~/.openbase/dispatcher-config.json` with default dispatcher reasoning effort `low`, default Super Agents reasoning effort `high`, and backend-specific default model settings.
9. Symlinks bundled or workspace skills into `~/.codex/skills` and `~/.claude/skills`.
10. Initializes runtime assets: in development mode runs `uv sync` in `cli`; in **both** modes downloads the LiveKit agent model files (VAD, turn detector) so the first voice session does not stall on downloads.
11. Installs the bundled `inject-session-id.sh` SessionStart hook script into `~/.openbase/hooks/` and registers it in both shared agent homes: a trusted `[[hooks.SessionStart]]` hook (with its `[hooks.state]` trust entry) in `~/.codex/config.toml` and a `hooks` entry in `~/.claude/settings.json`. The hook feeds each session's thread/session ID back into the conversation together with the instructions for using it, so agents stamp commits with the `Agent-Thread-Id` trailer without needing any standing `AGENTS.md` rule. In Claude Code sessions, it also exports the ID as the vendor-neutral `AGENT_SESSION_ID` so subsequent agent-aware CLI calls can carry attribution.
12. Registers the Super Agents MCP server in the shared agent homes — a `[mcp_servers.super-agents]` table in `~/.codex/config.toml` and an `mcpServers.super-agents` entry in `~/.claude.json`. Each entry identifies its spawning backend (`codex` or `claude_code`) so child threads inherit it unless a launch explicitly overrides the backend, and carries the per-session Openbase posture env. Only the MCP entry and the session-ID hook are written; your own model, sandbox, approval, and permission settings are never touched. The MCP command prefers the selected workspace's venv executable and falls back to the resolved local `uv` path.
13. Checks backend auth against the shared homes: Codex sessions use `~/.codex/auth.json` (run `codex login`), and Claude Code sessions use your own Claude Code login. When `--backend claude-code` is selected and no login is present, setup tells you to run `claude login`. When `--backend openbase-cloud` is selected, Claude Code runs against the Openbase Anthropic proxy with an Openbase machine token rather than a personal Claude login.
14. Installs or updates the `~/.local/bin/openbase-coder` shim: never overwrites a `uv tool install`-managed script; in standalone mode it points at the `current/` package launcher so it survives package upgrades; in development mode it execs the workspace `cli/.venv/bin/openbase-coder`.
15. Writes Codex app-server defaults like `CODEX_MODEL_REASONING_EFFORT=high`, `CODEX_SERVICE_TIER=standard`, `CODEX_APP_SERVER_URL=unix://` on macOS/Linux, and `LIVEKIT_CODEX_THREAD_CWD` into the shared `.env`. Existing default `ws://127.0.0.1:4500` installations migrate to the standard socket; explicit custom WebSocket endpoints are preserved. Backend model/provider configuration is applied by the service as `codex app-server -c` launch overrides, never written into `~/.codex/config.toml`. The visible `openbase_cloud` backend bypasses `codex-app-server`; the legacy Codex proxy path remains internal as `openbase_cloud_codex`.
16. Uses the bundled console build, or builds `console` in development mode.
17. Installs background services (launchd on macOS, systemd user units on Linux) unless skipped. Services gated to other backends (e.g. `codex-app-server` under `claude-code` or `openbase-cloud`) are not installed.
18. Configures the selected transport's private routes for the phone apps.
    The expert `tailscale` provider applies Tailscale Serve routes immediately.
    Openbase VPN and Openbase Direct save the choice during setup, then enroll
    and apply routes during the signed-in pairing flow.
19. Leaves Openbase Cloud registration to the later login/pairing flow. Use
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
- `~/.openbase/instructions/AGENTS.md` (the Openbase base instructions) is a generated regular file from `instructions/AGENTS.md`; setup rewrites it and records the source template path. It is editable afterwards via the console's "Openbase base instructions" target and is delivered per session, never written into the shared agent homes.
- Your own `~/.codex/AGENTS.md` applies natively to every Codex session in the shared home, including Openbase's. `~/.claude/CLAUDE.md` is kept symlinked to `~/.codex/AGENTS.md`.
- Shared default instruction files under `~/.openbase/instructions` are generated regular files with source-template comments.
- If `dispatcher-config.json` already exists, setup preserves it.
- Existing skill symlinks in `~/.codex/skills` and `~/.claude/skills` are updated to the bundled or workspace source. Real skill directories or files are left unchanged.
- Setup registers exactly two things in each shared agent home: the `super-agents` MCP server (`~/.codex/config.toml`, `~/.claude.json`) and the session-ID hook (`~/.codex/config.toml`, `~/.claude/settings.json`). It never touches your own model, sandbox, approval, or permission settings — Openbase's session posture (`SUPER_AGENTS_CODEX_APPROVAL_POLICY=never`, `SUPER_AGENTS_CODEX_SANDBOX_POLICY=danger-full-access`) is passed per session by super-agents via env. You may remove the entries; an explicit setup re-run restores them.
- If `npm` or `uv` are missing in development mode, related steps are skipped with messages.
- With the expert `tailscale` provider, setup fails with install/connect
  guidance when it cannot configure Serve. With Openbase VPN or Openbase
  Direct, setup saves the networking choice and pairing completes enrollment
  after login. `openbase-coder doctor` and `openbase-coder services status`
  remain unhealthy until the selected transport's routes and external
  Openbase health check pass.

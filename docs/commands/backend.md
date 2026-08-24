# backend

View or switch the selected coding backend.

In the apps: **Settings → Coding Backend** in the
[desktop app](../desktop-app.md) and [console](../console.md) switches the
same setting; the desktop onboarding flow chooses it during first-time setup.

## Usage

```bash
openbase-coder backend status
openbase-coder backend list
openbase-coder backend use codex
```

## Options

`status` and `use` accept:

| Option | Default | Description |
|---|---|---|
| `--env-file PATH` | `~/.openbase/.env` | Openbase `.env` file to inspect or update |

## Supported Backends

- `codex`: default native Codex app-server backend.
- `openbase_cloud`: Cloud-proxied Claude Code through Openbase Cloud, authenticated with Openbase login.
- `claude_code`: Claude Code backend for Super Agents UI-driver sessions using local Claude auth/billing, not `ANTHROPIC_API_KEY`.

The command only updates `~/.openbase/.env`, persisting the selection as
`OPENBASE_CODING_BACKEND=<backend>` — the same setting written by
`openbase-coder setup --backend ...` and read by the local console. Restart
Openbase services to apply it. Backend model/provider configuration is
applied by the service as `codex app-server -c` launch overrides; it is
never written into your `~/.codex/config.toml`.


The backend setting controls `super-agents-mcp` coding sessions. Codex uses the
local `codex-app-server` service. Openbase Cloud and direct Claude Code use
Claude Code for Super Agents UI-driver sessions and bypass `codex-app-server`.
In the apps, saving a changed backend first asks for confirmation, then automatically restarts Openbase
services and recreates the dispatcher thread. The restart interrupts active
voice calls, may interrupt coding turns, and clears the current dispatcher
conversation context; it does not delete Super Agent threads or project files.
Separately running Codex or Claude clients may still need to be reopened so
their MCP process reloads the backend.

When switching with the CLI, restart Openbase services and recreate the
dispatcher explicitly so the new environment is loaded:

```bash
openbase-coder restart --recreate-dispatcher
```

For Claude Code, Openbase uses your own shared `~/.claude` home and your own
Claude Code login (`claude login`). Check it with:

```bash
openbase-coder claude status
openbase-coder claude login
```

For Codex, Openbase uses your own `~/.codex/auth.json` — just run
`codex login`.

Openbase Cloud does not require a personal Claude or Anthropic login. It runs
Claude Code through the Openbase Anthropic proxy, authenticated
with an Openbase machine token. The legacy Codex-over-Openbase-Cloud proxy path
remains available internally as `openbase_cloud_codex` for compatibility but is
not listed as a normal backend choice.

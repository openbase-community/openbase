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

`use` additionally accepts:

| Option | Description |
|---|---|
| `--skip-login-check` | Switch even if the target backend has no usable login |
| `--no-restart` | Persist the choice without restarting the dispatcher services |

## Supported Backends

- `codex`: default native Codex app-server backend.
- `openbase_cloud`: Cloud-proxied Claude Code through Openbase Cloud, authenticated with Openbase login.
- `claude_code`: Claude Code backend for Super Agents UI-driver sessions using local Claude auth/billing, not `ANTHROPIC_API_KEY`.

`use` first checks that the target backend has a usable login (Claude Code
login for `claude_code`, `~/.codex/auth.json` for `codex`, the Openbase
login for `openbase_cloud`) and fails without changing anything when it is
missing; pass `--skip-login-check` to switch anyway. It then persists the
selection into `~/.openbase/.env` as `OPENBASE_CODING_BACKEND=<backend>` —
the same setting written by `openbase-coder setup --backend ...` and read by
the local console — and schedules a dispatcher restart so the change takes
effect. Backend model/provider configuration is applied by the service as
`codex app-server -c` launch overrides; it is never written into your
`~/.codex/config.toml`.


The backend setting controls `super-agents-mcp` coding sessions. Codex uses the
local `codex-app-server` service. Openbase Cloud and direct Claude Code use
Claude Code for Super Agents UI-driver sessions and bypass `codex-app-server`.
In the apps, saving a changed backend first asks for confirmation, then automatically restarts Openbase
services and recreates the dispatcher thread. `backend use` does the
equivalent on the CLI: it restarts the dispatcher services and recreates the
dispatcher thread on the new backend while the rest of the Openbase services
keep running (use `--no-restart` to only persist the setting). The restart
interrupts active voice calls and clears the current dispatcher
conversation context; it does not delete Super Agent threads or project files.
Separately running Codex or Claude clients may still need to be reopened so
their MCP process reloads the backend.

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

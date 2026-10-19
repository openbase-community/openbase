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
| `--no-restart` | Persist the choice without applying it to the running services |

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
the local console — and applies the change to the running services (see below). Backend model/provider configuration is applied by the service as
`codex app-server -c` launch overrides; it is never written into your
`~/.codex/config.toml`.


The backend setting controls `super-agents-mcp` coding sessions. Codex uses the
local `codex-app-server` service. Openbase Cloud and direct Claude Code use
Claude Code for Super Agents UI-driver sessions and bypass `codex-app-server`.
In the apps, saving a changed backend first asks for confirmation, then applies
the switch in place: the selected engine's app-servers come up (and the other
engine's go down), and the dispatcher thread is recreated on the new backend.
`backend use` does the equivalent on the CLI (use `--no-restart` to only persist
the setting). No Openbase service restarts for the switch: `django-cli` and the
voice worker read the backend from `~/.openbase/.env` and rebuild their backend
clients for the next turn or call, so the switch takes effect within a few
seconds; turns still running on the previous backend lose their live output. Only a voice call in progress is ended,
by restarting the voice worker, because that call is bound to the previous
backend. The switch clears the current dispatcher conversation context; it does
not delete Super Agent threads or project files.
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

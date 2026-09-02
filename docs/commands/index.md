# Commands Overview

Many of these operations also have UI equivalents in the
[desktop app](../desktop-app.md) and [web console](../console.md) (service
control, backend and model selection, settings) and in the
[iOS app](../ios-tabs.md) (threads, approvals, voice). The CLI is the layer
underneath all of them.

Openbase CLI command structure:

```bash
openbase-coder [OPTIONS] COMMAND [ARGS]
```

## Global Options

| Option | Description |
|---|---|
| `--version` | Print CLI version and exit |
| `--help` | Show help |

## Top-Level Commands

| Command | Description |
|---|---|
| [`backend`](backend.md) | View or switch the selected coding backend |
| [`claude`](claude.md) | Inspect the Claude Code login used by Openbase sessions |
| [`claude-sync`](claude-sync.md) | Synchronize Claude Code session snapshots across devices |
| [`codex-sync`](codex-sync.md) | Synchronize Codex session snapshots across devices |
| [`defaults`](defaults.md) | Manage default dispatcher and Super Agents model/reasoning settings |
| [`setup`](setup.md) | Full local bootstrap flow |
| [`server`](server.md) | Run local Django/ASGI server |
| [`service`](service.md) | Publish a local HTTP service privately over the Openbase VPN |
| [`restart`](restart.md) | Restart Openbase-managed services |
| [`self-update`](self-update.md) | Update a standalone install to the latest release |
| [`version`](self-update.md) | Print CLI and package versions plus update availability |
| [`services`](services.md) | Manage launchd services |
| [`doctor`](doctor.md) | Verify install, service health, and secrets |
| [`onboarding`](onboarding.md) | Inspect onboarding state and report it to Openbase cloud |
| [`login`](login.md) | Email-code login to Openbase cloud |
| [`loops`](loops.md) | Manage loops: scheduled or event-triggered agent prompts and commands (alias: `routines`) |
| [`logout`](logout.md) | Remove saved auth tokens |
| [`plugins`](plugins.md) | Install and manage Openbase plugins |
| [`bootstrap`](bootstrap.md) | Run plugin-provided bootstrap commands |
| [`voice routing`](../voice-routing.md) | Route the active LiveKit voice room between the dispatcher and Super Agents |

## Common Examples

```bash
# Full bootstrap
openbase-coder setup

# Start API server
openbase-coder server --host 0.0.0.0 --port 7999

# Check service states
openbase-coder services status

# Share a local development site with other tailnet devices
openbase-coder service publish docs-preview 3000

# Switch coding backend
openbase-coder backend use codex

# Restart Openbase-managed services
openbase-coder restart

# Check or change the active voice route
openbase-coder user voice-route
openbase-coder user transfer-to-agent "Lucy"
openbase-coder exit-to-dispatch

# Tail logs for one service
openbase-coder services logs django-cli

# Validate local environment
openbase-coder doctor

# Install plugin from local repo
openbase-coder plugins add ~/code/my-openbase-plugin

# List plugin-provided bootstrappers
openbase-coder plugins bootstrappers

# Run a bootstrapper
openbase-coder bootstrap django-app --params-file bootstrap.json
```

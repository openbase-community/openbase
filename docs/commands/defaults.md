# defaults

Manage default dispatcher and Super Agents model/reasoning settings.

In the apps: **Settings → Backend Model / Service Tier / Reasoning** in the
[desktop app](../desktop-app.md) and [console](../console.md) edit the same
settings.

Service tiers (Fast mode) apply to the Codex backend only; Claude Code turns
always run at the standard tier. Reasoning levels apply to both backends.

## Usage

```bash
openbase-coder defaults COMMAND [ARGS]
```

## Commands

| Command | Description |
|---|---|
| `dispatcher-reasoning [LEVEL]` | Show or set the default dispatcher reasoning effort |
| `dispatcher-model [MODEL]` | Show or set the default dispatcher model |
| `super-agents-reasoning [LEVEL]` | Show or set the default Super Agents reasoning effort |
| `super-agents-model [MODEL]` | Show or set the default Super Agents model |

## Examples

```bash
openbase-coder defaults dispatcher-reasoning low
openbase-coder defaults dispatcher-model gpt-5.5
openbase-coder defaults super-agents-reasoning high
openbase-coder defaults super-agents-model opus
```

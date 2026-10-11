# defaults

Manage default dispatcher and Super Agents model/reasoning settings, and the voice model used on calls.

In the apps: **Settings → Backend Model / Service Tier / Reasoning** and
**Settings → Voice** in the [desktop app](../desktop-app.md) and
[console](../console.md) edit the same settings.

Service tiers (Fast mode) apply to the Codex backend only; Claude Code turns
always run at the standard tier. Reasoning levels apply to both backends.

The model implies the engine. The picker catalog offers the latest Terra (`gpt-5.6-terra`), Luna (`gpt-6-luna`), Sol (`gpt-6.1-sol`), and Astra (`gpt-6-astra`), plus Claude Haiku 4.5 (`claude-haiku-4-5-20251001`), Sonnet 5 (`claude-sonnet-5`), Opus 5.5 (`claude-opus-5-5`), and Fable 5.1 (`claude-fable-5-1`). The runtime API supplies the same options to iOS, Android, console, and desktop. Legacy IDs and family aliases still resolve for existing threads and stored configuration, but are never added to picker options.

Fresh Openbase Cloud installs default both dispatcher and Super Agents to Claude Haiku, and the Settings UI marks it as the default. Free and trial accounts can select Haiku. The catalog disables Sonnet, Opus, and Fable with a paid-plan reason. Existing Sonnet configurations retain a Haiku compatibility fallback for older clients.

## Usage

```bash
openbase-coder defaults COMMAND [ARGS]
```

## Commands

| Command | Description |
|---|---|
| `dispatcher-reasoning [LEVEL]` | Show or set the default dispatcher reasoning effort |
| `dispatcher-model [MODEL] [--backend BACKEND]` | Show or set the default dispatcher model |
| `super-agents-reasoning [LEVEL]` | Show or set the default Super Agents reasoning effort |
| `super-agents-model [MODEL] [--backend BACKEND]` | Show or set the default Super Agents model |
| `voice-model [MODEL]` | Show or set the voice model used on calls: `gpt-live-1` (default) or `pipeline`. Without an argument it lists the options with the current one starred and the default marked |

## Voice Model

The voice model is picked like the agent model: one selectable id whose engine follows from it. `gpt-live-1` runs GPT-Live, a full-duplex model that listens and speaks at the same time while Super Agents do the work, always through Openbase Cloud with your Openbase account; `pipeline` keeps the classic speech-to-text, agent turn, text-to-speech path, which is the only option for local-only audio and the only one that uses the STT and TTS provider settings. Aliases such as `live`, `gpt-live`, and `classic` are accepted and normalized. Changes apply to the next voice call; no restart is needed. The same settings live under **Settings → Voice** in the desktop app and console.

## Options

| Option | Command | Description |
|---|---|---|
| `--backend BACKEND` | `dispatcher-model`, `super-agents-model` | Configure the named backend instead of the selected coding backend |

## Examples

```bash
openbase-coder defaults dispatcher-reasoning low
openbase-coder defaults dispatcher-model gpt-6.1-sol
openbase-coder defaults super-agents-reasoning high
openbase-coder defaults super-agents-model opus
openbase-coder defaults super-agents-model sol
openbase-coder defaults voice-model
openbase-coder defaults voice-model pipeline
```

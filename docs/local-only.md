# Local-Only Mode

Local-only mode keeps voice audio and coding-agent inference on the Mac. It is
useful when you want to avoid cloud speech or model providers, but it is less
fun than the managed path: Kokoro voice quality is lower than Cartesia or
Openbase Cloud audio, local MLX Whisper is not streaming in the same way, and a
call usually takes longer to set up because the local models need to load.

Local audio requires an Apple Silicon Mac and a Python 3.12 Openbase Coder
runtime: MLX Whisper only runs on Apple Silicon, and Kokoro declares Python
`<3.13`. Setup and the console settings refuse local audio on Python 3.13 or
newer; on Linux or Intel Macs the MLX dependencies are not available. Use the
Openbase Cloud or Cartesia audio providers on those machines.

## Use Local STT and TTS From the GUI

The recommended path is the GUI — the [desktop app](desktop-app.md) during
first-time setup, or the [console](console.md) Settings page afterward. It
handles the provider selection, downloads, and dispatcher restart wiring for
you.

During first-time desktop setup:

1. Open the Openbase desktop app.
2. Continue to the voice audio step.
3. Choose `Local audio`.
4. Run setup from the desktop app.

That setup path selects:

- Speech-to-text: `Local MLX Whisper`
- Text-to-speech: `Local Kokoro`

If Openbase is already installed:

1. Open the local Openbase console.
2. Go to `Settings`.
3. In `Text-to-speech provider`, choose `Local Kokoro`.
4. Click `Download` if the Kokoro model or voices are missing.
5. Pick a voice and click `Save voice`.
6. In `Speech-to-text provider`, choose `Local MLX Whisper`.
7. Click `Download` if the MLX Whisper model is missing.
8. Use `LiveKit dispatcher thread` -> `Recreate thread`.

After the dispatcher is recreated, start a new call. The first local-only call
after downloads can still take longer while models warm up.

## Run the Coding Agent Locally With Codex

Openbase uses the Codex app-server for the `codex` backend. To run that coding
agent against a local Ollama model, configure Codex for Ollama and make sure the
Openbase service Codex home uses that config.

Install and start Ollama, then confirm the model exists:

```bash
ollama list | rg '^qwen3-coder:30b-a3b-q4_K_M\s'
```

If it is missing:

```bash
ollama pull qwen3-coder:30b-a3b-q4_K_M
```

Configure Codex for the Ollama model:

```bash
ollama launch codex --config --model qwen3-coder:30b-a3b-q4_K_M
```

Check `~/.codex/config.toml` and confirm the active top-level settings point to
Ollama:

```toml
model = "qwen3-coder:30b-a3b-q4_K_M"
model_provider = "ollama-launch"
model_catalog_json = "~/.codex/ollama-launch-models.json"
```

The model catalog matters because it gives Codex metadata for the exact Ollama
model slug, including the large context window. Without it, Codex may fall back
to generic metadata.

Smoke-test normal Codex first:

```bash
codex exec --skip-git-repo-check --cd . 'Reply with exactly OK.'
codex debug models | jq '.models[] | select(.slug=="qwen3-coder:30b-a3b-q4_K_M")'
codex --strict-config doctor --summary --ascii
```

Then make Openbase use the Codex backend and the same Codex config:

```bash
openbase-coder backend use codex
openbase-coder setup --link-codex-config
openbase-coder services restart codex-app-server
```

`--link-codex-config` points Openbase's service Codex config at the normal Codex
config under `~/.codex/config.toml`. If you prefer separate configs, apply the
same Ollama `model`, `model_provider`, and `model_catalog_json` settings in
`~/.openbase/codex_home/config.toml` instead.

## Revert to Managed Models

To switch the coding agent back to OpenAI-backed Codex, update the active Codex
config so the Ollama lines are commented out and the managed model is selected:

```toml
# model = "qwen3-coder:30b-a3b-q4_K_M"
# model_provider = "ollama-launch"
# model_catalog_json = "~/.codex/ollama-launch-models.json"
model = "gpt-5.5"
```

Then restart the Codex service:

```bash
openbase-coder services restart codex-app-server
```

To switch voice audio back, use `Settings` in the GUI and choose Openbase Cloud
or provider-key audio for STT and TTS, then recreate the dispatcher thread.

## Troubleshooting

If Codex cannot connect to Ollama:

```bash
curl http://127.0.0.1:11434/api/version
ollama run qwen3-coder:30b-a3b-q4_K_M
```

Exit the Ollama prompt with `/bye`.

If Codex reports missing model metadata, compare the model slug in the Codex
config with the installed Ollama model and the Codex catalog:

```bash
ollama list
codex debug models | jq '.models[].slug'
```

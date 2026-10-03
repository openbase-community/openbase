# Voice Routing

Openbase Coder voice sessions normally start with the LiveKit dispatcher. The
dispatcher is the routing agent for the private voice room: it can start or
resume Super Agents, transfer the active voice route to one of them, and accept
the route back when the user is done speaking directly with that agent.

These commands affect only the private LiveKit voice route for the active room.
They do not publish code, send public messages, or change product behavior.

In the apps: the [iOS app's](ios-tabs.md) Call tab is where you actually hold
the voice session — you can ask the dispatcher to transfer you by voice, use
**Transfer Active Call** from a thread's detail view, or **Return to
Dispatch**. The [desktop app](desktop-app.md) and [console](console.md) show
the dispatcher thread as text chat on their Dispatch page. The commands below
are how agents (and scripts) drive the same routing.

## Check The Current Route

```bash
openbase-coder user voice-route
```

The command prints whether the active route is the dispatcher or a target
thread. It also shows the dispatcher thread ID and active target thread ID when
they are known.

Most commands default to the latest active LiveKit room. Use `--room` only when
you need to target a specific room:

```bash
openbase-coder user voice-route
openbase-coder user transfer-to-agent "Lucy" --room "openbase-room-name"
openbase-coder user exit-to-dispatch --room "openbase-room-name"
```

## Name A Super Agent

Super Agent thread names and speaking agent names are related but different.
The thread name is the durable work label, while the speaking agent name chooses
the voice identity used in the LiveKit room.
The speaking name is always derived deterministically from the exact thread name; choosing an unrelated person name is not a separate user-facing option.

Before creating, transferring to, or referring to a Super Agent by a thread
name, derive the speaking name:

```bash
openbase-coder super-agent-name "document-voice-routing-and-glossary"
openbase-coder super-agent-name "document-voice-routing-and-glossary" --json
```

Use the returned `agent_name` when calling Super Agents MCP tools and voice
transfer commands.

## Transfer Voice To A Super Agent

Transfer by speaking agent name when you know the active Super Agent voice:

```bash
openbase-coder user transfer-to-agent "Lucy"
```

Transfer by thread ID when you need to target a specific Codex app-server
thread:

```bash
openbase-coder user transfer-to-thread "019f1aec-fb5c-78a2-8dc6-8d52f46a22ee"
```

You can provide display context for a thread transfer:

```bash
openbase-coder user transfer-to-thread \
  "019f1aec-fb5c-78a2-8dc6-8d52f46a22ee" \
  --label "document-voice-routing-and-glossary" \
  --agent-name "Lucy"
```

After transfer, the user is speaking directly to that target thread over the
same LiveKit room. The dispatcher is no longer the active voice route until the
route is returned.

Direct voice instructions ask about unclear transcript portions while continuing clear, independent, reversible work. For example, an unclear note filename should not block a separately specified heading change and production build. Explicit spoken spelling or a correction takes precedence over a conflicting phonetic transcription. The ambiguous portion still requires clarification. Custom direct-voice instruction files override the built-in fallback, so update those separately when testing this behavior.

## Return To The Dispatcher

From any direct Super Agent voice route, return the active private voice session
to the dispatcher with:

```bash
openbase-coder user exit-to-dispatch
```

There is also a top-level alias for agents that need a shorter command:

```bash
openbase-coder exit-to-dispatch
```

Use this when the user says to go back to dispatch, return to the dispatcher,
stop talking to the current Super Agent, or otherwise hand routing back to the
main voice dispatcher. Agents should omit `--room` unless they are intentionally
targeting a specific LiveKit room.

## Speak Into The Voice Session

Agents can make a short spoken announcement in the active private voice session:

```bash
openbase-coder user say "Lucy" "I finished the documentation update."
```

The first argument is the speaking agent name. The remaining words are the
message to speak. This is useful for Super Agent introductions, plan-mode
questions, completion notices, and brief requests for user attention. If no
voice room is active, the command sends the same message as a phone alert
that opens the speaking agent's thread. This fallback requires an Openbase
Cloud login and a phone (iPhone or Android) registered for notifications; if
either delivery path fails, the command exits with an error instead of
claiming success.

For local audio cues:

```bash
openbase-coder user play success
openbase-coder user play /path/to/sound.wav
```

## A Guided Meditation While A Long Task Runs

When the dispatcher hands a task to a new Super Agent, the agent's first
turn greets you ("Hey there, I'm Dottie.") and Openbase Coder quietly asks
[Jev](https://docs.typesafe.ai/), TypeSafe AI's System One decision model,
how long the task will take: a score over duration buckets plus a calibrated
yes/no answer to "longer than a minute and a half". When Jev says the task
is a long one, Openbase Coder writes a short guided meditation from the
recent conversation and plays it over the same call while the agent works. The
meditation moves through three themes: letting go of attachment to the work,
gratitude that the work is being done, and the people you will connect with
and influence by doing it. It ends with a gentle return, ready for the agent
to report back.

The meditation is written by GPT-6 Sol at medium reasoning effort and
spoken by an ElevenLabs voice, with the pauses in the script rendered as real
silence. Set `JEV_API_KEY` (or `TYPESAFE_API_KEY`) and `ELEVENLABS_API_KEY`
in the Openbase `.env` file; without a Jev key a fast Codex model estimates
instead, and without an ElevenLabs key the script is saved but nothing
plays. Scripts, audio, and a
JSON record of each run land in `~/.openbase/meditations/`.

Tuning, in `~/.openbase/.env` or under `"task_meditation"` in
`~/.openbase/dispatcher-config.json`:

| Setting | Default | Purpose |
| ------- | ------- | ------- |
| `OPENBASE_TASK_MEDITATION_ENABLED` / `enabled` | `true` | Turn the feature off entirely |
| `OPENBASE_TASK_MEDITATION_THRESHOLD_SECONDS` / `threshold_seconds` | `90` | Minimum estimated task length that earns a meditation |
| `OPENBASE_TASK_ESTIMATE_JEV_MODEL` / `jev_model` | `jev-latest` | Jev model that estimates the task length |
| `OPENBASE_TASK_MEDITATION_DECISION_PROBABILITY` / `decision_probability` | `0.5` | Jev's probability that the task runs past the threshold at which the meditation plays |
| `OPENBASE_TASK_ESTIMATE_MODEL` / `estimator_model` | `gpt-5.5` | Codex model used for the estimate when no Jev key is set |
| `OPENBASE_TASK_ESTIMATE_REASONING_EFFORT` / `estimator_reasoning_effort` | `low` | Reasoning effort for that fallback estimate |
| `OPENBASE_TASK_MEDITATION_MODEL` / `meditation_model` | `gpt-6-sol` | Codex model that writes the meditation |
| `OPENBASE_TASK_MEDITATION_REASONING_EFFORT` / `meditation_reasoning_effort` | `medium` | Reasoning effort for the meditation |
| `ELEVENLABS_MEDITATION_VOICE_ID` / `voice_id` | Sarah | ElevenLabs voice for the meditation |

To hear one on demand, or to check a voice without waiting for a long task:

```bash
openbase-coder meditation run --thread-name "Refactor the billing module" --agent-name Dottie --force
openbase-coder meditation render ~/.openbase/meditations/<script>.txt --play
```

`meditation run --force` skips the estimate and always produces one;
`--no-play` renders the WAV without playing it. `meditation render` turns a
saved script (with `<pause N seconds>` markers) back into audio without any
model call. The worker's log is `~/.openbase/logs/task-meditation.log`.

## Ring The User For An Urgent Voice Handoff

Use an inbound call only when the user explicitly asked to be called or the
task is urgent enough to justify ringing their phone:

```bash
openbase-coder user call "Lucy"
```

The agent name must resolve to an existing resumable thread. Openbase Coder
stores that route locally, asks Openbase Cloud to send a short-lived VoIP
invitation to the user's registered iPhones, and reports how many devices
accepted the invitation. The push does not contain a thread ID, local path,
LiveKit credential, or room-routing instruction. If the user answers, the app
connects to the local dispatcher room first and activates the stored agent
route only after that room is connected.

Command success means Cloud accepted the ring request; it does not prove that
an iPhone displayed or answered it. The command requires Cloud login, a signed
iOS app with PushKit enabled, a registered device, and a reachable local
Openbase Coder runtime. A declined or expired invitation cannot be reused.

## Typical Voice Handoff

1. The dispatcher starts or finds a Super Agent thread.
2. The dispatcher derives the speaking name:

   ```bash
   openbase-coder super-agent-name "implement-my-feature" --json
   ```

3. The dispatcher transfers voice to the agent:

   ```bash
   openbase-coder user transfer-to-agent "Lucy"
   ```

4. The Super Agent talks with the user and works in its thread.
5. The Super Agent or dispatcher returns voice routing:

   ```bash
   openbase-coder exit-to-dispatch
   ```

## Related Commands

- `openbase-coder user voice-route`: inspect the active LiveKit voice route.
- `openbase-coder super-agent-name THREAD_NAME`: derive a Super Agent speaking
  name from a thread name.
- `openbase-coder user transfer-to-agent AGENT_NAME`: route voice to a named
  Super Agent.
- `openbase-coder user transfer-to-thread THREAD_ID`: route voice to a specific
  thread.
- `openbase-coder user exit-to-dispatch`: route voice back to the dispatcher.
- `openbase-coder exit-to-dispatch`: top-level alias for returning to the
  dispatcher.
- `openbase-coder user say AGENT_NAME MESSAGE`: speak a short announcement in
  the active room, or send a thread-linked phone notification when no room is
  active.
- `openbase-coder user call AGENT_NAME`: explicitly ring registered phones
  (iPhone or Android) for an urgent, short-lived handoff to an existing agent
  thread.

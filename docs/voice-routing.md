# Voice Routing

A voice call belongs to the conversation you start it from. Start a call from a project thread and you are talking to that thread: what you say becomes turns in it, its agent answers in its own voice, and the transcript stays there. Start a call from the dispatcher and you are talking to the dispatcher, which is a pinned conversation like any other. The dispatcher is also the routing agent for the private voice room: it can start or resume Super Agents, transfer the active voice route to one of them, and accept the route back when you are done speaking directly with that agent.

These commands affect only the private LiveKit voice route for the active room.
They do not publish code, send public messages, or change product behavior.

In the apps: the [iOS app's](ios-tabs.md#calls) call, started from the waveform button on the new-chat screen, is where you actually hold the voice session — you can ask the dispatcher to transfer you by voice, use **Transfer Active Call** from a thread's detail view, or **Return to Dispatch**. The [desktop app](desktop-app.md) and [console](console.md) show the dispatcher thread as text chat on their Dispatch page. The commands below are how agents (and scripts) drive the same routing.

Calls default to the **GPT-Live** voice model: one full-duplex model listens and speaks for the whole call while the dispatcher and Super Agents do the work behind it, so the routing below is unchanged. Everything you say goes to the agent on the call, which answers with its own tools, skills and files; the voice model only speaks the agent's answers and never answers from its own knowledge. The classic speech-to-text, agent turn, text-to-speech pipeline remains selectable (`openbase-coder defaults voice-model pipeline`, or **Settings → Voice** in the apps) and is the only option for local-only audio; the STT and TTS provider settings apply to that pipeline only. See [defaults](commands/defaults.md) and [configuration](configuration.md#dispatcher-config).

On the phone, the call button at the bottom of a thread starts a call in that thread: the first thing you say reaches that thread's agent, and the call shows that thread as its active voice conversation. If the thread cannot be resolved before connection (for example it no longer exists), the call does not start and the app shows why. If the thread becomes unreachable while the voice agent joins, the call stays on the dispatcher and announces the failure; it never falls back silently. Preparing a call does not change an existing call's active route. Opening a different thread during a call does not move the call: use **Transfer call here** in that thread's menu to move the voice route there, and **Back to Dispatch** in the call settings to return to the dispatcher. On a dispatcher call, the dispatcher also receives the identity of the thread open on your screen (iOS), so requests about "this thread" can continue that conversation and relay its answer; opening another thread updates this context and leaving chat clears it.

The dispatcher, agents receiving a direct voice transfer, and the voice model receive context about the computer hosting the call. On a Cloud workspace, local file checks describe that workspace, not your personal computer's desktop or screen. The agent can use available [laptop tools](laptop-tools.md) to reach your other computer; if that access is unavailable, it explains the limitation and offers workspace files or suggests connecting to Openbase on your personal computer. The voice model still waits for the agent's answer.

If a pause splits a GPT-Live request, a brief continuation can update the request already being handled. Codex accepts it during the running turn; Claude Code interrupts the partial request and receives the complete request. An interruption does not undo actions already performed before the continuation arrived.

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

## Choose A Super Agent's Project

The dispatcher always runs in its own directory (the projects folder on a Cloud workspace, your home directory on a Mac); nothing you say moves it. When it starts a Super Agent, it chooses that agent's working directory itself by looking around: it lists its own directory, the folders where you keep projects, any place you named, and your [recent projects](files-and-paths.md), and matches what you meant to a real folder. Names do not have to match exactly, so a misheard "tick tack toe" still finds `tic-tac-toe`, and a partial name finds the folder it clearly refers to. The dispatcher asks only when two folders are genuinely plausible, and it never starts the agent in its own directory just because nothing matched exactly; for a new project it creates the folder first.

## Transfer Voice To A Super Agent

Creating a Super Agent thread does not by itself give it work. The dispatcher passes the task as `prompt` to `super_agents_start`, which creates the thread and starts its first turn, or follows creation with `super_agents_start_turn`. It confirms that the agent is working only after the tool reports a started turn. Standing `developerInstructions` do not count as a task. In the mobile apps, a thread with no past, current, or queued turns explains that the agent has not been given a task yet; sending a message starts the work.

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

The first argument is the speaking agent name. The remaining words are the message to speak. Announcements use that agent's stable assigned voice. On GPT-Live calls its Cartesia identity maps to the matching GPT-Live character; a bounded announcement session speaks, then the active conversation resumes in its own voice. This never transfers the call to the announcing agent. The caller can interrupt announcements. This is useful for introductions, plan-mode questions, completion notices, and brief requests for attention. If no voice room is active, the command sends the same message as a phone alert that opens the speaking agent's thread. This fallback requires an Openbase Cloud login and a phone registered for notifications; if either delivery path fails, the command exits with an error instead of claiming success.

For local audio cues:

```bash
openbase-coder user play success
openbase-coder user play /path/to/sound.wav
```

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

On GPT-Live, starting a call on an agent or transferring to it selects that agent's mapped voice. Voice changes require a new GPT-Live session and may introduce a brief gap; the LiveKit room, active thread, and bounded recent conversation history continue. Returning to Dispatcher restores its configured voice. Catalog collisions can give different agents the same GPT-Live voice. The classic `pipeline` voice model remains selectable for subsequent calls.

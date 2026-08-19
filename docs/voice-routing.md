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
voice room is active, the command sends the same message as an iPhone alert
that opens the speaking agent's thread. This fallback requires an Openbase
Cloud login and an iPhone registered for notifications; if either delivery
path fails, the command exits with an error instead of claiming success.

For local audio cues:

```bash
openbase-coder user play success
openbase-coder user play /path/to/sound.wav
```

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
  the active room, or send a thread-linked iPhone notification when no room is
  active.

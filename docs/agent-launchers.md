# Codex and Claude Code from Your Terminal

`openbase codex` and `openbase claude` start Codex or Claude Code the way
Openbase starts them, so a session you begin at the keyboard shows up in the
Openbase app and on your phone, and the dispatcher can see and steer it.

```bash
openbase codex                 # new Codex session in this folder
openbase codex "fix the build" # with a first prompt
openbase codex resume --last   # any Codex arguments work
openbase claude                # new Claude Code session
openbase claude -c             # continue the last conversation
```

`openbase-coder codex` and `openbase-coder claude` are the same commands;
`openbase` forwards to them. Everything after the command name goes to the
agent unchanged, except the two Openbase flags below, which must come first.

Plain `codex` and `claude` are never changed. Openbase does not alias, wrap or
reconfigure them; use the `openbase` commands when you want the session to be
part of Openbase, and the plain ones when you don't.

## What the launcher adds

**Codex** runs with Openbase's Codex profile (`-p openbase`, or
`-p openbase-cloud` when an Openbase Cloud backend is selected — the files
are `~/.codex/openbase.config.toml` and `openbase-cloud.config.toml`) and
attaches to this computer's Openbase-managed Codex app-server
(`--remote unix://`), the same one the dispatcher and Super Agents use. The
folder you are in is passed explicitly (`-C`); an attached session otherwise
starts in the app-server's own folder.

- Over the shared app-server Codex applies the profile's model, reasoning
  effort and permissions. MCP servers and hooks come from the app-server's
  own configuration (your `~/.codex/config.toml`), where
  `openbase-coder profiles install` registers Openbase's Super Agents server
  by default.
- If the app-server is not running, the launcher says so in one line and
  starts a standalone session instead; that session is not visible to the
  phone.
- With an Openbase Cloud backend, sessions currently run standalone: the
  Cloud model provider is defined only in Openbase's profile, which the
  shared app-server cannot load for a terminal session.
- Codex subcommands that don't open a session (`exec`, `mcp`, `login`, …)
  run here with the profile and nothing else.

**Claude Code** runs with Openbase's settings and MCP layer
(`--settings ~/.openbase/profiles/claude/settings.json --mcp-config
~/.openbase/profiles/claude/mcp.json`) and Openbase's instructions appended
to the system prompt. The settings layer registers the session with Openbase
when it starts, which is what lets Openbase steer a terminal session. With an
Openbase Cloud backend, the session uses Openbase Cloud's models. Claude Code
subcommands that manage the installation (`mcp`, `doctor`, `update`, …) run
plain. `openbase claude status` and `openbase claude login` stay Openbase's
own [login helpers](commands/claude.md).

If a profile is missing, the launcher stops and tells you to run
`openbase-coder profiles install`.

## Send a message to a running session

From any other terminal, `openbase-coder threads list` shows the sessions you have open and `openbase-coder threads send` sends one a message: a new turn when it is idle, a correction to the current turn when it is busy.

```bash
openbase-coder threads list
openbase-coder threads send "fix the build" "use pnpm instead of npm"
openbase-coder threads send build "run the tests" --wait   # part of a name works; prints the reply
```

See [`threads`](commands/threads.md) for names, stdin, and what a Claude Code session does with a message from outside.

## Laptop and hub

If you use [Openbase Sync](code-sync.md) with an always-on hub, running
`openbase codex` or `openbase claude` on the laptop (the *edge*) starts the
session **on the hub** and attaches your terminal to it, when:

- this computer is a paired edge,
- the folder you are in is synced (and not excluded), and
- the hub's Openbase is reachable over your Openbase network.

Synced folders have the same path on both computers, so the session opens in
the same folder there. It keeps running on the hub if the laptop sleeps or
the connection drops (for up to 8 hours with no terminal attached); the
terminal reconnects to it automatically, and the conversation stays
available in the Openbase app. Keys, including Ctrl-C, go to
the agent; resizing the window resizes the session.

When one of those conditions is not met, the session runs on the laptop and
the launcher prints one line saying why (for example
`~/Scratch is not in a synced folder; running locally.`). On a computer that
is not paired, and on the hub itself, sessions always run locally and
nothing extra is printed.

| Flag | Effect |
|---|---|
| `--local` | Run on this computer even on a paired edge. |
| `--remote` | Run on the hub; fail with the reason instead of running locally. |

No SSH is involved: the terminal talks to the hub's Openbase over the same
authenticated connection the Openbase app uses, signed in as you.

## Troubleshooting

- **`The hub could not open a remote session (HTTP 500); its Openbase may
  need an update.`** The hub runs an older Openbase. Update it, or use
  `--local`.
- **A Codex session doesn't appear on the phone.** Check whether the launcher
  printed that it started a standalone session; start Openbase's services
  (`openbase-coder services start`) and launch again.
- **Codex asks whether to trust the folder.** That is Codex's own first-run
  prompt for a new folder; it appears once per folder.

# threads

See the Codex and Claude Code sessions open in terminals on this computer, send one a message, and maintain threads.

## Usage

```bash
openbase-coder threads list [--all] [--json]
openbase-coder threads send SESSION [MESSAGE] [--wait [--timeout SECONDS]] [--json]
openbase-coder threads archive-stale [--days N] [--dry-run]
openbase-coder threads push THREAD_ID [--to DEVICE] [-m MESSAGE]
```

The same subcommands are available through the `openbase` launcher when that alias is installed.

## threads list

Lists the sessions running in terminals on this computer, started with [`openbase codex`](codex.md) or [`openbase claude`](claude.md) (or plain `codex` when it attached to Openbase's Codex app-server).

```text
NAME             BACKEND      STATE  STEERABLE  FOLDER                HOW
Reply with pong  codex        idle   yes        ~/Scratch/demo        shared Codex app-server
tui-send-probe   claude_code  busy   yes        ~/Scratch/demo        Claude Code inbox socket
```

| Column | Meaning |
|---|---|
| `NAME` | The session's name: the thread name Openbase shows, or the name given with `claude --name` or `/rename`. |
| `STATE` | `busy` while a turn runs, `idle` at the prompt, `unknown` before the first message. |
| `STEERABLE` | Whether `threads send` can reach it. |
| `HOW` | The delivery path, or why the session can't be reached yet. |

A session is listed only while its process is alive. A Codex session is listed after its first message, because that is when Openbase's thread list picks it up; before then it shows as `codex (no messages yet)` and is not steerable.

| Flag | Effect |
|---|---|
| `--all` | Also list every other thread Openbase knows, such as dispatched Super Agents. |
| `--json` | Print the sessions (and, with `--all`, the other threads) as JSON. |

## threads send

```bash
openbase-coder threads send "Reply with pong" "use pnpm instead of npm"
echo "summarize what you changed" | openbase-coder threads send tui-send-probe
openbase-coder threads send "Reply with pong" "run the tests" --wait
```

`SESSION` is a name from `threads list` (a unique part of it is enough), a thread id, or a Claude Code session id or its first eight characters. Sessions open in a terminal win over other threads with the same name. `MESSAGE` comes from the argument or, when it is omitted or `-`, from stdin.

For managed sessions, an idle follow-up starts a new turn and busy steering submits input to the active turn. Foreign Claude Code inbox submissions have a different receipt: they do not confirm that an idle session resumed or that the model consumed the message.

- **Codex** sessions get the message through Openbase's Codex app-server, the same path the phone and the dispatcher use.
- **Claude Code terminal** sessions receive a best-effort inbox submission. The command reports delivery as unconfirmed; it does not start a managed turn. A session that runs with permissions bypassed (the Openbase profile's default) holds a message from outside until you approve it in that terminal, unless its settings set `crossSessionInbound` to `accept`; the command prints a one-line reminder.
- Any other thread Openbase lists (`threads list --all`) gets the message through Openbase.

| Flag | Effect |
|---|---|
| `--wait` | Wait for a confirmed managed turn to finish and print its reply. Unconfirmed inbox receipts cannot establish which turn answers the message. |
| `--timeout SECONDS` | With `--wait`, give up after this long (default 600). |
| `--json` | Print the delivery result (and, with `--wait`, the finished turn) as JSON. |

`--wait` needs a session Openbase lists and a confirmed managed receipt. It refuses unconfirmed inbox submissions so an unrelated prior completion cannot be presented as the reply. For an inbox submission, inspect the terminal or thread state before considering a retry; an ambiguous write may still be consumed. `--json` exposes the receipt, including `confirmed`, `written`, `mayHaveBeenWritten`, and the frame's `messageId`; that message ID is not a turn ID or delivery acknowledgment.

### Errors

- **`cannot be messaged yet`**: the Codex session has had no message yet; type one in its terminal first.
- **`its inbox socket no longer accepts connections`**: the Claude Code session has ended.
- **`delivery is unconfirmed`**: the inbox submission may still be consumed. Inspect the existing thread before retrying; do not automatically send it again or queue a duplicate.
- **`Cannot wait for confirmed work`**: the receipt does not establish a managed turn to wait for. No second submission is attempted.
- **`matches more than one session`**: use more of the name, or the thread id from `threads list --json`.

## threads archive-stale

Archives interactive Codex threads not updated within `--days` (default 10), so `codex resume <name>` against the shared app-server keeps working. Archived threads stay resumable by id. `--dry-run` reports without archiving.

## threads push

Moves a thread to a durable machine and continues it there. See [Push a Thread to Your Durable Machine](../push-to-durable.md).

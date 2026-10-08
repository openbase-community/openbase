# Push a Thread to Your Durable Machine

A conversation you started on your laptop stops when the laptop sleeps. **Push to durable machine** moves it to the computer that stays on (your Mac mini, desktop or Cloud DevSpace) and continues it there: the same conversation, the same files, still visible from every Openbase app.

Your **durable machine** is the always-on computer your laptop syncs with: today, the [Openbase Sync](code-sync.md) hub this computer is paired with.

## Before you push

- This computer and the durable machine are paired with Openbase Sync, and **Thread sync** is enabled (the `~/.openbase/thread-sync` folder is synced; pairing from the Sync page sets this up).
- The thread's working folder is inside a synced folder, such as `~/Projects`. The durable machine uses the same home-relative path: `~/Projects/app` here is `~/Projects/app` there.
- The durable machine is on, connected to Openbase VPN, signed in to the same Openbase account, and runs the same backend as the thread (Codex or Claude Code).
- The thread is idle: no turn running, no queued prompts, no approval waiting, and it is not the voice dispatcher or the thread on a live call. Openbase never interrupts a running turn to push it; wait for the turn to finish, or stop it yourself first.

## Push from the app

Open the thread in the desktop app or the web console, open the thread's **⋯** menu, and choose **Push to *mini*** (your durable machine's name). If the thread cannot move right now, the menu item says why. You can add a message for the agent to continue with; it is sent on the durable machine as the first turn there.

When the push finishes, the app follows the thread to the durable machine: new turns, the terminal tab and approvals all run there.

## Push from the terminal

```bash
openbase-coder threads push <thread-id>                 # to your durable machine
openbase-coder threads push <thread-id> -m "keep going" # and continue there
openbase-coder threads push <thread-id> --wait          # wait for a running turn
openbase-coder threads push <thread-id> --list          # where it can go, and why not
openbase-coder threads push <thread-id> --to mini       # pick a durable machine
```

The thread id is shown in the thread's **⋯** menu (**Copy thread ID**).

## What happens

1. The thread pauses here: new turns are refused while the push runs.
2. Openbase writes the thread's transcript into the thread-sync folder and waits until Openbase Sync confirms the durable machine has both the transcript and the latest files of the thread's folder.
3. The durable machine imports the transcript and the thread continues there under the **same thread id**. With a message, its first turn starts.
4. The copy on this computer becomes **read-only** and shows "Moved to *mini*". It still mirrors the conversation through thread sync, so you can read it here, but new turns go to the durable machine.

In the thread list, the thread now appears as living on the durable machine.

## When something goes wrong

Every push is safe to retry: a retry never creates a second thread or sends your message twice.

| Message | What to do |
|---|---|
| A turn is running | Wait for it to finish (or use `--wait`), then push. |
| Not in a synced folder | Add the folder (or a parent) under **Settings → Sync**. |
| Not reachable over Openbase VPN | Wake the durable machine and check its VPN, then retry. |
| Files are still syncing | Retry in a moment; a large change takes time to arrive. |
| Codex / Claude Code is not set up on *mini* | Set up that backend on the durable machine. |
| The push did not finish | The thread stays paused until the push is confirmed: retry it, or run `openbase-coder threads push <id> --cancel` to ask the durable machine and make the thread usable here again if it never arrived. |
| Both computers changed this thread | Resolve it under **Threads → Sync conflicts** on the durable machine, then push again. |

## Bringing a thread back

A moved thread's copy here stays read-only. To use it on this computer again, run `openbase-coder threads push <id> --release`. Only do this if the thread is no longer running on the durable machine, or the conversation will continue in two places.

## Limits

- Only threads that run through Openbase are paused. A session you resumed directly with plain `codex` or `claude` in a terminal is not stopped by the push; close it first (Openbase refuses to export a Codex thread that a terminal still has open).
- Pushes go to the durable machine you sync with. Pushing a thread from the durable machine back to a laptop is not supported.

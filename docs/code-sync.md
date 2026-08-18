# Sync Between Your Computers

Code sync keeps the same working directories on two or more of your machines
(for example a MacBook and a Mac mini, or a laptop and a Cloud DevSpace) in
near-realtime sync, so your secondary machine is always ready to take a voice
call. Files move on save — no commits, no pushes, no manual copying.

Under the hood, Openbase Coder runs a fully managed
[Syncthing](https://syncthing.net) instance as the `code-sync` service. You
never configure Syncthing yourself: device pairing comes from your Openbase
Cloud device registry, transport is pinned to your private Tailscale network,
and global discovery, relays, and NAT traversal are all disabled. Nothing
leaves your tailnet.

## What syncs

- Every directory you add with `openbase-coder sync add` (or from the console
  Sync settings). Folders are identified by their **home-relative path** —
  `~/Projects/myapp` on one machine maps to `~/Projects/myapp` on the other,
  even when the home directories differ.
- **Secrets sync deliberately.** `.env` files, keys, and other
  gitignored-but-needed files travel with the code. This is a core feature:
  git transports alone can never move them, and a second machine without its
  secrets cannot actually run your project. Only machines you own (they are
  all inside your tailnet) ever receive them.

## What never syncs

- **`.git` and all other VCS metadata (`.jj`, `.hg`) — categorically.** A git
  directory is a multi-file database mutated non-atomically; syncing it
  transfers refs from one moment and the index from another, which silently
  corrupts checkouts (this failure mode is why code sync exists in its
  current form). Each machine keeps its own private `.git`; branch pointers
  are reconciled through git's own transport instead (below).
- Dependency and build noise: `node_modules`, virtualenvs, `dist`/`build`
  outputs, `__pycache__`, `DerivedData`, caches, `*.sqlite3`.
- Machine-local state under `~/.openbase` (device identity, databases, logs).

Each synced folder gets a generated `.stignore` owned by openbase-coder; add
per-folder patterns via `extra_ignores` in the sync settings rather than
editing it.

## How git stays correct on both machines

Commits made on either machine propagate through a small reconciler that runs
every minute:

- Every ordinary repository publishes a small `.openbase-repo.json` manifest
  containing its active branch and commit. The file stays out of local Git
  status but travels through code sync. It makes the checked-out
  branch authoritative across machines: when one machine checks out another
  branch, peers attach `HEAD` to that branch without rewriting the working
  files that Syncthing already delivered.
- If a repository's files arrive on a machine that has never cloned it, the
  manifest bootstraps a machine-local `.git`, fetches the branch history from
  the peer, and restores a safe credential-free `origin` URL when one was
  available. No manual first clone is required.
- When the peer committed and Syncthing has already delivered the resulting
  files, your local branch pointer is **fast-forwarded** to the same commit —
  status goes clean, nothing moves twice. This only happens when it is
  provably safe: no merge/rebase in progress, your head is an ancestor of the
  peer's, and your working tree already matches the peer's commit exactly.
- When branch histories diverge, the synced manifest deterministically brings
  both active branch pointers back to one history. A commit displaced by that
  move is retained under `refs/openbase-code-sync/backups/` for recovery; it
  is never discarded. A repo sync conflict remains visible only while safe
  convergence is blocked (for example, by staged changes or an in-progress
  rebase), and clears after the branch heads agree.
- Uncommitted work needs no reconciliation at all — it syncs as files and
  simply shows as a dirty tree on both sides.

The staging area, stashes, reflog, and in-progress Git operations remain
machine-local. The reconciler pauses instead of changing a checkout with
staged changes or an active merge/rebase/cherry-pick.

Git **worktrees** under synced folders are first-class: the worktree's
files sync like any files, and each machine attaches its own local git
identity automatically (a small synced manifest tells the other machine
which repository and branch to attach). Run git commands in a worktree on
either computer; commits reconcile back through the same branch
fast-forward machinery as any repository.

Coding threads (Codex and Claude Code) also travel between your machines
over the same channel: each device exports snapshots of recent threads and
imports the other's automatically. Only threads active in the **last 15
days** are exchanged — after a long gap between machines, older threads
stay where they were created (they are never deleted, just not carried
across). Working directories beneath the source device's home are translated
to the same home-relative location on the receiving device (for example, a
Mac home path becomes the analogous path under a Linux DevSpace home); paths
outside a user home are preserved as recorded. A thread sync conflict is only
raised when the two machines hold
genuinely divergent transcripts; identical or append-only-extended copies
sync silently, and a standing conflict clears itself once the two sides
converge again.

Machines fetch from each other directly over Tailscale (read-only git smart
HTTP served by the local API with your own credentials); no GitHub round-trip
is involved.

## The write lease

To make stale-machine echoes structurally impossible, code sync holds an
advisory write lease: the machine with recent voice/agent activity (last 15
minutes) keeps its folders send-receive, while an idle machine that can see
an active peer flips its own folders receive-only. When nobody is provably
active the lease is sticky with its last holder, so plain manual edits always
still propagate. Set `lease_mode` to `manual` in sync settings to disable
automatic flipping when you intentionally work on both machines at once.

## Versioning: the undo net

Synced work is often uncommitted, so it has no reflog. Every managed folder
has staggered file versioning enabled: whenever an incoming sync replaces or
deletes a file, the previous copy is kept under
`~/.openbase/sync-versions/<folder-id>/` for 30 days. A bad deletion that
propagates through sync is an undo, not data loss. Local edits never create
versions, and the storage-heavy patterns are excluded from sync entirely;
`openbase-coder doctor` warns when version history grows past 2 GiB, and the
console offers a purge control (`POST /api/sync/versions/purge/`).

## Eligibility

Code sync arms only when your Openbase Cloud device registry shows **two or
more non-phone devices with Tailscale identities**. Phones never participate
as sync peers; they only view sync state and conflicts. With a single
machine, the console shows an "add a second machine" nudge and
`openbase-coder sync enable` explains what is missing.

## Conflicts

Two kinds of conflicts can be surfaced:

- **Repo conflicts** — a divergent branch could not yet follow its repository
  manifest safely. These self-clear after convergence; manual *Keep Local* /
  *Use Remote* controls remain available when intervention is needed.
- **File conflicts** — Syncthing's last-resort `*.sync-conflict-*` copies
  from truly simultaneous edits of one file. The reconciler finds and lists
  them so they are cleaned up deliberately instead of discovered by grep.

List and resolve them with `openbase-coder sync conflicts` and
`openbase-coder sync resolve`, or from the console/iOS conflict pages.

See the [`sync` command reference](commands/sync.md) for the full CLI.

## Troubleshooting

### File sync stalled (folder in error state)

When free disk drops below the engine's floor, Syncthing keeps running but
its folders enter an error state (typically `insufficient space on disk for
database`) and **nothing syncs** — the peer's working trees go stale, deleted
files can echo back from the peer as untracked copies, and branch switches
stop propagating (repository manifests ride the same folder).

Where the stall surfaces:

- a red banner on the dashboard (health warnings), plus a low-disk warning
  before the hard floor is hit,
- the Sync page: a "File sync is stalled" callout and a red state badge with
  the error on each affected folder row,
- `openbase-coder sync status`: an `ERROR:` line under the affected folder,
- `/api/sync/status/`: `folders[].error` and `syncthing_errors`.

The managed config pins an absolute floor of 2 GiB (`minDiskFree` per folder
and `minHomeDiskFree` for the engine's database) instead of Syncthing's 1%
default, which on large disks pauses sync with tens of GiB still free. Fix:
free disk space, then restart Openbase services (`openbase-coder services
stop code-sync && openbase-coder services start code-sync`); the folders
recover on the next scan.

### Reading the reconcile heartbeat

Every reconcile tick logs one summary line to the sync-workers log:

```
code_sync tick_complete repos=41 up_to_date=39 fast_forwarded=1
awaiting_files=1 remote_behind=0 diverged=0 skipped=0 fetch_failed=0
converged=0 published=1 conflicts=0 errors=0 lease=noop
```

`awaiting_files` climbing without draining means git state is ready but file
delivery is behind — check for a Syncthing stall. Per-repo failures are
isolated (one broken repo cannot abort the tick) and named in a
`code_sync tick_errors` warning line. The same counts appear in
`openbase-coder sync status` (`Reconcile:` line) and in
`/api/sync/status/` under `last_reconcile`.

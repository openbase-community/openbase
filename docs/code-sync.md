# Sync Between Your Computers

**Openbase Sync** mirrors the folders you choose between two of your
computers (for example a MacBook and a Mac mini, or a laptop and a Cloud
DevSpace) in near-realtime, so your other machine is always ready to take a
voice call or run an agent. Files move on save — no commits, no pushes, no
manual copying — and git history moves as git.

Openbase Sync runs as the `sync-daemon` service on each computer. The
service runs Openbase's sync daemon (`openbase-syncd`), a binary provided by
Openbase; the CLI, console and phone apps talk to it locally and never
implement sync themselves.

## Hub and edge

Openbase Sync pairs exactly two computers:

- The **hub** is the machine that is always on (typically a Mac mini or a
  Cloud DevSpace). It listens on its Openbase VPN address.
- The **edge** is the machine you carry (typically a laptop). It connects to
  the hub when it can and catches up after sleep or travel.

Both sides share a pair secret generated when the hub is configured. Traffic
only flows over your private Openbase VPN; nothing is relayed through a
third party.

## Roots: what syncs

Each synced directory is a **root**. A root is mirrored as a whole,
including:

- uncommitted changes and new files,
- **secrets**: `.env` files, keys and other gitignored-but-needed files
  travel with the code on purpose — git alone never moves them, and a second
  machine without its secrets cannot run your project. Only your two paired
  machines ever receive them,
- lockfiles and local databases.

Dependency directories (such as `node_modules` and virtualenvs) are not
transferred; each machine installs its own.

Roots are matched by their **home-relative path**: `~/Projects` on one
machine pairs with `~/Projects` on the other, even when the home directories
differ (a Mac and a Linux DevSpace). Keep the same layout on both machines.

### Product folders

Two Openbase features ride Openbase Sync through **product-folder roots**:

- **Thread sync**: `~/.openbase/thread-sync` carries Codex and Claude Code
  thread snapshots between your machines. The thread device-sync jobs run
  only when this folder is inside a configured root.
- **Skills sync**: `~/.agents/skills` plus any linked skill-source folders
  inside your home folder carry your personal skills (see below).

Pairing from the Sync page or with `sync-daemon pair hub` includes them;
with `configure`, add `--with-product-folders`.

## Git: history travels as git

`.git` directories are **never file-synced**. A git directory is a
multi-file database that is changed non-atomically; copying its files
transfers refs from one moment and the index from another, which silently
corrupts checkouts. Instead, Openbase Sync replicates git state as git:
commits, branches, tags and worktrees made on one machine appear on the
other through git's own object transfer, and the checked-out branch follows.

- A branch or commit can appear on this machine because the other one made
  it.
- A branch is never silently rewound. If both machines committed different
  history to the same branch, sync records a **branch conflict** instead of
  picking a winner.
- Deleting a worktree on one machine propagates to the other.

## Placement: large files and disk space

One side is the **anchor** and holds every file in full (the hub by
default). The other side keeps large files as placeholders until they are
used, and frees space when the disk runs low. Choose `--anchor edge` when the
hub has less disk than the laptop.

Advanced: a root in `~/.openbase/sync/config.toml` can list **pins** —
root-relative paths that are held in full only on the anchor side (`"."` pins
the whole root):

```toml
[[roots]]
id = "projects-media"
path = "~/Projects/media"
pins = ["raw-footage"]
```

## Conflicts

Openbase Sync merges what it can. When both machines changed the same thing
in incompatible ways, it keeps both versions and records a **conflict**
instead of overwriting either side. Kinds include both sides edited a file,
deleted on one side and edited on the other, a file versus a directory, a
database written on both sides, and a diverged branch.

Resolve each conflict by keeping this computer's version (**keep mine**) or
taking the other computer's (**take theirs**):

- the console **Sync** page,
- the iOS and Android **Computer Sync** screens,
- the CLI: `openbase-coder sync conflicts` and
  `openbase-coder sync resolve <id> --keep-local|--use-remote`.

Open conflicts also show as a dashboard warning.

### AI conflict labels (opt-in)

Openbase Sync can ask Openbase Cloud to label conflicts in text files, to help you decide which version to keep. It is off by default and set per computer:

```bash
openbase-coder sync-daemon judgment enable    # turn on for this computer
openbase-coder sync-daemon judgment status    # show whether it is on
openbase-coder sync-daemon judgment disable   # turn off again
```

What it does and does not do:

- **Labels only.** A label is a hint attached to the conflict. Openbase Sync never resolves a conflict on its own; you still choose **keep mine** or **take theirs**.
- **Your file contents leave this computer.** When a text file conflicts, the contents of both versions are sent to Openbase Cloud to be classified. Leave the feature off for folders whose contents must not leave your computers.
- **Billing.** Classification is billed to your Openbase Cloud account against a small free monthly allowance.

`enable` and `disable` update the `[judgment]` table of `~/.openbase/sync/config.toml`, tell Openbase Cloud about the choice for this computer (you must be signed in; if the computer is offline, it is retried at its next periodic check-in), and restart the `sync-daemon` service. Turn it on separately on each computer that should use it.

## Personal skills

Use **Settings → Agents → Skills → Sync my skills across devices** to share
your personal skills. Sharing uses the `~/.agents/skills` product-folder
root and linked skill-source directories inside your home folder. It never
syncs entire backend homes, credentials or plugin caches; linked sources
outside your home folder or in machine-local Openbase state are reported as
unavailable for sharing.

## Coding threads

Codex and Claude Code threads travel between your machines through the
thread-sync product folder: each device exports snapshots of recent threads
and imports the other's automatically. Only threads active in the **last 15
days** are exchanged. Working directories beneath the source device's home
are translated to the same home-relative location on the receiving device. A
thread sync conflict is raised only when the two machines hold genuinely
divergent transcripts.

## Display-bound commands

Openbase also provides a small companion `edge` command for work that must
happen on the laptop while agents run on the hub: `edge run` runs a
display-bound command (for example opening a browser) on the laptop, and
`edge forward` forwards a port from the laptop (for example Chrome DevTools
on 9222) to the hub.

## Set up

Both computers must be signed in to the same Openbase account and
connected to Openbase VPN.

1. On the always-on computer (for example a Mac mini), open the console
   **Sync** page and choose **Make this my always-on computer**. It becomes
   the hub and starts syncing `~/Projects` plus the product folders.
2. On each other computer, open **Sync**. Under **Sync with…**, your other
   computers are listed with their role; choose **Sync with this** next to
   the always-on computer. This computer becomes an edge with the hub's
   folders: the hub hands over its pair secret and folder list over Openbase
   VPN, so there is nothing to copy by hand.
3. Check it on the Sync page (the peer shows as connected), or with
   `openbase-coder sync status`.

Once syncing, the Sync page shows which computer is the hub, lets you add
or remove a folder (the change is made on both computers), and has **Stop
syncing on this computer**. Stopping removes the `sync-daemon` service and
moves `~/.openbase/sync/config.toml` to `~/.openbase/trash/`; your files
stay where they are. A computer that is not paired runs no sync service.

The sync daemon is provided with Openbase. If pairing reports that it is
missing, update Openbase.

### From the command line

The same steps without the console:

```bash
# On the always-on computer
openbase-coder sync-daemon pair hub

# On each other computer
openbase-coder sync-daemon pair candidates      # your computers and their role
openbase-coder sync-daemon pair join <hub-name>

# Stop syncing on a computer
openbase-coder sync-daemon pair leave
```

`openbase-coder sync-daemon configure` remains for manual setups (custom
addresses, a pair secret you manage yourself, or `--anchor hub`). Configure
the hub with its Openbase VPN address and roots, then each edge with the
hub's address and the pair secret the hub printed, using the same roots:

```bash
openbase-coder sync-daemon configure --role hub --listen <hub-vpn-ip> \
  --root ~/Projects --with-product-folders
openbase-coder sync-daemon configure --role edge --peer <hub-vpn-ip> \
  --pair-secret <secret> --root ~/Projects --with-product-folders
```

`configure` installs and starts the `sync-daemon` service unless you pass
`--no-start`. Stop it with `openbase-coder sync-daemon disable` (your
configuration and files are kept).

## Migrating from the previous sync

Earlier releases synced folders with a Syncthing-based `code-sync` service.
That service is no longer installed; setup and self-update remove a leftover
`code-sync` service. To move an existing machine over:

```bash
# 1. On each computer: preview, then migrate
openbase-coder sync migrate-from-syncthing
openbase-coder sync migrate-from-syncthing --apply

# 2. Only after step 1 has been done on EVERY computer: remove the old markers
openbase-coder sync migrate-from-syncthing --apply --remove-markers
```

With `--apply`, the migration stops and removes the old service, moves its
state (`~/.openbase/code-sync`, `~/.openbase/sync-versions`,
`~/.openbase/sync-config.json`) into
`~/.openbase/trash/syncthing-migration-<timestamp>/` — nothing is deleted —
and turns your previously synced folders plus the product folders into
Openbase Sync roots. If Openbase Sync is already configured, the missing
roots are added and the service is restarted; otherwise the migration prints
the `sync-daemon configure` command to run.

The old sync's marker and ignore files in your synced folders (`.stfolder`,
`.stignore`, `.stglobalignore`) stay in place until you pass
`--remove-markers`. Remove them only once the old sync is stopped on every
computer: if one computer still ran it, Openbase Sync would carry the
deletion of its ignore file over, and the old sync there would start copying
`.git` directories. The markers are moved to the same trash folder.

Custom ignore rules from the previous sync are not carried over — Openbase
Sync recognizes dependency and build folders itself. They stay in the
trashed `sync-config.json`, and the migration prints how many were left
behind.

The migration is safe to run again (a finished machine has nothing left to
do), and safe on machines that never used the previous sync. See
[`sync migrate-from-syncthing`](commands/sync.md#migrate-from-syncthing).

## Troubleshooting

- **"Openbase Sync is configured but its daemon is not answering"** — start
  the service: `openbase-coder services start sync-daemon`, then check
  `openbase-coder services logs sync-daemon`.
- **"Not connected to the other computer"** — make sure the hub is on and
  both machines are connected to Openbase VPN. The edge reconnects
  automatically.
- **Unresolved conflicts** — resolve them on the Sync page or with
  `openbase-coder sync resolve`.
- **A commit or build fails mysteriously** — check
  `openbase-coder sync conflicts`, and compare `HEAD` with
  `origin/<branch>` before committing.

See the [`sync`](commands/sync.md) and [`sync-daemon`](commands/sync-daemon.md)
command references for the full CLI.

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

Openbase Sync connects your computers to one always-on computer:

- The **hub** is the machine that is always on (typically a Mac mini or a
  Cloud DevSpace). It listens on its Openbase VPN address and holds every
  synced folder.
- An **edge** is any other computer (typically a laptop, or a cloud
  workspace that works on a project or two). It connects to the hub when it
  can and catches up after sleep or travel. Changes from one edge reach the
  others through the hub. An edge can sync all of the hub's folders or only
  some of them (see [A project-only cloud workspace](#a-project-only-cloud-workspace)).

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

One side is the **anchor** and holds every file in full. The other side
keeps large files as placeholders until they are used, and frees space when
the disk runs low. Pairing from the Sync page (or `sync-daemon pair`) makes
the edge the anchor: your laptop keeps everything, and the always-on hub
fetches a large file when it is used. With `sync-daemon configure` the hub is
the anchor unless you pass `--anchor edge`.

Sync never fills a disk. It keeps a minimum of free space on each disk it
writes to, and refuses to write below it: the file stays on the other
computer and arrives by itself once space is freed. The Sync page and
`openbase-coder sync status` show each folder's free space, the minimum sync
keeps, and how many files are waiting for space. If a disk fills completely
(something else used the space), sync stops accepting changes on that
computer without losing any; they arrive when space returns.

The limits scale with the size of the disk unless you set them in the
`[placement]` table of `~/.openbase/sync/config.toml`:

| Setting | Default | On a 5 GB disk |
|---|---|---|
| `low_water_mb`: free space sync never writes below | 10% of the disk, at most 10 GB | about 512 MB |
| `version_quota_mb`: room for previous versions of files (conflict and undo copies) | 15% of the disk, at most 10 GB | about 768 MB |
| `version_retention_days`: how long previous versions are kept | 30 | 30 |
| `lazy_mb`: files above this stay placeholders on the non-anchor side until used | 1% of the disk, at most 100 MB | about 51 MB |
| `pinned_mb`: files above this stay on the anchor until explicitly fetched | 5% of the disk, at most 1 GB | about 256 MB |

On a disk of 100 GB or more every default is its maximum. `thin = true`
makes a computer keep large files as placeholders whatever the anchor (set
by project-only pairing).

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

On the Sync page, conflicts are grouped by repository or folder, with search,
a filter by kind, and paging for long lists. Opening a conflict shows both
versions and, for text files, the differences between them (when this
computer holds both versions). Select a group, or everything a filter shows,
to resolve many conflicts the same way; the page asks you to confirm the
count first.

A **branch conflict** is different: neither computer's branch was moved, so
there is no version to pick. Merge or rebase in git on either computer; the
conflict closes by itself once both computers point at the same commit. The
Sync page shows the commits only one side has and the commands to reconcile,
and the CLI and API refuse **keep mine** or **take theirs** for it.

## Is sync healthy?

The top of the Sync page answers it: up to date, syncing (with the number of
changes waiting to be sent or confirmed, the rate and a rough time left),
checking files, the other computer not reachable (with when it was last
connected), or the sync service not running. Each synced folder lists, per
paired computer, how many changes are still to send and how far this
computer has received the other's changes. On the always-on computer, the page
lists every computer syncing with it.

The page also lists **stale git locks**: lock files (such as
`.git/index.lock`) left behind by a git process that died. Git commands in that
repository fail, and Openbase Sync cannot replicate its commits, until the
lock is removed. **Move lock to Openbase trash** moves one into
`~/.openbase/trash/git-locks/` (nothing is deleted), and only when the lock is
more than 10 minutes old and no running process holds it open.

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

To move a conversation that is already running on your laptop over to the hub, so it keeps going while the laptop sleeps, push it: see [Push a Thread to Your Durable Machine](push-to-durable.md).

## Starting agents on the hub

On a paired edge, `openbase codex` and `openbase claude` started in a synced
folder run the session on the hub and attach your terminal to it, so the
work keeps going while the laptop sleeps. See
[Codex and Claude Code from Your Terminal](agent-launchers.md).

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
   the always-on computer, then choose which of its folders this computer
   syncs. Each folder shows its number of files and size, next to this
   computer's free space. On a laptop every folder is preselected; on a
   cloud workspace none is (see below). This computer becomes an edge with
   the chosen folders: the hub hands over its pair secret and folder list
   over Openbase VPN, so there is nothing to copy by hand.
3. Check it on the Sync page (the peer shows as connected), or with
   `openbase-coder sync status`.

Once syncing, the Sync page shows which computer is the hub and lets you
add or remove a folder. On the hub, a change is made on every computer. On
an edge, the hub's folders that this computer does not sync are listed with
**Sync here**, and removing a folder asks whether to stop syncing it **on
this computer** only or **everywhere**. The page also has **Stop
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
openbase-coder sync-daemon pair folders <hub-name>   # its folders, their size, your free disk
openbase-coder sync-daemon pair join <hub-name>                     # all of them
openbase-coder sync-daemon pair join <hub-name> --root ~/Projects/app   # only some

# Later, on an edge
openbase-coder sync-daemon pair add-folder ~/Projects/other
openbase-coder sync-daemon pair remove-folder ~/Projects/app --this-computer

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

## A project-only cloud workspace

A cloud workspace (an Openbase Cloud DevSpace) usually has a small disk,
about 5 GB, and works on one or two projects. It can sync just those
projects with your always-on computer, while the always-on computer keeps
holding everything and your laptop keeps syncing all of it.

Set it up on the cloud workspace:

1. Make sure the always-on computer syncs the project as its own folder.
   Folders are synced whole, so for one project out of `~/Projects`, the hub
   needs `~/Projects/<project>` as a folder of its own rather than only
   `~/Projects`. Folders cannot nest, so a hub that syncs `~/Projects` as a
   whole offers only that one folder; split it on the hub first (stop syncing
   `~/Projects` everywhere, then add each project folder you want to share).
2. On the cloud workspace, open **Sync**, choose **Sync with this** next to
   the always-on computer, tick the project folders, and confirm. Nothing is
   preselected on a cloud workspace, and the chooser warns when the chosen
   folders may not fit. From a terminal:

   ```bash
   openbase-coder sync-daemon pair folders <hub-name>
   openbase-coder sync-daemon pair join <hub-name> --root ~/Projects/<project>
   ```

A cloud workspace joins **project-only**: it syncs only the chosen folders,
and large files stay on the always-on computer as placeholders until
something on the cloud workspace uses them (or `openbase-sync fetch
<path>`). Its disk limits scale down with its disk (see
[Placement](#placement-large-files-and-disk-space)). Your laptop and the hub
are unchanged: the laptop's edits to the chosen projects reach the cloud
workspace through the hub, and the other folders never travel to it.

To work on another project later, use **Sync here** next to it on the
cloud workspace's Sync page (or `pair add-folder`). To drop one, remove it
**on this computer**: the hub and the laptop keep syncing it, and the files
on the cloud workspace stay until you delete them. If you delete them and
later sync the folder again, they come back from the hub; nothing is
deleted elsewhere.

`--project-only` makes any computer join this way, and `--full-copy` makes a
cloud workspace keep every file in full.

Large files that only your laptop holds in full (the laptop is the anchor
and the hub kept a placeholder) reach the cloud workspace only after the hub
has them; fetch them on the hub first if the cloud workspace needs them.

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

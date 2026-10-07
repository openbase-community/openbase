# sync

Inspect Openbase Sync, resolve its conflicts, and migrate from the previous
sync. See [Sync Between Your Computers](../code-sync.md) for how Openbase Sync
works, and [`sync-daemon`](sync-daemon.md) for configuring it.

## Usage

```bash
openbase-coder sync COMMAND [ARGS]
```

## Subcommands

| Subcommand | Description |
|---|---|
| `status [--json]` | Show the Openbase Sync role, roots, connected peer and open conflict count |
| `conflicts [--json]` | List open conflicts (id, root, path, kind) |
| `resolve ID --keep-local\|--use-remote` | Resolve one conflict by keeping this computer's version or taking the other computer's |
| `migrate-from-syncthing [--apply]` | Move this machine from the previous Syncthing-based sync to Openbase Sync |

`status`, `conflicts` and `resolve` talk to the local `sync-daemon` service
and fail with an explanation when Openbase Sync is not configured or not
running.

## migrate-from-syncthing

A dry run by default: it prints what it would do and changes nothing.

| Option | Description |
|---|---|
| `--apply` | Perform the migration |
| `--replace-nested` | When a migrated root contains roots that are already configured (for example `~/Projects` over `~/Projects/app`), drop the inner roots in favour of the outer one. Without it, overlapping roots are skipped and reported |
| `--no-restart` | Do not restart the `sync-daemon` service after adding roots |

With `--apply` it:

1. stops and uninstalls the old `code-sync` service if it is installed;
2. moves `~/.openbase/code-sync`, `~/.openbase/sync-versions`,
   `~/.openbase/sync-config.json` and the old `.stfolder`/`.stignore`
   markers in previously synced folders into
   `~/.openbase/trash/syncthing-migration-<timestamp>/` (nothing is deleted);
3. maps the previously synced folders (for example `~/Projects`) plus the
   product folders (`~/.openbase/thread-sync`, `~/.agents/skills` and
   linked skill-source folders) to Openbase Sync roots;
4. if Openbase Sync is configured, adds the missing roots to
   `~/.openbase/sync/config.toml` and restarts the `sync-daemon` service when
   it is installed; otherwise prints the `openbase-coder sync-daemon
   configure ...` command to run.

The command is idempotent and safe on a machine that never used the previous
sync.

## Examples

```bash
openbase-coder sync status
openbase-coder sync conflicts
openbase-coder sync resolve 42 --use-remote

# Preview, then perform, the migration from the previous sync
openbase-coder sync migrate-from-syncthing
openbase-coder sync migrate-from-syncthing --apply
```

## Notes

- Earlier `sync enable`, `disable`, `add`, `remove`, `ignores`,
  `heal-echoes`, `reconcile` and `install-engine` subcommands were removed
  with the previous sync. Choose what to sync with
  `openbase-coder sync-daemon configure --root ...`.
- The phone apps read the same state through `/api/sync/status/`,
  `/api/sync/conflicts/` and `/api/sync/conflicts/resolve/`; the console uses
  `/api/sync/daemon/...`.

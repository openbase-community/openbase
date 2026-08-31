# sync

Keep code in sync between your computers with a managed Syncthing instance.
See [Sync Between Your Computers](../code-sync.md) for what syncs, what never
syncs, and how git state is reconciled.

## Usage

```bash
openbase-coder sync COMMAND [ARGS]
```

## Subcommands

| Subcommand | Description |
|---|---|
| `install-engine` | Pre-fetch the pinned Syncthing engine without enabling sync |
| `enable` | Create the sync identity, render config/ignores, install and start the `code-sync` service, and advertise sync capabilities to Openbase Cloud |
| `disable` | Stop and remove the `code-sync` service (local data and version history are kept) |
| `status` | Show enablement, eligibility, folders, peers, and conflict counts |
| `add PATH` | Add a directory under `$HOME` to sync (stored as a home-relative path) |
| `remove PATH` | Stop syncing a directory (files stay on disk) |
| `ignores list [--folder RELPATH]` | Show custom Syncthing ignore rules for a synced folder |
| `ignores add PATTERN [--folder RELPATH]` | Add a custom Syncthing ignore pattern to a synced folder |
| `ignores remove PATTERN [--folder RELPATH]` | Remove a custom Syncthing ignore pattern from a synced folder |
| `conflicts` | List unresolved repo and file conflicts |
| `resolve ID --keep-local\|--use-remote` | Resolve one conflict (`--use-remote` safety-stashes the working tree first) |
| `reconcile [--loop]` | Run one git-state reconcile tick, or loop forever |

## Options

| Option | Command | Description |
|---|---|---|
| `--force` | `enable` | Enable before the cloud registry shows a second device (used by DevSpace provisioning) |
| `--interval SECONDS` | `reconcile --loop` | Loop interval (default 60) |

## Examples

```bash
# Turn on sync and pick what to share
openbase-coder sync enable
openbase-coder sync add ~/Projects/myapp
openbase-coder sync status

# Inspect and resolve a divergence after committing on both machines
openbase-coder sync conflicts
openbase-coder sync resolve 3f2a... --use-remote
```

## Notes

- Eligibility requires two or more non-phone devices with Tailscale
  identities in your Openbase Cloud device registry.
- The reconcile tick also runs automatically every minute inside the
  `openbase-routines` service whenever sync is enabled; the `reconcile`
  command exists for one-off runs and debugging.
- Settings are also exposed at `GET/PUT /api/sync/settings/` for the console
  Sync page.

# sync-daemon

Configure and manage Openbase Sync, the hub/edge mirror between two of your
computers. See [Sync Between Your Computers](../code-sync.md) for how it
works.

## Usage

```bash
openbase-coder sync-daemon COMMAND [ARGS]
```

## Subcommands

| Subcommand | Description |
|---|---|
| `pair hub\|join HUB\|leave\|candidates` | Pair this computer with your other computers, the same as the console Sync page (see below) |
| `configure` | Write `~/.openbase/sync/config.toml` and (by default) install and start the `sync-daemon` service |
| `install-binary SOURCE [--ctl PATH] [--edge PATH]` | Install the Openbase-provided `openbase-syncd` binary (and optionally the `openbase-sync` control binary and the `edge` companion) into `~/.openbase/bin` |
| `status` | Print the daemon status as JSON (peers, roots, open conflicts) |
| `conflicts` | Print open conflicts as JSON |
| `resolve ID keep_local\|use_remote` | Resolve one conflict |
| `install-hooks` | Install the Openbase Sync agent hooks and shell snippet |
| `judgment enable\|disable\|status` | Turn AI conflict labels from Openbase Cloud on or off for this computer, or show the setting (see below) |
| `disable` | Stop and remove the `sync-daemon` service; configuration and files are kept |

## pair

Pair computers without copying addresses or secrets. Both computers must be
signed in to the same Openbase account and connected to Openbase VPN; the
console **Sync** page runs the same steps.

| Command | Description |
|---|---|
| `pair candidates [--json]` | List your other computers on Openbase VPN with their sync role (always-on hub, edge, not syncing, offline) |
| `pair hub [--root PATH]...` | Make this computer the hub: listen on its Openbase VPN address, sync `~/Projects` plus the product folders (or the `--root` folders), generate the pair secret, install and start the service |
| `pair join HUB [--root PATH]...` | Make this computer an edge of `HUB` (its name or Openbase VPN address). The hub hands over its pair secret, ports and folders; `--root` keeps only some of the hub's folders |
| `pair leave [--yes]` | Stop syncing on this computer: remove the service and move `config.toml` to `~/.openbase/trash/`. Files are not touched |

`join` reports a clear error when the hub is offline, signed in to another
account, not set up as a hub yet, or runs an older Openbase. Every command
reports when the sync daemon is missing: update Openbase.

## configure options

| Option | Description |
|---|---|
| `--role hub\|edge` | Required. The hub is the always-on machine; the edge connects to it |
| `--listen ADDR` | Hub only (required): the hub's Openbase VPN address to listen on |
| `--peer ADDR` | Edge only (required): the hub's Openbase VPN address |
| `--pair-secret SECRET` | Edge only (required): the secret printed by the hub's `configure`; generated for a hub |
| `--root PATH` | A directory to mirror; repeatable. Paths may be written `~/...` |
| `--with-product-folders` | Also add `~/.openbase/thread-sync`, `~/.agents/skills` and linked skill-source folders, so thread sync and skills sync keep working |
| `--group NAME` | Sync group name (default `default`) |
| `--low-water-mb N` | Never write below this much free disk (default 10240) |
| `--anchor hub\|edge` | Which side keeps every file in full (default `hub`); choose `edge` when the hub has less disk |
| `--start/--no-start` | Install and start the service after writing the config (default on) |

At least one `--root` (or `--with-product-folders`) is required. Use the same
roots on both machines.

## judgment

Opt this computer in or out of AI conflict labels: Openbase Cloud labels text-file conflicts to help you choose, and never resolves them for you. The contents of both versions of a conflicting text file are sent to Openbase Cloud for classification, billed against a small free monthly allowance. See [AI conflict labels](../code-sync.md#ai-conflict-labels-opt-in).

| Command | Description |
|---|---|
| `judgment enable [--json] [--no-restart]` | Set `[judgment] enabled = true` and this computer's cloud device id in `config.toml`, register the choice with Openbase Cloud, and restart the `sync-daemon` service if it is installed |
| `judgment disable [--json] [--no-restart]` | Set `enabled = false` plus this computer's cloud device id, register the choice with Openbase Cloud, and restart the service |
| `judgment status [--json]` | Show whether labels are enabled and which device id the daemon uses |

Registering with Openbase Cloud needs you to be signed in. If it fails (for example offline), the command still saves the setting, prints a warning, and the choice is sent at the computer's next periodic check-in. Other settings in `config.toml` are kept as they are.

## Advanced: pins

A root in `config.toml` can list `pins`: root-relative paths held in full
only on the anchor side (`"."` = the whole root).

```toml
[[roots]]
id = "projects-media"
path = "~/Projects/media"
pins = ["raw-footage"]
```

Restart the service after editing the file:
`openbase-coder services restart sync-daemon`.

## Examples

```bash
# Pair from the command line (same as the Sync page)
openbase-coder sync-daemon pair hub              # on the always-on computer
openbase-coder sync-daemon pair join mini        # on the laptop

# On the hub (always-on Mac mini)
openbase-coder sync-daemon configure --role hub --listen 100.64.0.2 \
  --root ~/Projects --with-product-folders

# On the laptop, with the secret the hub printed
openbase-coder sync-daemon configure --role edge --peer 100.64.0.2 \
  --pair-secret <secret> --root ~/Projects --with-product-folders

openbase-coder sync-daemon status

# Turn on AI conflict labels for this computer
openbase-coder sync-daemon judgment enable
```

## Notes

- The companion `edge` command provides `edge run` (run a display-bound
  command on the laptop from the hub) and `edge forward` (forward a laptop
  port, such as Chrome DevTools on 9222, to the hub).
- Moving from the previous Syncthing-based sync: see
  [`sync migrate-from-syncthing`](sync.md#migrate-from-syncthing).

# codex

Start Codex with Openbase's profile, attached to this computer's
Openbase-managed app-server so the session is visible to and steerable from
Openbase.

## Usage

```bash
openbase-coder codex [--local | --remote] [CODEX ARGS...]
openbase codex [--local | --remote] [CODEX ARGS...]
```

Arguments after the optional Openbase flag go to `codex` unchanged.
`openbase-coder codex --help` shows the launcher's help; run plain
`codex --help` for Codex's own options.

| Flag | Effect |
|---|---|
| `--local` | Run on this computer even when it is a paired Openbase Sync edge. |
| `--remote` | Run on the Openbase Sync hub; fail instead of running locally. |

What runs locally, for a new session:

```bash
codex -p openbase --remote unix:// -C "$PWD" [CODEX ARGS...]
```

- `-p openbase-cloud` replaces `-p openbase` with an Openbase Cloud backend,
  and that session runs standalone (without `--remote`).
- `resume` and `fork` get `--remote` after the subcommand and keep the
  thread's own folder.
- Other subcommands (`exec`, `mcp`, …) only get `-p`.
- Without a running app-server the session starts standalone, with a
  one-line notice.
- Your own `--remote`, `--no-daemon` or `-C` is kept as given.

On a paired edge in a synced folder with the hub reachable, the session runs
on the hub instead. See
[Codex and Claude Code from Your Terminal](../agent-launchers.md).

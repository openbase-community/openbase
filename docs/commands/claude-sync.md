# claude-sync

Synchronize Claude Code session snapshots across devices.

There is no local home-to-home sync step: Openbase runs Claude Code sessions
in your own shared `~/.claude` home, so there is only one local session store
— sessions started in the terminal, IDEs, the desktop apps, and Openbase
voice all live in the same store and see each other instantly.

## Usage

```bash
openbase-coder claude-sync devices init
openbase-coder claude-sync devices status
openbase-coder claude-sync devices once
openbase-coder claude-sync devices run
```

`devices` exports and imports Claude Code session snapshots through
`~/.openbase/thread-sync` by default (an exchange directory shared
between machines, for example via [code sync](../code-sync.md)). Conflicts
are not merged; they are recorded in
`~/.openbase/claude-thread-device-sync-ledger.json` and shown by
`openbase-coder claude-sync devices status`.

`once` runs one export plus import pass; `export-once` and `import-once` run
each half separately. `run` polls continuously; the default `sync-workers`
Openbase service runs the same sweep on an interval.

## Options

```bash
openbase-coder claude-sync devices status
openbase-coder claude-sync devices once --stability-delay 0.2 --max-age-days 15
openbase-coder claude-sync devices run --interval 60 --max-age-days 15
```

Sessions that are active, recently changing, malformed, too old, or divergent
across devices are skipped instead of overwritten.

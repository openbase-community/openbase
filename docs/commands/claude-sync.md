# claude-sync

Synchronize Claude Code session history between normal Claude Code and
Openbase's managed Claude Code config.

## Usage

```bash
openbase-coder claude-sync once
openbase-coder claude-sync run
openbase-coder claude-sync devices once
```

`once` runs a conservative bidirectional sync pass between `~/.claude` and
`~/.openbase/claude_config`. It copies stable Claude Code project session JSONL
files and session companion files, records fingerprints in
`~/.openbase/claude-thread-sync-ledger.json`, and backfills Openbase Claude
thread metadata so imported sessions can appear in Openbase thread lists.

`run` polls continuously; the default `sync-workers` Openbase service runs the
same sweep on an interval.

`devices` exports and imports Openbase-managed Claude Code session snapshots
through `~/.openbase/claude-thread-sync` by default. Conflicts are not merged;
they are recorded in `~/.openbase/claude-thread-device-sync-ledger.json` and
shown by `openbase-coder claude-sync devices status`.

## Options

```bash
openbase-coder claude-sync once --stability-delay 0.2 --max-age-days 15
openbase-coder claude-sync run --interval 60 --max-age-days 15
openbase-coder claude-sync devices status
openbase-coder claude-sync devices run --interval 60 --max-age-days 15
```

Sessions that are active, recently changing, malformed, too old, or divergent in
both homes are skipped instead of overwritten.

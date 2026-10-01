# loops

Manage loops: recurring or event-triggered work. A loop pairs a **When**
(schedule, webhook triggers, and/or file triggers) with a **Then** (an agent
prompt or a shell command) and runs on this machine.

In the apps: the **Loops** page in the [console](../console.md) and
[desktop app](../desktop-app.md) lists loops, shows When → Then, and manages
triggers.

`openbase-coder routines` is an alias of the same command group.

## Usage

```bash
openbase-coder loops COMMAND [ARGS]
```

## Commands

| Command | Description |
|---|---|
| `list` | List loops |
| `show NAME` | Show one loop |
| `create NAME` | Create a loop (`--prompt` for agent loops, `--kind command --command` for command loops; `--time HH:MM` daily or `--interval-seconds N`) |
| `update NAME` | Update fields; `--enable` / `--disable` |
| `delete NAME` | Delete a loop |
| `run-due` | Run currently due loops (`--name`, `--force`) |
| `add-webhook-trigger NAME` | Add a webhook trigger; prints the ingest token and path |
| `add-file-trigger NAME` | Add a file (flag) trigger: run the loop when a file matching `--path GLOB` appears or changes |
| `remove-trigger NAME TRIGGER_ID` | Remove a trigger |
| `emit NAME` | Run a loop now with a local event payload (`--data JSON`) |
| `doctor` | Report loop health and scheduler liveness |
| `run-loop` | Long-lived scheduler process (run by the `openbase-routines` service) |

## Webhook Triggers

`add-webhook-trigger` creates a capability URL served by the local API at
`/api/hooks/t/<token>/`. With `--cloud` it also creates an Openbase Cloud
relay endpoint and prints `providerUrl` — a publicly reachable URL to paste
into the provider. Cloud stores deliveries durably and acks the provider
immediately; this machine polls pending events every 30 seconds (the
`cloud_webhook_events` job in the `sync-workers` service), runs them through
the same local checks, and acks them, so an offline machine catches up on its
next poll. Anyone who can POST to a trigger URL and pass the trigger's checks
can make the loop run, so:

- The token is a secret. Rotate by removing and re-adding the trigger.
- Optional `--hmac-secret` verifies provider signatures (SHA-256; header
  defaults to `X-Hub-Signature-256`).
- `--filter PATH OP VALUE` (repeatable) matches JSON payload fields. Ops:
  `equals`, `notEquals`, `contains`, `startsWith`, `endsWith`, `exists`,
  `regex`.
- Agent loops require `--sender-path` plus one or more `--allow-sender`
  values: external events may only start agent runs for verified, allowlisted
  senders.

Duplicate deliveries (same event id per trigger) are dropped. Event runs do
not consume the schedule: a webhook run never delays or replaces a daily or
interval run.

Agent loops receive the event as a "Triggering event" section appended to the
prompt; command loops receive it in the `SUPER_AGENTS_EVENT_JSON` environment
variable.

## File Triggers

`add-file-trigger NAME --path GLOB` runs the loop when a file matching the glob
is created or modified. The glob must be absolute (a leading `~` is expanded)
and may use `*`, `?`, and `**`. The `openbase-routines` scheduler scans every
file trigger on each sweep (about once a minute). Each `(path, mtime)` pair
fires once, so touching a file fires the loop again, and a file that is deleted
and recreated fires as a new file. Existing matches are recorded silently when
the trigger is added; pass `--fire-existing` to run for them too.

The event payload is:

```json
{"path": "/abs/dir/review-request.md", "name": "review-request.md", "dir": "/abs/dir", "mtime": 1790000000, "change": "created", "contents": "…"}
```

`change` is `created` or `modified`; `contents` is included for UTF-8 files up to 16 KB. Each sweep tracks at most 500 currently matching files per trigger, so narrow the glob or split it across loops when a directory can exceed that. `--filter PATH OP VALUE` applies to this payload (for example `--filter name endsWith -request.md`). File triggers need no sender allowlist, even on agent loops: a local file carries the same trust as a locally emitted event. Each file event runs the loop independently, so with `--fresh-thread-per-run` several files can be handled in parallel; prompts should claim their file (for example by writing a response next to it) rather than assume they are the only run.

### The `.triggers/` convention

Just as agents write reports for people under `.reports/`, they leave messages
for other agents and loops under a `.triggers/` directory at a project,
workspace, or worktree root: one Markdown file per message, with a small YAML
front matter (`kind`, `status`, `from`, `created_at`) and the message as the
body. A request file (`review-request.md`) is answered by a response file next
to it (`review-response.md`); the request is pending while the response is
missing or older than it. `.triggers/` is never committed: setup adds it to the
global Git ignore. Loops watch these files with file triggers, for example
`--path '/path/to/checkouts/*/.triggers/review-request.md'`. The
`openbase-recommended-loops` skill ships ready-made loops built on this
convention.

## Example

```bash
openbase-coder loops create pr-feedback \
  --prompt "Address the PR feedback in the triggering event." \
  --interval-seconds 86400

openbase-coder loops add-webhook-trigger pr-feedback --cloud \
  --description "PR comments" \
  --sender-path sender.id \
  --allow-sender 12345 \
  --filter comment.body startsWith /openbase \
  --hmac-secret "$(openssl rand -hex 32)"

openbase-coder loops emit pr-feedback --data '{"note": "test run"}'

openbase-coder loops create local-review \
  --prompt "Review the worktree whose .triggers/review-request.md is in the triggering event." \
  --time 04:00 --fresh-thread-per-run
openbase-coder loops add-file-trigger local-review \
  --path '/path/to/*-worktrees/*/.triggers/review-request.md'
```

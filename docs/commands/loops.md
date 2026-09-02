# loops

Manage loops: recurring or event-triggered work. A loop pairs a **When**
(schedule and/or webhook triggers) with a **Then** (an agent prompt or a shell
command) and runs on this machine.

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
```

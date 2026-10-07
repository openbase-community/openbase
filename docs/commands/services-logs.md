# services logs

Tail logs for one service.

## Usage

```bash
openbase-coder services logs NAME
```

## Example

```bash
openbase-coder services logs django-cli
```

Current services write a combined log at `~/.openbase/logs/<service>.log`.
The command also tails legacy `~/.openbase/logs/<service>.stdout.log` and
`.stderr.log` files when they exist.

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

Log files live at `~/.openbase/logs/<service>.log`. Older installs may still
have `~/.openbase/logs/<service>.stdout.log` and
`~/.openbase/logs/<service>.stderr.log`; the command tails those legacy files
when present.

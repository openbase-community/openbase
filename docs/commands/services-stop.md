# services stop

Stop all services or one named service.

## Usage

```bash
openbase-coder services stop [NAME]
```

This stops jobs through the host service manager and keeps them unloaded until
restarted.

## Example

```bash
openbase-coder services stop codex-app-server
```

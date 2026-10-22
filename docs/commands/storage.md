# storage

Use S3-compatible object storage through Openbase without seeing the keys. The full guide is [Databases and Object Storage](../databases-and-storage.md).

```bash
openbase-coder storage list [--json]
openbase-coder storage add NAME --endpoint URL --bucket BUCKET [--region REGION] [--virtual-host] \
    [--access-key-id ID] [--secret-access-key SECRET] [--readonly-access-key-id ID] [--readonly-secret-access-key SECRET]
openbase-coder storage proxy NAME [--port N] [--read-only] [--background] [--purpose TEXT]
openbase-coder storage stop NAME
openbase-coder storage ls NAME [PREFIX] [--limit N] [--json]
openbase-coder storage get NAME KEY [DESTINATION]
openbase-coder storage put NAME SOURCE [KEY] [--content-type TYPE]
openbase-coder storage rm NAME KEY
openbase-coder storage audit NAME [--json]
openbase-coder storage revoke NAME
openbase-coder storage restore NAME
openbase-coder storage remove NAME [--yes]
```

Keys are read from `OPENBASE_STORAGE_ACCESS_KEY_ID`, `OPENBASE_STORAGE_SECRET_ACCESS_KEY` and the `OPENBASE_STORAGE_READONLY_*` variables, or a hidden prompt, when the options are omitted. `proxy` serves `http://127.0.0.1:PORT` for any S3 client using path-style addressing and placeholder credentials, limited to the connection's bucket; `--read-only` uses the read-only key or, without one, refuses everything but GET and HEAD.

# db

Use Postgres databases through Openbase without seeing their passwords. The full guide is [Databases and Object Storage](../databases-and-storage.md).

```bash
openbase-coder db list [--json]
openbase-coder db add NAME [--url URL] [--readonly-url URL]        # or OPENBASE_DATABASE_URL / OPENBASE_DATABASE_READONLY_URL
openbase-coder db provision NAME [--region REGION]
openbase-coder db proxy NAME [--port N] [--read-only] [--background] [--purpose TEXT]
openbase-coder db stop NAME
openbase-coder db query NAME [SQL] [--file PATH] [--read-only] [--json]
openbase-coder db connect NAME [--read-only] [-- PSQL_ARGS...]
openbase-coder db readonly create NAME [--role-name ROLE] [--schema SCHEMA]
openbase-coder db readonly rotate NAME
openbase-coder db readonly drop NAME [--schema SCHEMA] [--yes]
openbase-coder db audit NAME [--json]
openbase-coder db revoke NAME
openbase-coder db restore NAME
openbase-coder db remove NAME [--teardown] [--yes]
```

`proxy` listens on `127.0.0.1:PORT` (`--port 0` picks a free port) and accepts any user with no password; it authenticates upstream with the leased role. `--read-only` requires read-only credentials, which `db readonly create` makes from the read-write role. `revoke` blocks further leases and closes running proxies within a minute. `remove --teardown` also deletes a database that `provision` created.

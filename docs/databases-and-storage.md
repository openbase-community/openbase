# Databases and Object Storage

Openbase can hold your Postgres databases' and S3-compatible buckets' credentials so that agents on any of your devices use them without ever seeing a password or key. Openbase Cloud keeps the credentials encrypted; `openbase-coder db` and `openbase-coder storage` lease them into a local process that proxies the connection on `127.0.0.1`. Your app, psql, an S3 SDK or an agent only ever sees the loopback endpoint. Every lease, change and revocation is recorded in Openbase Cloud, and you can revoke access from any device at any time.

This works the same on a Mac, a Linux desktop, a Docker install and a cloud workspace (the workspace's bootstrap credential carries the `connections` scope). Proxies are per machine: start one wherever the agent runs.

## Databases

```bash
openbase-coder db add shop --url "postgresql://user:password@host:5432/shop?sslmode=require"
openbase-coder db list
openbase-coder db proxy shop --port 5433 --background
openbase-coder db query shop "select count(*) from orders"
openbase-coder db connect shop                 # psql through a temporary proxy
openbase-coder db readonly create shop         # a SELECT-only role, stored as the read-only credentials
openbase-coder db query shop --read-only "select * from orders limit 5"
openbase-coder db stop shop
openbase-coder db audit shop
openbase-coder db revoke shop
```

`db add` stores the URL's host, port, database and `sslmode` as the connection target and the user and password as the read-write credentials. Prefer passing the URL through the `OPENBASE_DATABASE_URL` environment variable or the hidden prompt instead of `--url`, so the password never lands in your shell history. `--readonly-url` (or `OPENBASE_DATABASE_READONLY_URL`) stores an existing read-only role; `db readonly create` makes one for you.

`db proxy` listens on `127.0.0.1:<port>`; clients connect with any user and no password (`postgresql://127.0.0.1:5433/shop`), and the proxy opens the real connection with the leased role, authenticating upstream with SCRAM-SHA-256, MD5 or a cleartext password over TLS as the server requires. TLS to the upstream follows the stored `sslmode` (`require` by default; `verify-full` verifies the server certificate). Loopback traffic is plaintext; the proxy refuses TLS from local clients. `--read-only` uses the read-only role. `--background` detaches the proxy and `db stop` ends it; `db list` shows running proxies. A proxy re-checks its lease every minute and closes when the connection is revoked or removed.

`db query` runs SQL itself (simple query protocol, text results like psql), with `--json` for structured output and `--file` or stdin for longer scripts. `db connect` opens `psql` through a temporary proxy and closes it when psql exits.

`db provision NAME` creates a new small Postgres database on the provider Openbase Cloud is configured with (Neon) and stores it in the vault; `db remove NAME --teardown` deletes that database again. `db remove` without `--teardown` only forgets the entry.

## Object storage

```bash
OPENBASE_STORAGE_ACCESS_KEY_ID=... OPENBASE_STORAGE_SECRET_ACCESS_KEY=... \
  openbase-coder storage add uploads --endpoint https://s3.us-east-005.backblazeb2.com --bucket my-uploads --region us-east-005
openbase-coder storage proxy uploads --port 9000 --background
openbase-coder storage ls uploads photos/
openbase-coder storage put uploads ./report.pdf reports/2026-10.pdf
openbase-coder storage get uploads reports/2026-10.pdf ./
openbase-coder storage rm uploads reports/old.pdf
openbase-coder storage stop uploads
openbase-coder storage audit uploads
openbase-coder storage revoke uploads
```

A storage connection is one bucket at one S3-compatible endpoint (Backblaze B2, Cloudflare R2, Tigris, AWS S3, MinIO and others). `--region` is the SigV4 signing region: `auto` for R2 and Tigris, the bucket's region for B2 and AWS. `--virtual-host` addresses the bucket as a subdomain of the endpoint; path-style is the default. Add a read-only key pair with `--readonly-access-key-id` and `--readonly-secret-access-key` (or the `OPENBASE_STORAGE_READONLY_*` variables) when the provider can issue one.

`storage proxy` serves `http://127.0.0.1:<port>` for any S3 client using path-style addressing and placeholder credentials; the proxy ignores the client's signature, re-signs the request with the leased key (AWS Signature Version 4, unsigned payload) and forwards it. Requests for buckets other than the leased one are refused locally. `--read-only` uses the read-only key when one is stored and otherwise refuses everything but GET and HEAD in the proxy. SDK streaming uploads (`aws-chunked` bodies with trailing checksums) are decoded before forwarding; set `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` if a provider rejects an SDK's checksum headers.

Example clients:

```bash
AWS_ACCESS_KEY_ID=openbase AWS_SECRET_ACCESS_KEY=openbase \
  aws --endpoint-url http://127.0.0.1:9000 s3 ls s3://my-uploads/
```

```python
import boto3
from botocore.config import Config

s3 = boto3.client(
    "s3",
    endpoint_url="http://127.0.0.1:9000",
    aws_access_key_id="openbase",
    aws_secret_access_key="openbase",
    config=Config(s3={"addressing_style": "path"}),
)
```

`storage ls`, `get`, `put` and `rm` work without any SDK on the machine.

## Security model

- Credentials are stored encrypted in Openbase Cloud and returned only to a lease request from your own login or one of your cloud workspaces' machine tokens. The CLI keeps them in the proxy process's memory and never writes them to disk or prints them.
- Each lease records who (session or workspace), which device, which role and for what purpose. `db audit` and `storage audit` show the trail; the device also keeps a local log under `~/.openbase/connections/`.
- `revoke` blocks all further leases and closes running proxies within a minute; `restore` re-enables access. For a leaked read-only database role, `db readonly rotate` changes its password. For a leaked storage key, rotate it at the provider and re-add the connection.
- Read-only access is a real database role (`db readonly create` grants `SELECT` in the chosen schema and on future tables) or a provider-issued read-only key; the local method filter of `storage proxy --read-only` is a guardrail for the case where no such key exists.
- Proxies bind to `127.0.0.1` only. Anything that can reach loopback on that machine can use the proxy while it runs; stop it when the task is done.

## Recommended providers

For a new small Postgres database Openbase recommends [Neon](https://neon.com) (scale-to-zero compute, a free tier, branching and an API Openbase uses for `db provision`). For object storage Openbase recommends [Backblaze B2](https://www.backblaze.com/cloud-storage) (10 GB free, free API calls, per-bucket keys with read-only capability), with Cloudflare R2 and Tigris as alternatives. See the bundled `openbase-databases-and-storage` skill for how agents are expected to use these commands.

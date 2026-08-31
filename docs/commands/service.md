# Publish a Local Service

Use `openbase-coder service` when a site or other single-port HTTP service on
your computer should be available from another device. It publishes the local
service only inside your Openbase VPN/tailnet; it never enables Tailscale
Funnel or exposes the service to the public internet.

```bash
# Start the app locally, then give it a memorable tailnet URL.
openbase-coder service publish docs-preview 3000

# See URLs and gateway health.
openbase-coder service list

# Stop sharing it.
openbase-coder service unpublish docs-preview
```

`publish` verifies that `127.0.0.1:3000` is accepting connections, chooses
uncommon ports from the dynamic/private range `49152-65535`, and prints a URL
similar to:

```text
http://my-mac.example-tailnet:52807/docs-preview/
```

Prefer that URL over telling another device to use `localhost`: on a phone,
`localhost` means the phone itself. Traffic stays encrypted by the tailnet even
though the generated URL uses HTTP between tailnet peers.

## Persistence is opt-in

In an interactive terminal, `publish` asks whether to start the local gateway
at login with launchd and defaults to **No**. In non-interactive use it remains
session-only unless `--persist` is passed explicitly:

```bash
openbase-coder service publish docs-preview 3000 --persist
```

Persistent publications still use an automatically selected uncommon tailnet
port. If `--tailnet-port` is supplied, Openbase rejects common or registered
ports and accepts only `49152-65535`. The local app may continue to use its
normal port, such as `3000`; it remains bound to loopback.

Openbase Direct cannot publish arbitrary host services because it carries only
Openbase app traffic. Switch the computer to **Openbase VPN** before publishing.

## Naming and DNS boundary

Names use lowercase letters, numbers, and hyphens. Openbase puts the name in
the URL path on the computer's existing MagicDNS address. It does not use
`.local`, which is reserved for multicast DNS, and it does not claim to create
control-plane DNS records.

A dedicated hostname such as `docs-preview.example-tailnet` requires an
administrator-managed DNS record. Headscale supports this through
[`dns.extra_records`](https://headscale.net/stable/ref/dns/), while hosted
Tailscale's named
[`Services`](https://tailscale.com/docs/features/tailscale-services) require
admin definition, tagged hosts, and approval. Those provider-side operations
are intentionally outside this local command.

The local proxy is based on private
[`tailscale serve`](https://tailscale.com/docs/reference/tailscale-cli/serve)
routing and never uses Funnel. `.local` avoidance follows
[RFC 6762](https://www.rfc-editor.org/rfc/rfc6762).

## Docker and multiple ports

`service publish` represents one HTTP ingress. For a Docker Compose project
with several externally consumed ports or non-HTTP protocols, use the
[Docker tailnet pattern](../docker.md) instead. You can still publish a single
web gateway from a multi-container project when all browser traffic enters
through that one local HTTP port.

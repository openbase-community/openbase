# Publish a Local Service

Use `openbase-coder service` when a site or other single-port HTTP service on
your computer should be available from another device. It publishes the local
service only inside your Openbase VPN/tailnet; it never enables Tailscale
Funnel or exposes the service to the public internet.

```bash
# Start the app locally, then publish it with the established dynamic URL.
openbase-coder service publish docs-preview 3000

# Require a dedicated private hostname instead of allowing a port fallback.
openbase-coder service publish docs-preview 3000 --mode hostname

# See URLs and gateway health.
openbase-coder service list

# Stop sharing it.
openbase-coder service unpublish docs-preview
```

`publish` verifies that `127.0.0.1:3000` is accepting connections. It defaults
to the established uncommon-port mode so this new behavior cannot change an
existing workflow. `--mode hostname` explicitly opts into a dedicated Openbase
VPN hostname. The provider must advertise both private-hostname DNS allocation
and hostname Serve routing, and the name must resolve to this computer's
tailnet address before Openbase changes the registry or starts a gateway. A
supported hostname looks like:

```text
http://docs-preview.gabes-mac-mini-openbase.netmesh.openbase.cloud/
```

The default dynamic/private mode uses a port from `49152-65535`:

```text
http://gabes-mac-mini-openbase.netmesh.openbase.cloud:52807/docs-preview/
```

Use `--mode auto` only when you explicitly want Openbase to try the hostname
and safely fall back to a dynamic port when either capability is unavailable.
Use `--mode hostname` to fail instead of falling back. Supplying
`--tailnet-port` keeps the route dynamic and accepts only `49152-65535`.

Dedicated-hostname publications are root-mounted and forward every path and
query unchanged. Existing dynamic publications retain their established
`/<name>/` URL and one-prefix stripping for compatibility. The retired shared
`/services/<name>/` mode is unavailable. The local gateway and upstream target
bind only to `127.0.0.1`; they never bind `0.0.0.0`, and publication never uses
Funnel. Traffic between tailnet peers remains WireGuard-encrypted even when the
printed URL uses HTTP.

Prefer the printed URL over telling another device to use `localhost`: on a
phone, `localhost` means the phone itself.

## Persistence is opt-in

In an interactive terminal, `publish` asks whether to start the local gateway
at login with launchd and defaults to **No**. In non-interactive use it remains
session-only unless `--persist` is passed explicitly:

```bash
openbase-coder service publish docs-preview 3000 --persist
```

Persistent dynamic publications still use an automatically selected uncommon
tailnet port unless `--tailnet-port` is supplied. The local app may continue to
use its normal port, such as `3000`; it remains bound to loopback.

Openbase Direct cannot publish arbitrary host services because it carries only
Openbase app traffic. Dedicated private hostnames require **Openbase VPN**.

## Naming and DNS boundary

Names use lowercase letters, numbers, and hyphens. `.local` remains reserved
for multicast DNS. A dedicated hostname is used only when the Openbase VPN
provider explicitly reports the `{service}.{node_dns_name}` allocation pattern,
atomic hostname routing, and HTTP port 80, and local DNS confirms the result.
The CLI never invents an unresolved hostname or treats the node's ordinary
MagicDNS name as proof that a child name exists.

The local proxy is based on private
[`tailscale serve`](https://tailscale.com/docs/reference/tailscale-cli/serve)
routing and never uses Funnel. `.local` avoidance follows
[RFC 6762](https://www.rfc-editor.org/rfc/rfc6762).

Openbase VPN applies the complete desired Serve configuration atomically. The
signed helper derives targets from a fixed Openbase rule vocabulary, preserves
the built-in console and LiveKit routes, uses an ETag compare-and-swap, and
refuses to overwrite an unexpected configuration. Funnel is not present in the
desired state and cannot be requested through the helper surface. Until the
Openbase VPN helper and control plane advertise hostname routing and DNS
allocation, explicit `--mode auto` remains on the dynamic-port fallback and
`--mode hostname` fails closed. The default remains dynamic regardless.

## Docker and multiple ports

`service publish` represents one HTTP ingress. For a Docker Compose project
with several externally consumed ports or non-HTTP protocols, use the
[Docker tailnet pattern](../docker.md) instead. You can still publish a single
web gateway from a multi-container project when all browser traffic enters
through that one local HTTP port.

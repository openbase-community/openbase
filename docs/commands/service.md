# Publish a Local Service

Use `openbase-coder service` to make a local single-port HTTP service available to your other devices over Openbase VPN. Publications are private to the tailnet; they never enable Funnel or expose the app to the public internet.

```bash
# Start the app locally, then publish its root on a dedicated private hostname.
openbase-coder service publish docs-preview 3000

# See the exact URLs and gateway health.
openbase-coder service list

# Stop sharing it.
openbase-coder service unpublish docs-preview
```

`publish` verifies that `127.0.0.1:3000` accepts connections. By default it requires a dedicated Openbase VPN hostname, equivalent to `--mode hostname`. The provider must advertise both private-hostname DNS allocation and hostname Serve routing, and the allocated name must resolve to this computer's tailnet address before Openbase starts the gateway or changes Serve routing. A successful publication looks like:

```text
http://docs-preview.workstation.netmesh.openbase.cloud/
```

There is no port or service-name suffix in this URL. If the installed helper or control plane lacks hostname support, the default command fails with the capability error; it does not silently choose another URL format. Update the Openbase VPN components before retrying, or explicitly choose the root-mounted port mode below.

## Root-mounted only

Every publication forwards the incoming path and query unchanged, including WebSocket paths. The service name identifies the registry entry and optional hostname; it is never added to or stripped from the URL path. Applications do not need a base-path setting, a service-name redirect, or gateway-specific routes.

For example, a request to `/docs-preview/api?q=1` reaches the upstream at exactly `/docs-preview/api?q=1`, not `/api?q=1`. The gateway does not rewrite redirect locations or cookie paths. Both `/<service>/` publication prefixes and the older shared `/services/<service>/` mode are unsupported.

Existing dynamic registry entries remain readable and removable, but `service list` now prints their root URL. Restart existing gateways to load the root-only forwarding behavior, and replace old bookmarks with the printed root URL. Old prefixed paths are ordinary application paths, not compatibility aliases or redirects.

## Explicit port mode

If you intentionally want a dedicated uncommon port instead of a hostname:

```bash
openbase-coder service publish docs-preview 3000 --mode dynamic
# Optionally choose a private-range port explicitly.
openbase-coder service publish demo 4000 --mode dynamic --tailnet-port 52807
```

This mode uses a port in `49152-65535` and still serves at the root:

```text
http://workstation.netmesh.openbase.cloud:52807/
```

`--mode auto` explicitly permits trying a hostname and falling back to a root-mounted dynamic port when hostname capability is unavailable. It is not the default. `--tailnet-port` requires `--mode dynamic` or `--mode auto`; it cannot change the default hostname mode implicitly.

The app and local gateway bind only to `127.0.0.1`, never `0.0.0.0`. Tailnet traffic remains WireGuard-encrypted even when the printed URL uses HTTP. Give other devices the exact printed URL, not `localhost`, which would point at the device doing the browsing.

## Persistence is opt-in

Interactive `publish` asks whether to restore the gateway at login with launchd and defaults to **No**. Non-interactive publication is session-only unless `--persist` is explicitly supplied:

```bash
openbase-coder service publish docs-preview 3000 --persist
```

The local application needs its own lifecycle management. Persisting the gateway does not install or start the upstream app.

## Naming and DNS boundary

Names contain lowercase letters, numbers, and hyphens. `.local` is reserved for multicast DNS. Dedicated service names require Openbase VPN and its owner-scoped DNS allocator; ordinary node MagicDNS support does not imply that child service hostnames exist. Official Tailscale and unknown providers cannot allocate Openbase private service hostnames. Openbase Direct cannot publish arbitrary host services.

The local proxy uses private [`tailscale serve`](https://tailscale.com/docs/reference/tailscale-cli/serve) routing, never Funnel. The signed Openbase VPN helper derives routing from a fixed rule vocabulary, preserves built-in console and LiveKit routes, and applies changes atomically with an ETag compare-and-swap. It refuses to overwrite unexpected configuration. Hostname publication fails closed unless both the helper and control plane advertise the required capabilities and DNS resolves to the correct node.

## Docker and multiple ports

`service publish` represents one HTTP ingress. For a Docker Compose project with multiple externally consumed ports or non-HTTP protocols, use the [Docker tailnet pattern](../docker.md). You can publish one web gateway from a multi-container project when all browser traffic enters through that HTTP port.

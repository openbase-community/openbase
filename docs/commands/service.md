# Publish a Local Service

Use `openbase-coder service` to make a local single-port HTTP service available to your other devices over Openbase VPN. Access is private to your account's VPN devices, not the public internet. No Funnel is enabled.

```bash
openbase-coder service publish crm 3000
openbase-coder service list
openbase-coder service unpublish crm
```

Publication has one supported shape: a dedicated hostname serving the application at its root. An illustrative URL is `http://crm.n11111111111111111111111111111111.vpn.obs.so/`. Use the actual URL printed by the command, never an invented name. There is no explicit port, service-name path, personal name, or device name.

## Account namespace and private DNS

Production device names use `net.obs.so`; private service names use the sibling `vpn.obs.so` zone. Staging uses `net-staging.obs.so` and `vpn-staging.obs.so`. Service DNS must not be beneath the device MagicDNS zone: the VPN client's authoritative local resolver would return NXDOMAIN before consulting the split DNS route. Neither private device nor private service records are published in public DNS.

Each account has a permanent opaque namespace derived from its random enrollment identifier. Service names are unique within that account. To move a service between devices, unpublish it on the old device and publish the same name on the new device; its URL remains stable. Another account can independently publish the same service name.

Service records are not distributed through Headscale's global extra-record list. An independent VPN-only DNS resolver identifies the querying device through the VPN and returns records only for its account. Stock Headscale distributes a split DNS route containing the resolver address, not a shared list of private service names. The resolver's sole cross-account network exception is DNS port 53; application connections remain restricted to the same account. Names are not credentials, and knowing another account's name or IP does not authorize access.

Both the Cloud allocator and signed VPN helper must advertise the current account-private hostname contract. Publication verifies that DNS resolves exclusively to the local node before applying a route. Missing capability or failed resolution is an error: there is no automatic or explicit legacy port fallback. Update/configure the VPN components before retrying.

## Root-mounted only

Every request path and query is forwarded unchanged, including WebSockets. There is no prefix stripping, prefix alias, shared dispatcher, or app-specific redirect workaround. For example, `/crm/api?q=1` reaches the upstream at exactly that path, not `/api?q=1`. Redirect locations and cookie paths are not rewritten. Applications must not change their base path for publication.

The retired `--mode`, `--tailnet-port`, and path-based publication options are rejected. Old registry entries can be inspected for cleanup, but the updated helper rejects legacy dynamic publication routes. Remove old publications before replacing the helper and republish using their root hostname.

The app and local proxy bind only to `127.0.0.1`. Hostname routing uses private HTTP port 80; the upstream and loopback proxy can use unrelated local ports. Traffic between devices remains WireGuard-encrypted even though the URL uses HTTP. This is not browser HTTPS and does not provide a browser secure context. Apps may additionally require their own login; the publisher does not add a per-request Openbase browser login.

## Persistence is opt-in

Interactive publication asks whether to restore the gateway at login and defaults to **No**. Non-interactive publication is session-only unless explicitly requested:

```bash
openbase-coder service publish crm 3000 --persist
```

The upstream app needs its own lifecycle management. Persisting the gateway does not install or start that app.

## Provider boundary

This feature requires Openbase VPN and its authenticated Cloud allocator. Openbase Direct carries only Openbase app traffic and cannot publish arbitrary host services. Official Tailscale and unknown providers cannot allocate Openbase private service names. `.local` is reserved for multicast DNS.

The helper uses typed, atomic Serve rules, preserves the built-in console and LiveKit routes, and refuses to overwrite unexpected configuration. It accepts only root-mounted account hostnames resolving to this node and loopback proxy ports; callers cannot supply arbitrary targets, paths, or Funnel settings.

For Docker/multi-port projects, publish a single web ingress when one exists. One HTTP publication does not carry database, UDP, or other independent ports. PaaS deployment routing is a separate feature and is unchanged.

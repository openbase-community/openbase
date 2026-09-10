# Secure Maritime Workspaces

## Status (updated 2026-08-31)

Implemented on the `feature/secure-maritime-support` branch and not deployed.
The feature remains gated by `DEVSPACE_CONTAINER_WORKSPACES_ENABLED`. Production
enablement additionally requires an immutable runtime image digest, a
server-side Maritime API credential, provider lifecycle validation, and a
reviewed billing rate.

## Goal

Openbase Cloud can provision one isolated Maritime micro-VM for a container
Workspace without transferring a user's Openbase login, host installation
files, personal tailnet credential, or provider credential into that VM.
Stopping a Workspace preserves its `/data` volume; terminating it explicitly
revokes its Openbase and Netmesh identities before provider deletion.

This is a generic Workspace backend. It does not encode a personal testing
routine, developer machine, or one-off deployment.

## Security invariants

- Provider web exposure is disabled (`publicWeb: false`). The local Openbase
  API binds to `127.0.0.1` by default and is reached only through the user's
  isolated Openbase Direct network. One caveat: LiveKit's ICE-TCP mux
  (`7881/tcp`) cannot loopback-bind (its TCP mux ignores the bind address),
  so in netmesh mode that one listener is on all container interfaces and
  isolation relies on the provider exposing no non-tunnel interface.
- The container runs as an unprivileged user and refuses Maritime startup as
  root. Runtime-created credential files use mode `0600`; the state directory
  uses mode `0700`.
- Durable runtime state is below `/data`. Image filesystem state is disposable.
- The runtime image is configured by immutable `@sha256:` digest. Mutable tags
  are rejected by the Cloud control plane.
- The Maritime provider token stays in Openbase Cloud. It is used only on the
  server-to-provider request and is never placed in agent environment variables.
- Bootstrap and provider control-plane clients require HTTPS; configuration
  that would transmit either credential over plaintext is rejected.
- A Workspace never receives host `auth.json`, owner access/refresh JWTs,
  reusable Tailscale auth keys, or broad owner credentials.
- Bootstrap grants are random, short-lived, single-use values. Cloud stores
  only their SHA-256 hashes. Issuing a replacement revokes any outstanding
  grant for that Workspace.
- Bootstrap mints one machine identity whose exact scopes are `llm_proxy` and
  `audio_proxy`, plus a non-reusable, expiring Netmesh enrollment key. The CLI
  rejects any broader or differently ordered scope set.
- Owner identity pinning stores only the Openbase subject and normalized email;
  it is not an authentication credential. Inbound user JWTs still require
  signature validation and a matching subject.
- The Django API and `openbase-tunneld` control API are loopback-bound and
  bearer-token protected. Local capability tokens are generated on the
  Workspace, stored with mode `0600`, and are not returned by client APIs.
- Stop/sleep preserves the provider agent, `/data`, machine token, and enrolled
  Netmesh node so a normal wake reconnects. Explicit terminate revokes the
  outstanding bootstrap, machine token, and matching Netmesh node before
  deleting the provider agent and durable disk.

## Control-plane contract

Cloud creates the agent with resource limits, an idle TTL, `publicWeb: false`,
and secret environment entries containing only non-user configuration plus one
bootstrap grant. The only credential in that environment is the bootstrap
grant; it becomes inert after exchange or expiry.

The container sends the grant in the `Authorization` header using the
`Openbase-Bootstrap` scheme to:

```text
POST /api/openbase/devspaces/bootstrap/exchange/
```

The successful response contains:

- one Openbase machine token, display prefix, deterministic Workspace install
  ID, and the exact two proxy scopes;
- non-secret owner subject/email data for local owner pinning;
- one non-reusable Netmesh enrollment key and its control-plane metadata.

It never contains an owner access token or refresh token. The exchange is
serialized with a database row lock. A consumed, expired, revoked, malformed,
or non-container grant receives the same generic rejection.

## Runtime bootstrap sequence

1. Maritime starts the digest-pinned Openbase image as the image's unprivileged
   user with its durable disk mounted at `/data`.
2. `openbase-coder provision --kind container` exchanges the one-time grant.
3. The CLI atomically persists the machine token and owner pin under `/data`
   with mode `0600`. It stages the one-time Netmesh key there for the supervisor.
4. Normal setup creates installation state and local service credentials. It
   finds the cached scoped machine token, so no owner login is requested.
5. The entrypoint passes the staged enrollment key to `openbase-tunneld` and
   deletes it once the daemon reports an enrolled, forwarding node — not
   before, so a failed first enrollment (boot-time egress lag, restart before
   login) can retry with the same single-use key. The daemon stores only its
   enrolled node state and its own authenticated loopback-control capability.
6. The entrypoint supervises Openbase services and exposes them only over the
   per-user Netmesh forwarders.

If setup exits after a successful exchange but before service startup, the
durable staged credentials allow the next boot to resume without replaying the
grant. The enrollment key remains single-use and is deleted when consumed.

## Threat model

| Threat | Boundary and mitigation | Residual risk / response |
| --- | --- | --- |
| Provider control-plane breach | Workspace environment contains no owner JWT, host auth file, reusable tailnet key, or provider token; bootstrap expires and is single-use. | An attacker could control the VM or read its scoped machine token. Revoke the Workspace machine token and Netmesh node, then terminate/rebuild. |
| Bootstrap interception or replay | TLS transport, header delivery, hash-only database storage, short expiry, row lock, one-time consumption, generic errors. | Interception before first use permits one bootstrap. Revoke credentials and issue a new Workspace; inspect provider access logs without preserving the plaintext grant. |
| Compromised container process | Unprivileged runtime, private provider networking, exact proxy scopes, local capability auth, owner-subject pinning. | The process can use that Workspace's LLM/audio allowance and its own durable files. It cannot refresh owner sessions or administer other Workspaces. |
| Malicious local process | Loopback APIs require file-backed capability tokens; sensitive files are `0600` beneath a `0700` data directory. | Processes running as the same Unix user remain inside the same trust domain. Rebuild the Workspace after arbitrary-code compromise. |
| Mutable/supply-chain image | Cloud rejects non-digest image references; the embedded network daemon builds from the same reviewed checkout. | Digest pinning proves identity, not safety. Scan and sign release images and update the configured digest deliberately. |
| Accidental public exposure | `publicWeb` is false, the API binds loopback, and access is through per-user Netmesh isolation. | A provider regression could violate policy. Validate the effective provider configuration before production enablement. |
| Destructive cleanup | Stop and terminate are distinct. Stop preserves `/data`; terminate revokes identities before provider deletion. | Provider deletion may irreversibly delete the disk. UI/API callers must reserve terminate for explicit deletion. |

## Safe migration from experimental deployments

Experimental agents must not be adopted in place or have their volume mounted
into a first-class Workspace. They may contain copied owner refresh tokens,
host auth files, provider credentials, or reusable network keys that the secure
bootstrap cannot prove absent.

For each experiment:

1. Stop it and preserve its volume unchanged while deciding whether forensic
   inspection is needed. Do not boot or execute it merely to inspect files.
2. Revoke the exact owner session/token family, machine token, and network key
   that were copied or minted for the experiment. Avoid account-wide revocation
   unless the exact target cannot be identified.
3. Preserve a metadata-only incident record. Do not copy credential values or
   private filesystem paths into reports, tickets, commits, or logs.
4. Create a new first-class container Workspace through the Cloud API. It must
   receive a fresh grant and an immutable reviewed image.
5. Move only reviewed, non-credential user data into the new `/data` volume.
   Never migrate `auth.json`, machine-token files, tailnet state, environment
   secret files, or agent/provider metadata wholesale.
6. After the retention decision, delete the old agent/volume explicitly and
   record whether deletion is recoverable. If the provider cannot snapshot or
   export through the approved tooling, keep the stopped volume until that
   decision is made.

## Production enablement checklist

- Build, scan, and publish the runtime image; set `MARITIME_IMAGE` to its digest.
- Store `MARITIME_API_TOKEN` only in the Cloud app's server-side secret store.
- Confirm provider create/start/stop/delete response schemas in staging and
  verify the effective agent has no public web endpoint — including that no
  provider network path reaches the all-interfaces LiveKit ICE-TCP listener
  on `7881/tcp` (see the exposure caveat above).
- Set and review Maritime CPU, memory, disk, idle TTL, bootstrap TTL, and hourly
  billing settings.
- Run migration checks and the focused Cloud/CLI tests.
- Exercise create, failed bootstrap, retry, sleep/wake, explicit terminate, and
  credential revocation against an isolated non-production account.
- Keep `DEVSPACE_CONTAINER_WORKSPACES_ENABLED=0` until those checks pass.

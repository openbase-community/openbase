# Self-Hosting the Device Registry

Openbase Coder normally uses Openbase Cloud for one small but essential job
during setup: the **device registry**, the rendezvous where your signed-in
devices (Mac, iPhone, Android) publish their names and tailnet addresses so
they can find each other. Everything else — your agents, code, and
conversations — already runs on your own machines.

Self-hosting replaces that registry with one you run yourself, so the core
Openbase Coder loop works with no Openbase-operated service in the path. It is
built from two public packages: the
[api-core](https://github.com/openbase-community/openbase-drf-api-core)
backbone (accounts and authentication) and the
[openbase-devices](https://github.com/openbase-community/openbase-devices) app
(the registry itself).

## What works, and what doesn't

Self-hosting covers device rendezvous only. Supported:

- Account signup/login against your own server (email + password).
- Device registration, deregistration, and the cross-device onboarding state
  the desktop app and CLI use to pair your Mac and phone.
- Device-to-device connectivity over **your own Tailscale tailnet** — the
  registry stores the addresses; Tailscale carries the traffic.

Not included — these are Openbase Cloud services and simply don't exist on a
self-hosted registry:

- **Push notifications and voice calls** (APNs/FCM push and VoIP wake-ups
  require Openbase's app signing credentials).
- **LLM and audio proxying** — bring your own API keys; agents run with your
  local backend credentials.
- **Sharing, fleet features relayed through the cloud, managed deploys
  (Openbase Cloud PaaS), and Cloud DevSpaces.**
- **The Openbase-managed tailnet (netmesh)** — self-hosted setups use stock
  [Tailscale](https://tailscale.com) with your own account and tailnet.
- **Mobile apps pointed at your server**: the released iOS and Android builds
  are pinned to Openbase Cloud today. The CLI and desktop app fully support a
  custom registry URL; a mobile override is on the roadmap, so a self-hosted
  registry currently pairs desktop devices with each other, not with the stock
  mobile apps.

## Running the registry

You need Docker and a hostname your devices can reach (a Tailscale MagicDNS
name works well).

```sh
git clone https://github.com/openbase-community/openbase-devices.git
cd openbase-devices/selfhost
cp .env.example .env   # set DJANGO_SECRET_KEY, POSTGRES_PASSWORD, ALLOWED_HOSTS
docker compose up --build -d
```

Create your account on the server:

```sh
docker compose exec web django-admin ensure_known_user \
  --email you@example.com --password 'choose-a-password'
```

Serve it over HTTPS (put your usual reverse proxy in front of port 8000, or
use `tailscale serve`).

## Pointing Openbase Coder at your registry

Set the registry URL before logging in, using the CLI's cloud override:

```sh
export OPENBASE_CODER_CLI_WEB_BACKEND_URL=https://registry.example.com
printf '%s' 'choose-a-password' | \
  openbase-coder login --email you@example.com --password-stdin
```

To make the override permanent for the whole install (services included), add
the same variable to `~/.openbase/.env`. The desktop app rides on the CLI, so
it follows the same setting.

After login, device registration happens automatically; `openbase-coder
doctor` and the desktop onboarding flow will show your devices syncing through
your own server.

## Connectivity

Join every device to the same Tailscale tailnet with your own Tailscale
account, and select Tailscale as the tailnet provider during onboarding. The
registry only stores the addresses your devices report — no device traffic
flows through it, and a registry outage never interrupts working devices.

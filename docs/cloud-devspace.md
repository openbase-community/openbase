# Cloud DevSpace

A Cloud DevSpace (shown as a **Sandbox** in the Openbase Cloud dashboard) is a
cloud Linux workspace that runs the full Openbase Coder runtime —
`openbase-coder`, LiveKit, Codex, and the background services — on an instance
Openbase Cloud launches for you. The instance provisions itself at launch: it
signs in to your Openbase Cloud account, points coding sessions at the Openbase
Cloud backend, defaults voice audio to Openbase Cloud, and starts the services.
You do not need direct OpenAI, Anthropic, Cartesia, or AssemblyAI accounts for
this path.

DevSpaces come in two kinds, chosen at launch:

- **Full (GUI) workspace** — a remote Linux desktop you reach through the
  Amazon DCV web or native client. The Openbase Coder desktop app (the same
  app as on macOS, built for Linux) is pre-installed, pinned to the dock, and
  opens automatically in the desktop session.
- **Headless workspace** — no remote desktop. The workspace is reached through
  Openbase-mediated networking, and you interact with it from the phone apps
  and the [web console](console.md).

From the apps' point of view a DevSpace is just another backend host: once it
is on your tailnet, the [iOS app](ios-tabs.md) adds it under
**Settings → Backend Host** exactly like a Mac, and the
[web console](console.md) is reachable at `http://<host>:18080`.

## Start the Sandbox

1. Open `https://app.openbase.cloud` and sign in.
2. Go to the dashboard. The dashboard opens the Sandboxes page.
3. In `Get access`, click `Subscribe` if access is not active, or ask a
   coordinator to activate complimentary access and then click `Refresh access`.
4. Click `Continue to launch`.
5. In `Spin up your Sandbox`, choose a sandbox size. Use `m6i.2xlarge
   (recommended)` unless you have a reason to choose another size.
6. Click `Spin up Sandbox` or `Start Sandbox`.
7. Wait on `Waiting for Sandbox` until the page says the credentials are ready.
8. Choose the browser connection flow and click `Open Sandbox in Browser`.
9. Continue past the certificate prompt only if the IP address matches the
   Sandbox URL shown on the page.
10. On `Sign in to your Sandbox`, sign in with:
    - Username: `ubuntu`
    - Password: the password shown on the page
    - Session: `openbase`

The browser connection is the Amazon DCV web client. It gives you a Linux
desktop in the cloud instance.

## First Connection

When the desktop appears, the Openbase Coder desktop app is already running
(or one click away in the dock). Openbase Cloud sign-in, backend selection,
and service startup already happened during launch provisioning — there are
no terminal commands to run.

The desktop app walks you through the one step that must be yours: private
networking. It opens Tailscale's browser authentication page; sign in to the
same tailnet your iPhone uses and approve the new Linux device. The app then
joins your tailnet, enables Tailscale SSH, and registers the DevSpace with
your Openbase account so the phone apps can find it. The DevSpace never joins
anyone else's tailnet — only the one you authenticate.

## Get the iOS Host Name

Prefer the Tailscale DNS name when MagicDNS is enabled:

```bash
tailscale status --json | jq -r '.Self.DNSName // empty' | sed 's/[.]$//'
```

If that prints nothing, use the Tailscale IPv4 address:

```bash
tailscale ip -4
```

The iOS app builds these URLs from the host:

- Openbase Coder API: `http://<host>:18080`
- LiveKit signaling: `ws://<host>:7880`

## Connect From the iOS App

1. On the iPhone, open Tailscale and sign in to the same tailnet.
2. Open the Openbase iOS app and sign in to Openbase Cloud.
3. Open `Settings`.
4. In `Backend Host`, enter a friendly `Name`.
5. In `Tailscale DNS or IP`, enter the DNS name or `100.x.y.z` IP from the
   Linux instance.
6. Tap `Add Backend`.
7. Select the new backend in `Selected Backend`.
8. Open `Call`.
9. Start a voice call and speak to the dispatcher.

The first call should request a room token from the Linux instance, connect to
LiveKit over Tailscale, and dispatch the `livekit-agent` worker running in the
cloud desktop.

After one successful connection identifies the backend as an Openbase Cloud
Workspace, later calls automatically resume that Workspace when idle shutdown
has stopped its EC2 instance. The Call screen shows whether it is checking,
starting, or waiting for the Workspace, and lets you cancel the pending call.
Cancelling the call does not stop an EC2 startup that Openbase Cloud has already
accepted.

Openbase deliberately does not attempt this recovery for an unrecognized or
ordinary Mac/Linux backend. An existing saved DevSpace that has never reported
its Cloud identity to the current iOS app therefore needs one successful manual
start and call before automatic resume is available.

## Quick Recovery

If the iOS app cannot connect, open a terminal in the DCV desktop and check:

```bash
tailscale status
tailscale serve status
openbase-coder services status
openbase-coder doctor
```

If services started before Tailscale was authenticated, start them again:

```bash
openbase-coder services start
```

If the call reaches `Connecting...` or `Waiting for Agent`, inspect recent logs:

```bash
openbase-coder services logs livekit-server
openbase-coder services logs livekit-agent
```

## Manual Fallback

Older images, or an instance whose launch provisioning did not complete, can
be brought up by hand from a terminal in the DCV desktop. None of this is
needed on a healthy current DevSpace.

Authenticate Tailscale directly (instead of through the desktop app):

```bash
sudo tailscale up
```

Open the printed URL in a browser inside the desktop (`firefox &`; install it
with `sudo snap install firefox` if missing), sign in to the same tailnet your
iPhone uses, and approve the device. Confirm with `tailscale status` and
`tailscale ip -4` — the IP should be a `100.x.y.z` address.

Sign in to Openbase Cloud and select the cloud backend:

```bash
openbase-coder login
openbase-coder backend use openbase_cloud
```

Use the underscore spelling, `openbase_cloud`, for compatibility with cloud
images that have an older `openbase-coder` CLI installed.

Start the default services, which also configures the Tailscale Serve routes
the iOS app uses (`18080 → 7999` API, `7880` LiveKit signaling):

```bash
openbase-coder services start
```

Verify with `openbase-coder services status` and `openbase-coder doctor` —
both should report healthy services and healthy Tailscale Serve routes. If
`doctor` reports missing Openbase Cloud audio configuration on an older image,
refresh setup and start services again:

```bash
openbase-coder setup --backend openbase_cloud --audio-provider openbase-cloud
openbase-coder services start
openbase-coder doctor
```

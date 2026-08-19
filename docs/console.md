# Web Console & Openbase Cloud

The Openbase Coder console is the dashboard UI. The
[desktop app](desktop-app.md) embeds it, the local runtime serves it in any
browser, and Openbase Cloud hosts your account at
`https://app.openbase.cloud`.

## Reaching the Console

- **Desktop app** — the dashboard is the console; no browser needed.
- **Local browser** — `http://127.0.0.1:7999` on the Mac running the
  runtime, or `http://<tailscale-host>:18080` from any device on your
  tailnet.
- **From the iOS app** — the Console tab opens the local console in an
  embedded browser with your CLI auth token injected automatically; the Diff
  tab opens the mobile-optimized diff view at `/mobile/diff`.

Authentication uses the local CLI token managed by `openbase-coder login`
and the runtime; the iOS app and desktop app handle this for you.

!!! tip "Managing Openbase Cloud from the terminal"

    Openbase Cloud also has its own Heroku-style CLI, `openbase`, for apps,
    deploys, logs, config, and usage — a fast alternative to this dashboard.
    See the [Openbase Cloud CLI docs](https://docs-cloud.openbase.cloud). It
    shares this sign-in: `openbase login` runs `openbase-coder login`.

## Console Pages

The console serves the same pages as the desktop app dashboard — Overview,
Projects, Threads, Reports, Dispatch, Approvals, Routines, Skills,
Templates, Diff, Status, Devices, Instructions, Tools, Launchctl, and
Settings. See [Desktop App](desktop-app.md#the-dashboard) for the full tour,
including what each page can do on iPhone.

Differences in a browser:

- Electron-only features are absent: the guided onboarding flow, app
  auto-update notices, and LiveKit companion screen sharing.
- The Diff page supports a mobile layout (`/mobile/diff`), which is what the
  iOS Diff tab embeds.
- Plugins can register additional console pages, rendered as iframes; they
  appear in the sidebar when installed. See
  [plugins](commands/plugins.md).

Useful shortcuts: **Cmd/Ctrl+B** toggles the sidebar; in a thread,
**Enter** sends the prompt and **Shift+Enter** inserts a newline.

## Openbase Cloud (app.openbase.cloud)

`https://app.openbase.cloud` is your Openbase Cloud account. As a user you
touch it for:

- **Sign-in** — `openbase-coder login`, the desktop app, and the iOS app all
  authenticate against it via browser OAuth.
- **Device onboarding** — during setup your Mac registers itself here so the
  iOS app can find it ("Link Your Computer" pairing).
- **Subscription** — the Openbase Cloud coding backend and extras such as
  Apple Music playback during muted calls are tied to your cloud
  subscription.
- **Cloud DevSpace** — launching a cloud sandbox that runs the full Openbase
  runtime on Linux. See [Cloud DevSpace](cloud-devspace.md).

Both apps link to it directly: the desktop sidebar's **Cloud** item and the
iOS sidebar's **Cloud** tab.

### Deploying apps (Openbase Cloud PaaS)

Openbase Cloud also runs a managed platform-as-a-service for deploying your
own apps — a Heroku-style push-to-deploy flow, separate from the Coder coding
runtime these docs cover. You connect a GitHub repository in the **Deployment**
dashboard at `https://app.openbase.cloud`, and pushes to the tracked branch
build and release automatically for both backend and frontend apps. Config
vars, secrets, hostnames, logs, releases, and usage are all managed there.

The same account drives it from the terminal with the separate `openbase`
CLI (`openbase apps`, `openbase logs`, `openbase config`, …); it shares this
sign-in, so `openbase login` runs `openbase-coder login`. See the
[Openbase Cloud CLI docs](https://docs-cloud.openbase.cloud) for the full
deploy workflow.

**On iPhone:** the Cloud tab opens app.openbase.cloud in the embedded
browser, and onboarding's "Start with Cloud" path uses it without any local
pairing.

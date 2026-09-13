# Web Console & Openbase Cloud

The Openbase Coder console is the dashboard UI. The
[desktop app](desktop-app.md) embeds it, the local runtime serves it in any
browser, and Openbase Cloud hosts your account at
`https://app.openbase.cloud`.

## Reaching the Console

- **Desktop app** — the dashboard is the console; no browser needed.
- **Local browser** — run `openbase-coder auth open-console` on the computer
  running the runtime. It opens `127.0.0.1:7999` with an owner-only,
  installation-scoped capability and removes that capability from the address
  bar immediately after launch.
- **Another tailnet device** — use the iOS/Android app, which supplies your
  owner JWT. Merely knowing the Tailscale, Netmesh, or Docker URL is
  intentionally insufficient to authenticate to the runtime.
- **From the iOS app** — the Console tab opens the local console in an
  embedded browser with your CLI auth token injected automatically; the Diff
  tab opens the mobile-optimized diff view at `/mobile/diff`.

Authentication uses either the owner's Openbase JWT or the runtime's local
installation capability. The iOS, Android, and desktop apps handle this for
you; a browser launched manually without the capability remains unauthenticated.

!!! tip "Managing Openbase Cloud from the terminal"

    Openbase Cloud also has its own Heroku-style CLI, `openbase`, for apps,
    deploys, logs, config, and usage — a fast alternative to this dashboard.
    See the [Openbase Cloud CLI docs](https://docs-cloud.openbase.cloud). It
    shares this sign-in: `openbase login` runs `openbase-coder login`.

## Console Pages

The console serves the same pages as the desktop app dashboard — Overview,
Projects, Threads, Reports, Dispatch, Approvals, Routines, Skills, Memories,
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

## Tabs and Panes

Normal navigation replaces the focused tab's view. Right-click a thread, report, or project and choose **Open in new tab**, **Open to the right**, or **Open below** to keep several views open. The tab strip appears when more than one tab is open. Each pane has its own tabs, and the layout can be resized, rearranged, maximized, or restored with the layout undo and redo controls.

The address follows the focused item; it does not encode the entire workspace. The layout is saved separately on the current device. Unsent prompts and report edits stay with their tab during navigation and rearrangement, but are not saved across app restarts. Closing a tab with unfinished text asks for confirmation.

In **Settings > Interface > Tab position**, choose **Horizontal** (the default, above the content) or **Vertical** (beside the navigation sidebar, grouped by pane). The preference is saved on this device without changing the layout. Vertical tabs support reordering within the list, closing, and the same right-click actions. Narrow screens use horizontal tabs to preserve content space.

In the Electron desktop app:

| Action | macOS | Windows / Linux |
| --- | --- | --- |
| Next / previous tab in focused pane | Control-Tab / Control-Shift-Tab | Ctrl-Tab / Ctrl-Shift-Tab |
| Next / previous tab (alternate) | Command-Option-Right / Left | - |
| Split right | Command-\\ | Ctrl-\\ |
| Split down | Command-Option-\\ | Ctrl-Alt-\\ |
| Select tab 1 through 8 in focused pane | Command-1 through 8 | Ctrl-1 through 8 |
| Select last tab in focused pane | Command-9 | Ctrl-9 |
| Close focused tab | Command-W | Ctrl-W |

The console uses the same bindings where the browser delivers them to the page; browser-reserved tab shortcuts may take precedence. Dialogs and open menus keep keyboard control until dismissed. Separate native windows are not currently supported by the workspace layout.

In Electron, closing a tab with the keyboard uses the same unfinished-text confirmation as its close button and leaves the window open. The final tab stays open.

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

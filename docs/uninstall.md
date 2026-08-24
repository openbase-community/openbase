# Uninstall Openbase Coder

Uninstall does not depend on the `openbase-coder` command. Use normal macOS,
Linux, and Python tool cleanup commands so you can remove Openbase even if the
CLI environment is broken.

Openbase Coder state lives in four places, each removed in its own section
below: the CLI runtime state in `~/.openbase`, the desktop app's Electron
state and caches under `~/Library`, a few Openbase entries in your shared
Codex/Claude agent configs (`~/.codex`, `~/.claude`), and the iOS app's
on-phone state (removed automatically when you delete the app from the
iPhone).

## Service Cleanup With The CLI

If the CLI still runs, this removes every service plist and wrapper (it works
even when `~/.openbase` has already been deleted):

```bash
openbase-coder services uninstall
```

Otherwise use the manual commands below.

## macOS Launchd Services

Stop and remove the launchd jobs first:

```bash
for plist in "$HOME"/Library/LaunchAgents/com.openbase.coder.*.plist; do
  [ -e "$plist" ] || continue
  launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
done

rm -f "$HOME"/Library/LaunchAgents/com.openbase.coder.*.plist
```

## Linux Systemd User Services

If the machine was set up with systemd user units, stop and remove them first:

```bash
systemctl --user stop 'com.openbase.coder.*.service' 2>/dev/null || true
rm -f "$HOME"/.config/systemd/user/com.openbase.coder.*.service
systemctl --user daemon-reload
```

## Remove The CLI Package

Remove the persistent `openbase-coder` command with the same tool used to
install it:

=== "uv tool"

    ```bash
    uv tool uninstall openbase-coder
    ```

=== "pipx"

    ```bash
    pipx uninstall openbase-coder
    ```

=== "pip"

    ```bash
    pip uninstall openbase-coder
    ```

Setup also writes an `openbase-coder` shim that the tools above do not own
(it wraps either the standalone package launcher or a development
workspace's virtualenv). Remove it explicitly:

```bash
rm -f "$HOME"/.local/bin/openbase-coder
```

## Remove Or Archive Local State

Only remove or archive `~/.openbase` after the service jobs above are stopped
and deleted. That directory contains logs, tokens, plugins, generated service
wrappers, the workspace checkout, and the local database.

This covers the CLI runtime state only. The desktop app keeps separate
Electron state outside `~/.openbase` — see
[Remove The Desktop App](#remove-the-desktop-app) below.

To remove it completely:

```bash
rm -rf "$HOME"/.openbase
```

To keep a backup instead:

```bash
backup="$HOME/.openbase.backup.$(date +%Y%m%d-%H%M%S)"
mv "$HOME"/.openbase "$backup"
echo "Archived Openbase state at $backup"
```

## Remove The Desktop App

Delete the app itself, then its persistent storage. Electron keeps per-app
state (localStorage, IndexedDB, cookies, window data) outside the app bundle,
so deleting the app alone leaves it behind — and a later reinstall would
silently pick up the old state:

```bash
rm -rf "/Applications/Openbase Coder.app"

# Electron user data — current and older app identities:
rm -rf "$HOME/Library/Application Support/@openbase/coder-desktop"
rm -rf "$HOME/Library/Application Support/openbase-coder-desktop"
rm -rf "$HOME/Library/Application Support/coder-desktop"
```

The auto-updater and the screen-share companion keep their own caches and
preferences (downloaded updates alone can exceed 1 GB):

```bash
rm -rf "$HOME/Library/Caches/@openbasecoder-desktop-updater"
rm -rf "$HOME/Library/Caches/tech.openbase.coder.desktop" \
       "$HOME/Library/Caches/tech.openbase.coder.desktop.ShipIt" \
       "$HOME/Library/Caches/tech.openbase.coder.LiveKitCompanion"
defaults delete tech.openbase.coder.desktop 2>/dev/null || true
defaults delete tech.openbase.coder.LiveKitCompanion 2>/dev/null || true
rm -rf "$HOME/Library/HTTPStorages/tech.openbase.coder.desktop" \
       "$HOME/Library/HTTPStorages/tech.openbase.coder.LiveKitCompanion" \
       "$HOME/Library/HTTPStorages/tech.openbase.coder.LiveKitCompanion.binarycookies"
rm -rf "$HOME/Library/Saved Application State/tech.openbase.coder.desktop.savedState"
```

On iPhone, deleting the Openbase app removes its local state (the CLI auth
token in the Keychain and the backend host list); there is nothing else to
clean up on the phone.

## Clean Up Shared Agent Configs

Openbase registers exactly two things in your own Codex and Claude homes: the
`super-agents` MCP server and the session-ID hook. It stores no separate
agent credential — Openbase used your normal Codex and Claude Code logins
directly, and those are untouched by uninstalling. To remove the Openbase
entries:

- In `~/.codex/config.toml`: delete the `[mcp_servers.super-agents]` table
  and the `[[hooks.SessionStart]]` entry pointing at
  `~/.openbase/hooks/inject-session-id.sh` (plus its matching `[hooks.state]`
  trust entry).
- In `~/.claude.json`: delete the `mcpServers.super-agents` entry.
- In `~/.claude/settings.json`: delete the session-ID hook entry under
  `hooks`.

Setup also symlinks skills into the shared homes. Remove the symlinks that
point at Openbase sources (the workspace checkout or `~/.openbase`
packages) — inspect with `ls -l ~/.codex/skills ~/.claude/skills` and delete
Openbase-owned links, leaving any real skill directories of your own in
place.

## Optional Tailscale Cleanup

If this machine only used Tailscale Serve for Openbase, clear the local Serve
configuration after services are removed:

```bash
tailscale serve reset
```

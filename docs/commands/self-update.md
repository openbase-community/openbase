# self-update

Update a standalone Openbase Coder install to the latest release for its
channel. The full contract (feed, atomicity, rollback, quiescing, channels,
signing) lives in the workspace `AUTO_UPDATE.md` guide.

Standalone installs also update **automatically**: the background routines
service checks the release feed every few hours and applies available updates
on its own (deferring while a voice session is active, except for required
updates). Set `OPENBASE_CODER_AUTO_UPDATE=0` in `~/.openbase/.env` to opt out
and update only manually. Automatic-update output is written to
`~/.openbase/logs/self-update.log`.

In the apps: the [desktop app](../desktop-app.md#auto-update) updates itself
separately via Electron auto-update and shows a footer indicator when a CLI
update is available; the [iOS app](../ios-tabs.md) updates through
TestFlight/App Store.

## Usage

```bash
openbase-coder self-update            # update if a newer release exists
openbase-coder self-update --check    # check only; do not install
openbase-coder self-update --force    # update even during a voice session
openbase-coder self-update --json     # machine-readable result (UI-driven)
```

Check the installed versions and update availability at any time:

```bash
openbase-coder version
```

It prints the CLI version, the active standalone package (target, channel,
layout), and whether a newer release is available.

## Behavior

Developer workspace installs cannot check release feeds or apply releases, even with `--force` or an inherited package-directory setting. An older packaged updater also stops if the active installation has switched to a developer workspace, including while waiting for a call or downloading. Developer status ignores cached release-update flags from a previous packaged installation.

1. Refuses in development-workspace installs (git-managed; no auto-update).
2. Fetches `update-manifest.json` for the install's channel — `stable` from
   the latest GitHub release, `beta` from the newest non-`.dev` release with a
   manifest, and `staging` from the newest `.dev` release with a manifest —
   and verifies its Ed25519 signature when the build embeds the release public
   key.
3. Compares versions and honors `min_supported_version` and the package
   `layout_version`; a release with a newer layout than the updater
   understands is reported as `blocked` (reinstall via the desktop app or
   install.sh).
4. Defers when a voice session is active unless `--force` is passed.
5. Downloads the target tarball, verifies its SHA-256, extracts it to
   `~/.openbase/packages/standalone/releases/<version>-<target>/`, and
   smoke-runs the new launcher.
6. Atomically flips the `current` symlink, keeping the outgoing release
   behind `previous`, then regenerates and restarts services with the new
   CLI, rebuilds the plugin site when the bundled Python changed, and
   refreshes `~/.openbase/bin/codex` when Openbase installed it.
7. Rolls back to `previous` (and reinstalls services) if the post-update
   health gate fails; older releases are pruned, keeping two.

Network timeouts, connection failures, transient HTTP errors, and incomplete downloads leave the running release intact. Automatic attempts retry after one minute, doubling the delay up to fifteen minutes; manual attempts report the error so you can retry. Downloads restart from the beginning and must pass the complete archive checksum before activation. Temporary storage failures during preparation also retry automatically. A signal-killed download is retried by the running routines service, and the next attempt removes its partial archive. Retry waits continue to honor update opt-outs and developer-install boundaries.

Activation holds the same lock as service restarts and managed Codex refreshes. A busy service batch defers the update before changing `current`; the updater delegates its lease only to its synchronous activation commands, so those commands can install services without deadlocking. Services are ordered by their declared dependencies, and a provider readiness failure triggers the existing rollback path.

A failed service rollback reports that recovery is incomplete instead of claiming that the old services were restored. The current runtime pointer is restored first; resolve the reported error and run `openbase-coder services install` if service restoration fails.

## Statuses

`updated`, `up-to-date`, `deferred` (voice session), `blocked` (newer package
layout), `rolled-back` (health gate failed; exit code 1).

## Related

- `GET /api/update/status/` (`?refresh=1` re-checks the feed) and
  `POST /api/update/apply/` expose the same functionality to the desktop app
  and console; apply runs detached and logs to `~/.openbase/logs/self-update.log`.
- Update flags also appear in the `versions` block of
  `GET /api/onboarding/status/`.

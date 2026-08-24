# Mac App Download

The desktop app is the production install path: it bundles the
complete CLI runtime and walks you through setup in the app — no terminal
required — including your coding backend, managed voice audio, and iPhone
pairing. (Prefer working from source? See
[Developer Setup](developer-setup.md). On Windows, see
[Run in Docker](../docker.md).)

Before you start, check the shared [prerequisites](index.md#prerequisites) —
in particular, install and sign in to Tailscale if you want iPhone-to-Mac
voice networking.

## Download and Open

Download the Apple Silicon app from [Downloads](../downloads.md) and open
it. The app activates its bundled CLI runtime and walks you through the
guided setup flow — see
[Desktop App](../desktop-app.md#install-and-first-run-setup) for what each
step looks like.

The bundled runtime package includes Python, Openbase Coder dependencies,
the console build, and LiveKit server, so no developer tooling (Git, `uv`,
Node) is required.

## Prefer the Terminal?

If you would rather run the underlying setup commands yourself instead of
letting the app drive them, follow
[Manual Setup for the Desktop App](../manual-installation.md); it is the
same installation, operated by hand from your own terminal.

## Optional: Fully Local Audio

After setup, for fully local speech-to-text and text-to-speech:

```bash
openbase-coder setup --audio-provider local
```

This requires an Apple Silicon Mac — see [Local-Only Mode](../local-only.md).

## After Setup

Authenticate with Openbase Cloud (required for iOS app pairing and cloud
onboarding):

```bash
openbase-coder login
```

Then verify the install with the
[health check commands](index.md#health-check), and continue with the
[next steps](index.md#next-steps).

If a development workspace install already exists on this machine, the two
cannot coexist — [uninstall](../uninstall.md) it first.

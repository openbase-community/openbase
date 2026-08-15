#!/usr/bin/env bash
# Container entrypoint for the Openbase Coder runtime.
#
# Networking: Tailscale is the networking layer, as on every other install —
# phones reach the runtime via `tailscale serve` (18080 -> 7999 API, 7880
# LiveKit signaling) and LiveKit advertises the tailnet IP for media. The
# entrypoint supervises an in-container tailscaled running unprivileged with
# userspace networking; node identity persists in the ~/.openbase volume.
# Set OPENBASE_CODER_NETWORK_MODE=local to opt out (loopback-only testing).
#
# First run (empty ~/.openbase volume): performs a non-interactive
# `openbase-coder setup`, installs the pinned livekit-server, and writes
# container-appropriate overrides into the generated env file. Every run:
# regenerates the per-service wrapper scripts (the same ones launchd/systemd
# installs execute) and supervises them, restarting any service that exits.
#
# Passing any arguments bypasses the supervisor and execs them instead,
# so `docker run <image> openbase-coder --help` and `docker run <image> bash`
# behave as expected.
set -euo pipefail

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

DATA_DIR="${OPENBASE_CODER_CLI_DATA_DIR:-$HOME/.openbase}"
ENV_FILE="$DATA_DIR/.env"
WRAPPER_DIR="$DATA_DIR/launchd"
NETWORK_MODE="${OPENBASE_CODER_NETWORK_MODE:-tailscale}"

# Run a command under a restart-on-exit loop, prefixing its output.
start_supervised() {
    name="$1"
    shift
    (
        while :; do
            rc=0
            "$@" 2>&1 || rc=$?
            echo "[supervisor] exited with status $rc; restarting in 5s"
            sleep 5
        done
    ) 2>&1 | sed -u "s/^/[$name] /" &
}

shutdown() {
    trap - TERM INT
    kill 0 2>/dev/null || true
    wait || true
    exit 0
}
trap shutdown TERM INT

# --- Tailscale (the networking layer) ---------------------------------------
tailscale_state="unavailable"
manage_tailscaled=0
if [ "$NETWORK_MODE" = "tailscale" ]; then
    TS_DIR="$DATA_DIR/tailscale"
    mkdir -p "$TS_DIR" "$DATA_DIR/bin"
    if [ -n "${TS_SOCKET:-}" ]; then
        # External tailscaled (e.g. a tailscale/tailscale sidecar sharing this
        # network namespace, with its socket volume mounted here).
        ts_socket="$TS_SOCKET"
    else
        ts_socket="$TS_DIR/tailscaled.sock"
        manage_tailscaled=1
    fi

    # Product code (service wrappers, device registration, serve health) calls
    # a bare `tailscale`, and wrappers prepend ~/.openbase/bin to PATH — shim
    # the CLI there so every call talks to the right daemon socket.
    printf '#!/bin/sh\nexec /usr/bin/tailscale --socket=%s "$@"\n' "$ts_socket" \
        >"$DATA_DIR/bin/tailscale"
    chmod 0755 "$DATA_DIR/bin/tailscale"
    export PATH="$DATA_DIR/bin:$PATH"

    if [ "$manage_tailscaled" = "1" ]; then
        # Userspace networking needs no privileges; kernel TUN is used when
        # the container actually grants it (root + /dev/net/tun).
        tun_args=(--tun=userspace-networking)
        if [ "$(id -u)" = "0" ] && [ -e /dev/net/tun ]; then
            tun_args=()
        fi
        start_supervised tailscaled /usr/sbin/tailscaled \
            --state="$TS_DIR/tailscaled.state" \
            --socket="$ts_socket" \
            "${tun_args[@]}"
    fi

    for _ in $(seq 1 30); do
        [ -S "$ts_socket" ] && break
        sleep 1
    done

    ts_backend_state() {
        tailscale status --json 2>/dev/null \
            | sed -n 's/.*"BackendState": *"\([^"]*\)".*/\1/p' | head -1
    }
    tailscale_state="$(ts_backend_state)"
    if [ "$tailscale_state" != "Running" ] && [ -n "${TS_AUTHKEY:-}" ]; then
        echo "[entrypoint] Joining tailnet with auth key ..."
        tailscale up --authkey="$TS_AUTHKEY" \
            --hostname="${TS_HOSTNAME:-openbase-coder}" || true
        tailscale_state="$(ts_backend_state)"
    fi
    if [ "$tailscale_state" != "Running" ]; then
        echo "[entrypoint] Tailscale is not connected (state: ${tailscale_state:-unknown})."
        echo "[entrypoint] Authenticate with:"
        echo "[entrypoint]   docker exec -it <container> tailscale up"
        echo "[entrypoint] Services and serve routes recover automatically after login."
    fi
fi

# --- First-run setup ---------------------------------------------------------
if [ ! -f "$DATA_DIR/installation.json" ]; then
    echo "[entrypoint] First run: setting up Openbase Coder in $DATA_DIR ..."
    setup_args=(
        --backend "${OPENBASE_CODER_BACKEND:-openbase-cloud}"
        --audio-provider "${OPENBASE_CODER_AUDIO_PROVIDER:-openbase-cloud}"
        --skip-services
        --json-progress
    )
    if [ -n "${OPENBASE_CODER_WORKSPACE_DIR:-}" ]; then
        setup_args+=(--workspace-dir "$OPENBASE_CODER_WORKSPACE_DIR")
    fi
    if [ -n "${ASSEMBLY_AI_API_KEY:-}" ]; then
        setup_args+=(--assembly-ai-api-key "$ASSEMBLY_AI_API_KEY")
    fi
    if [ -n "${CARTESIA_API_KEY:-}" ]; then
        setup_args+=(--cartesia-api-key "$CARTESIA_API_KEY")
    fi
    openbase-coder setup "${setup_args[@]}"
fi

# --- Container env overrides -------------------------------------------------
# Service wrappers source the env file with `set -a` after inheriting the
# process environment, so the file's values win; container-appropriate
# settings must live in the file itself (last assignment takes effect).
# Rewritten every start so mode switches take effect on restart.
tmp_env="$(mktemp)"
awk '/^# BEGIN docker overrides/{skip=1} !skip{print} /^# END docker overrides/{skip=0}' \
    "$ENV_FILE" >"$tmp_env"
{
    echo "# BEGIN docker overrides"
    echo "LIVEKIT_NETWORK_MODE=$NETWORK_MODE"
    echo "OPENBASE_CODER_CLI_HOST=${OPENBASE_CODER_CLI_HOST:-0.0.0.0}"
    echo "OPENBASE_CODER_CLI_ALLOWED_HOSTS=${OPENBASE_CODER_CLI_ALLOWED_HOSTS:-*}"
    if [ "$NETWORK_MODE" = "local" ]; then
        echo "LIVEKIT_BIND_IP=${LIVEKIT_BIND_IP:-0.0.0.0}"
    elif [ "$manage_tailscaled" = "1" ] && [ "$(id -u)" != "0" ]; then
        # Userspace tailscaled has no tailscale0 netdev; inbound tailnet
        # media is proxied to loopback, so that is the media interface.
        echo "LIVEKIT_INTERFACE=${LIVEKIT_INTERFACE:-lo}"
    fi
    echo "# END docker overrides"
} >>"$tmp_env"
mv "$tmp_env" "$ENV_FILE"

# Setup only auto-installs the pinned livekit-server for dev workspaces with
# uv project state; install it explicitly here (idempotent: checks version).
python -c "from openbase_coder_cli.livekit_install import ensure_pinned_livekit_server; ensure_pinned_livekit_server()"

# Regenerate wrappers every start so binary paths track image upgrades.
openbase-coder services regenerate

# --- Tailscale serve routes --------------------------------------------------
# Idempotent; retried in the background until Tailscale is connected, so an
# interactive `tailscale up` after boot needs no container restart.
if [ "$NETWORK_MODE" = "tailscale" ]; then
    (
        while :; do
            if [ "$(ts_backend_state)" = "Running" ] \
                && python -c "from openbase_coder_cli.services.tailscale_serve import configure_tailscale_serve; configure_tailscale_serve()" 2>&1; then
                echo "serve routes configured (18080 -> 7999, 7880 -> 7880)"
                break
            fi
            sleep 15
        done
    ) 2>&1 | sed -u "s/^/[tailscale-serve] /" &
fi

# --- Runtime services ----------------------------------------------------------
default_services="livekit-server livekit-agent django-cli sync-workers openbase-routines"
if [ -f "$WRAPPER_DIR/codex-app-server.sh" ]; then
    default_services="$default_services codex-app-server"
fi
services="${OPENBASE_CODER_SERVICES:-$default_services}"

for name in $services; do
    wrapper="$WRAPPER_DIR/$name.sh"
    if [ ! -f "$wrapper" ]; then
        echo "[entrypoint] WARN: no wrapper for $name at $wrapper; skipping"
        continue
    fi
    start_supervised "$name" bash "$wrapper"
done

echo "[entrypoint] Supervising services: $services"
echo "[entrypoint] Local API: http://localhost:7999/api/health/"
if [ "$NETWORK_MODE" = "tailscale" ]; then
    ts_host="$(tailscale status --json 2>/dev/null \
        | sed -n 's/.*"DNSName": *"\([^"]*\)".*/\1/p' | head -1 | sed 's/\.$//')"
    if [ -n "$ts_host" ]; then
        echo "[entrypoint] Tailnet API: http://$ts_host:18080/api/health/"
    fi
fi
echo "[entrypoint] To authenticate with Openbase Cloud, run:"
echo "[entrypoint]   docker exec -it <container> openbase-coder login"
wait

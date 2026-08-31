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
umask 077

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

DATA_DIR="${OPENBASE_CODER_CLI_DATA_DIR:-$HOME/.openbase}"
ENV_FILE="$DATA_DIR/.env"
WRAPPER_DIR="$DATA_DIR/launchd"
RUN_DIR="$DATA_DIR/run"
NETWORK_MODE="${OPENBASE_CODER_NETWORK_MODE:-tailscale}"
MARITIME_MODE=0
if [ "${OPENBASE_CODER_RUNTIME:-}" = "maritime" ]; then
    MARITIME_MODE=1
    if [ "$(id -u)" = "0" ]; then
        echo "[entrypoint] Refusing to run the Maritime workspace as root." >&2
        exit 1
    fi
    case "$DATA_DIR" in
        /data/*) ;;
        *)
            echo "[entrypoint] Maritime state must live below /data." >&2
            exit 1
            ;;
    esac
    PROJECTS_DIR="${OPENBASE_CODER_PROJECTS_DIR:-/data/workspace}"
    case "$PROJECTS_DIR" in
        /data/*) ;;
        *)
            echo "[entrypoint] Maritime projects must live below /data." >&2
            exit 1
            ;;
    esac
fi

# Tell the runtime the entrypoint (not launchd/systemd) supervises services;
# status checks then read the $RUN_DIR/<name>.pid files maintained below.
export OPENBASE_CODER_SERVICE_SUPERVISOR=external
mkdir -p "$DATA_DIR" "$RUN_DIR"
if [ "$MARITIME_MODE" = "1" ]; then
    mkdir -p "$PROJECTS_DIR"
    chmod 0700 "$PROJECTS_DIR"
fi
chmod 0700 "$DATA_DIR"
rm -f "$RUN_DIR"/*.pid

# `codex login` writes ~/.codex (which setup symlinks the service auth to);
# keep it inside the volume so backend logins survive container recreation.
if [ ! -e "$HOME/.codex" ]; then
    mkdir -p "$DATA_DIR/normal-codex-home"
    ln -s "$DATA_DIR/normal-codex-home" "$HOME/.codex"
fi

# Run a command under a restart-on-exit loop, prefixing its output and
# maintaining the service pidfile the runtime's status checks read.
start_supervised() {
    name="$1"
    shift
    (
        while :; do
            "$@" 2>&1 &
            svc_pid=$!
            echo "$svc_pid" >"$RUN_DIR/$name.pid"
            rc=0
            wait "$svc_pid" || rc=$?
            rm -f "$RUN_DIR/$name.pid"
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
    if [ "$MARITIME_MODE" = "1" ]; then
        openbase-coder provision --kind container
        unset OPENBASE_CODER_BOOTSTRAP_TOKEN
    else
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
    if [ "$NETWORK_MODE" = "netmesh-tsnet" ]; then
        echo "LIVEKIT_NETWORK_MODE=local"
    else
        echo "LIVEKIT_NETWORK_MODE=$NETWORK_MODE"
    fi
    if [ "$MARITIME_MODE" = "1" ]; then
        echo "OPENBASE_CODER_CLI_HOST=${OPENBASE_CODER_CLI_HOST:-127.0.0.1}"
        echo "OPENBASE_CODER_CLI_ALLOWED_HOSTS=${OPENBASE_CODER_CLI_ALLOWED_HOSTS:-localhost,127.0.0.1,.netmesh.openbase.cloud}"
    else
        echo "OPENBASE_CODER_CLI_HOST=${OPENBASE_CODER_CLI_HOST:-0.0.0.0}"
        echo "OPENBASE_CODER_CLI_ALLOWED_HOSTS=${OPENBASE_CODER_CLI_ALLOWED_HOSTS:-*}"
    fi
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

# Voice-agent model files: baked into current images, but heal older images
# and volumes (a fast no-op when the cache is warm; non-fatal offline).
python -m openbase_coder_cli.livekit_agent.livekit download-files \
    || echo "[entrypoint] WARN: could not verify LiveKit model files; voice calls may fail until they download."

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
if [ "$NETWORK_MODE" = "netmesh-tsnet" ]; then
    default_services="openbase-tunneld $default_services"
fi
if [ -f "$WRAPPER_DIR/codex-app-server.sh" ]; then
    default_services="$default_services codex-app-server"
fi
# code-sync is conditional: supervise it only when the feature is enabled
# (enabling it from the console requires a container restart to take effect).
if [ -f "$WRAPPER_DIR/code-sync.sh" ] \
    && python -c "from openbase_coder_cli.sync_config import code_sync_enabled; import sys; sys.exit(0 if code_sync_enabled() else 1)" 2>/dev/null; then
    default_services="$default_services code-sync"
fi
services="${OPENBASE_CODER_SERVICES:-$default_services}"
netmesh_authkey=""
if [ "$NETWORK_MODE" = "netmesh-tsnet" ] \
    && [ -f "$DATA_DIR/bootstrap-netmesh-authkey" ]; then
    netmesh_authkey="$(/bin/cat "$DATA_DIR/bootstrap-netmesh-authkey")"
    rm -f "$DATA_DIR/bootstrap-netmesh-authkey"
fi

for name in $services; do
    wrapper="$WRAPPER_DIR/$name.sh"
    if [ ! -f "$wrapper" ]; then
        echo "[entrypoint] WARN: no wrapper for $name at $wrapper; skipping"
        continue
    fi
    if [ "$name" = "openbase-tunneld" ] && [ -n "$netmesh_authkey" ]; then
        export TS_AUTHKEY="$netmesh_authkey"
        start_supervised "$name" bash "$wrapper"
        unset TS_AUTHKEY
        netmesh_authkey=""
    else
        start_supervised "$name" bash "$wrapper"
    fi
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
if [ "$MARITIME_MODE" != "1" ]; then
    echo "[entrypoint] To authenticate with Openbase Cloud, run:"
    echo "[entrypoint]   docker exec -it <container> openbase-coder login"
fi
wait

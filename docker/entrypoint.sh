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

# --- Privilege drop (Maritime) ----------------------------------------------
# Maritime's VM init launches the image entrypoint as root regardless of the
# Dockerfile USER and without the image ENV (observed 2026-09-28; without
# this the root guard below exited PID 1 and the VM kernel-panicked). The
# image's own bin directories (~/.openbase/bin, the cli venv) are owned by
# the unprivileged user, so they must never be searched while we are root: a
# binary planted there would run as root at the next boot. Keep a fixed
# system PATH and absolute paths until the drop is done, then re-exec this
# script as the image user with no inheritable capabilities, an empty
# bounding set and no_new_privs, environment otherwise intact.
if [ "${OPENBASE_CODER_RUNTIME:-}" = "maritime" ] && [ "$(/usr/bin/id -u)" = "0" ]; then
    export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    if ! /usr/bin/id openbase >/dev/null 2>&1; then
        echo "[entrypoint] Refusing to run the Maritime workspace as root (no 'openbase' user)." >&2
        exit 1
    fi
    /bin/mkdir -p /data
    # Maritime's VM init boots with an empty /etc/hosts and no hostname, so
    # "localhost" does not resolve and the LiveKit worker can never reach its
    # server. Repair both here, the only point where we still hold root.
    if ! /usr/bin/getent hosts localhost >/dev/null 2>&1; then
        /usr/bin/printf '127.0.0.1\tlocalhost\n::1\tlocalhost\n' >>/etc/hosts 2>/dev/null \
            || echo "[entrypoint] Could not add localhost to /etc/hosts." >&2
    fi
    case "$(/bin/hostname 2>/dev/null)" in
        ""|"(none)"|localhost)
            /bin/hostname "${OPENBASE_TSNET_HOSTNAME:-openbase-workspace}" 2>/dev/null || true
            ;;
    esac
    # Reparent only root-owned entries (the platform-created mount and
    # first-boot directories); never rewrite a user's existing files, and
    # never follow links (-h) so nothing under /data can redirect the chown.
    /usr/bin/find /data -maxdepth 2 -user root \
        -exec /bin/chown -h openbase:openbase {} + 2>/dev/null || true
    echo "[entrypoint] Started as root; re-executing as 'openbase'." >&2
    exec /usr/bin/setpriv --reuid=openbase --regid=openbase --init-groups \
        --inh-caps=-all --bounding-set=-all --no-new-privs \
        /usr/bin/env HOME=/home/openbase USER=openbase LOGNAME=openbase \
        "$0" "$@"
fi

# The Dockerfile ENV is not guaranteed to reach us: Maritime's VM init rebuilds
# the environment from its own store and drops the image's ENV, so the cli
# venv fell off PATH ("openbase-coder: command not found", 2026-09-28).
# Re-assert the image defaults here (mirrors the Dockerfile ENV block; keep
# the two in sync); explicit platform values still win. This runs only after
# the privilege drop above, so the user-owned directories it puts on PATH are
# never searched by root.
case ":${PATH:-}:" in
    *":/opt/openbase-coder/workspace/cli/.venv/bin:"*) ;;
    *)
        export PATH="/home/openbase/.openbase/bin:/opt/openbase-coder/workspace/cli/.venv/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
        ;;
esac
export OPENBASE_CODER_WORKSPACE_DIR="${OPENBASE_CODER_WORKSPACE_DIR:-/opt/openbase-coder/workspace}"
export OPENBASE_CODER_CLI_CONSOLE_BUILD_DIR="${OPENBASE_CODER_CLI_CONSOLE_BUILD_DIR:-/opt/openbase-coder/console-dist}"
export UV_PYTHON_DOWNLOADS="${UV_PYTHON_DOWNLOADS:-never}"

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
    # Maritime VMs accept no inbound connections, so netmesh (outbound-only
    # tunneld) is the only mode that works there. Default to it so a missing
    # env value can never silently boot the tailscale path.
    NETWORK_MODE="${OPENBASE_CODER_NETWORK_MODE:-netmesh}"
    # Maritime's VM init keeps its exec/command servers on 8081/8082, the
    # LiveKit worker's default health port; Cloud passes the port explicitly,
    # and this default covers a hand-launched workspace.
    export LIVEKIT_AGENT_PORT="${LIVEKIT_AGENT_PORT:-18081}"
    # The privilege drop at the top of this script already re-executed us as
    # the image user; nothing past this point may run as root.
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
    # The image ENV pins OPENBASE_CODER_WORKSPACE_DIR to an image-layer path,
    # so it only wins here when the platform points it somewhere durable.
    PROJECTS_DIR="${OPENBASE_CODER_PROJECTS_DIR:-/data/workspace}"
    case "${OPENBASE_CODER_WORKSPACE_DIR:-}" in
        /data/*) PROJECTS_DIR="$OPENBASE_CODER_WORKSPACE_DIR" ;;
    esac
    case "$PROJECTS_DIR" in
        /data/*) ;;
        *)
            echo "[entrypoint] Maritime projects must live below /data." >&2
            exit 1
            ;;
    esac
    export OPENBASE_CODER_PROJECTS_DIR="$PROJECTS_DIR"
fi
# "netmesh" is the canonical env-contract value; "netmesh-tsnet" is the
# internal tailnet-provider id. Accept both spellings.
if [ "$NETWORK_MODE" = "netmesh" ]; then
    NETWORK_MODE="netmesh-tsnet"
fi

# The one-time bootstrap grant is only ever consumed by `provision` during
# first-run setup below; keep it (a credential, even if usually already
# consumed) out of every supervised service's environment on every boot.
BOOTSTRAP_TOKEN_FOR_PROVISION="${OPENBASE_CODER_BOOTSTRAP_TOKEN:-}"
unset OPENBASE_CODER_BOOTSTRAP_TOKEN

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

# The claude-code backend writes ~/.claude and ~/.claude.json; keep both in
# the volume too, or its login is lost on any container recreate. Newer
# Claude CLIs follow CLAUDE_CONFIG_DIR (state file included); the symlinks
# cover tools that still hardcode the home paths.
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$DATA_DIR/normal-claude-home}"
mkdir -p "$CLAUDE_CONFIG_DIR"
if [ ! -e "$HOME/.claude" ]; then
    ln -s "$CLAUDE_CONFIG_DIR" "$HOME/.claude"
fi
if [ ! -e "$HOME/.claude.json" ]; then
    [ -f "$CLAUDE_CONFIG_DIR/.claude.json" ] || printf '{}\n' >"$CLAUDE_CONFIG_DIR/.claude.json"
    ln -s "$CLAUDE_CONFIG_DIR/.claude.json" "$HOME/.claude.json"
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
        OPENBASE_CODER_BOOTSTRAP_TOKEN="$BOOTSTRAP_TOKEN_FOR_PROVISION" \
            openbase-coder provision --kind container
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
        echo "LIVEKIT_NETWORK_MODE=netmesh"
    else
        echo "LIVEKIT_NETWORK_MODE=$NETWORK_MODE"
    fi
    if [ -n "${OPENBASE_CODER_CLI_PORT:-}" ]; then
        # Container env must win over any stale port in the env file so the
        # API binds where the platform (and tunneld's 18080 forward) expect.
        echo "OPENBASE_CODER_CLI_PORT=$OPENBASE_CODER_CLI_PORT"
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

# Render every managed instruction file before any service starts. First-run
# setup does this once; an image upgrade on a persisted /data does not re-run
# setup, and workspaces redeployed in place came back with only AGENTS.md
# until something rendered the rest (2026-10-09). Idempotent, and it leaves
# user-authored files alone; non-fatal so a template problem cannot block boot.
python -c "from openbase_coder_cli.codex_home_instructions import refresh_openbase_instruction_files_from_installation as refresh; refresh(report=print)" \
    || echo "[entrypoint] warning: instruction refresh failed" >&2

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
# Cloud workspaces report activity so Cloud (and, for Maritime, the provider)
# can tell a busy workspace from an idle one; provision installs the wrapper.
if [ "${MARITIME_MODE:-0}" = "1" ] && [ -f "$WRAPPER_DIR/openbase-cloud-heartbeat.sh" ]; then
    default_services="$default_services openbase-cloud-heartbeat"
fi
if [ -f "$WRAPPER_DIR/codex-app-server.sh" ]; then
    default_services="$default_services codex-app-server"
fi
# Openbase Sync is conditional: supervise the daemon only once it is
# configured (configuring it requires a container restart to take effect).
if [ -f "$WRAPPER_DIR/sync-daemon.sh" ] \
    && python -c "from openbase_coder_cli.sync_daemon import is_configured; import sys; sys.exit(0 if is_configured() else 1)" 2>/dev/null; then
    default_services="$default_services sync-daemon"
fi
services="${OPENBASE_CODER_SERVICES:-$default_services}"
# The staged single-use netmesh key must survive failed enrollments (egress
# can lag boot on Maritime, and the container can restart before login
# completes), so it is deleted only once the daemon reports an enrolled,
# forwarding node — see the confirmation watcher below.
netmesh_authkey=""
NETMESH_AUTHKEY_FILE="$DATA_DIR/bootstrap-netmesh-authkey"
if [ "$NETWORK_MODE" = "netmesh-tsnet" ] && [ -f "$NETMESH_AUTHKEY_FILE" ]; then
    netmesh_authkey="$(/bin/cat "$NETMESH_AUTHKEY_FILE")"
fi
netmesh_key_staged=0
if [ -n "$netmesh_authkey" ]; then
    netmesh_key_staged=1
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

if [ "$netmesh_key_staged" = "1" ]; then
    (
        while :; do
            # The daemon tries TS_AUTHKEY once at start. On Maritime, egress
            # can still be down at that moment, and a failed first login
            # parks the node in NeedsLogin for good — so while the staged key
            # is still here, resubmit it through the daemon's local login
            # API until the node reports enrolled and forwarding.
            if python -c "
import sys
from pathlib import Path
from openbase_coder_cli.services.tunneld import tunneld_health, tunneld_login
h = tunneld_health()
if h.get('backend_state') == 'Running' and h.get('forwards_up'):
    sys.exit(0)
if h.get('backend_state') == 'NeedsLogin':
    key = Path('$NETMESH_AUTHKEY_FILE').read_text().strip()
    if key and tunneld_login(key):
        print('resubmitted the staged enrollment key')
sys.exit(1)
" 2>/dev/null; then
                rm -f "$NETMESH_AUTHKEY_FILE"
                echo "enrollment confirmed; staged auth key removed"
                break
            fi
            sleep 10
        done
    ) 2>&1 | sed -u "s/^/[netmesh-enroll] /" &
fi

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

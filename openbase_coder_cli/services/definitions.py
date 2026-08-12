from __future__ import annotations

from dataclasses import dataclass

from openbase_coder_cli.backend_config import (
    CODEX_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)


@dataclass
class ServiceDefinition:
    name: str
    description: str
    command_template: str
    workdir_template: str
    install_by_default: bool = True
    port: int | None = None
    cleanup_ports: tuple[int, ...] = ()
    cleanup_command_substrings: tuple[str, ...] = ()
    # Coding backends this service applies to; None means all backends.
    backends: tuple[str, ...] | None = None
    service_type: str = "simple"
    restart_policy: str | None = "always"
    keep_alive: bool = True

    def supports_backend(self, coding_backend: str) -> bool:
        return self.backends is None or coding_backend in self.backends


SERVICES: list[ServiceDefinition] = [
    ServiceDefinition(
        name="livekit-server",
        description="LiveKit Server",
        command_template=(
            'LIVEKIT_NETWORK_MODE="${{LIVEKIT_NETWORK_MODE:-tailscale}}"\n'
            'LIVEKIT_TCP_PORT="${{LIVEKIT_TCP_PORT:-7881}}"\n'
            'LIVEKIT_UDP_PORT="${{LIVEKIT_UDP_PORT:-7882}}"\n'
            'LIVEKIT_LOOPBACK_IFACE="lo0"\n'
            'if [ "$(uname)" != "Darwin" ]; then\n'
            '    LIVEKIT_LOOPBACK_IFACE="lo"\n'
            "fi\n"
            'case "$LIVEKIT_NETWORK_MODE" in\n'
            "    local)\n"
            '        LIVEKIT_BIND_IP="${{LIVEKIT_BIND_IP:-127.0.0.1}}"\n'
            '        NODE_IP_ARGS=(--node-ip "$LIVEKIT_BIND_IP")\n'
            '        LIVEKIT_CONFIG_BODY="$(printf \'rtc:\\n  tcp_port: %s\\n  udp_port: %s\\n  enable_loopback_candidate: true\\n  interfaces:\\n    includes:\\n      - %s\\n  ips:\\n    includes:\\n      - 127.0.0.1/32\\n\' "$LIVEKIT_TCP_PORT" "$LIVEKIT_UDP_PORT" "$LIVEKIT_LOOPBACK_IFACE")"\n'
            "        ;;\n"
            "    tailscale)\n"
            '        if [ -z "${{LIVEKIT_NODE_IP:-}}" ] && command -v tailscale >/dev/null 2>&1; then\n'
            '            LIVEKIT_NODE_IP="$(tailscale ip -4 2>/dev/null | head -n 1)"\n'
            "        fi\n"
            '        if [ -z "${{LIVEKIT_NODE_IP_V6:-}}" ] && command -v tailscale >/dev/null 2>&1; then\n'
            '            LIVEKIT_NODE_IP_V6="$(tailscale ip -6 2>/dev/null | head -n 1)"\n'
            "        fi\n"
            '        if [ -n "${{LIVEKIT_NODE_IP:-}}" ] && ! [[ "$LIVEKIT_NODE_IP" =~ ^([0-9]{{1,3}}\\.){{3}}[0-9]{{1,3}}$ ]]; then\n'
            '            echo "Ignoring invalid Tailscale IPv4 value: $LIVEKIT_NODE_IP" >&2\n'
            "            LIVEKIT_NODE_IP=\n"
            "        fi\n"
            '        if [ -n "${{LIVEKIT_NODE_IP_V6:-}}" ] && ! [[ "$LIVEKIT_NODE_IP_V6" =~ ^[0-9A-Fa-f:]+$ ]]; then\n'
            '            echo "Ignoring invalid Tailscale IPv6 value: $LIVEKIT_NODE_IP_V6" >&2\n'
            "            LIVEKIT_NODE_IP_V6=\n"
            "        fi\n"
            '        if [ -z "${{LIVEKIT_NODE_IP:-}}" ]; then\n'
            '            echo "LIVEKIT_NODE_IP is required for Tailscale LiveKit signaling and media." >&2\n'
            "            exit 1\n"
            "        fi\n"
            '        if [ "$(uname)" = "Darwin" ]; then\n'
            '            if [ -z "${{LIVEKIT_INTERFACE:-}}" ]; then\n'
            '                LIVEKIT_INTERFACE="$(ifconfig 2>/dev/null | awk -v ip="$LIVEKIT_NODE_IP" \'BEGIN {{ iface = "" }} /^[a-z0-9]+:/ {{ iface = substr($1, 1, length($1) - 1) }} index($0, "inet " ip " ") {{ print iface; exit }}\')"\n'
            "            fi\n"
            '            if [ -z "$LIVEKIT_INTERFACE" ]; then\n'
            '                LIVEKIT_INTERFACE="$(route -n get "$LIVEKIT_NODE_IP" 2>/dev/null | sed -n \'s/.*interface: //p\' | head -n 1)"\n'
            "            fi\n"
            "        else\n"
            '            if [ -z "${{LIVEKIT_INTERFACE:-}}" ]; then\n'
            '                LIVEKIT_INTERFACE="$(ip -o -4 addr show 2>/dev/null | awk -v ip="$LIVEKIT_NODE_IP" \'index($4, ip "/") == 1 {{ print $2; exit }}\')"\n'
            "            fi\n"
            '            if [ -z "$LIVEKIT_INTERFACE" ]; then\n'
            '                LIVEKIT_INTERFACE="$(ip -4 route get "$LIVEKIT_NODE_IP" 2>/dev/null | sed -n \'s/.* dev \\([^ ]*\\).*/\\1/p\' | head -n 1)"\n'
            "            fi\n"
            "        fi\n"
            '        if [ -z "$LIVEKIT_INTERFACE" ]; then\n'
            '            echo "LIVEKIT_INTERFACE is required for Tailscale LiveKit media." >&2\n'
            "            exit 1\n"
            "        fi\n"
            '        LIVEKIT_BIND_IP="${{LIVEKIT_BIND_IP:-127.0.0.1}}"\n'
            '        NODE_IP_ARGS=(--node-ip "$LIVEKIT_NODE_IP")\n'
            '        LIVEKIT_CONFIG_BODY="$(printf \'rtc:\\n  tcp_port: %s\\n  udp_port: %s\\n  enable_loopback_candidate: true\\n  interfaces:\\n    includes:\\n      - %s\\n      - %s\\n  ips:\\n    includes:\\n      - 127.0.0.1/32\\n      - %s/32\\n\' "$LIVEKIT_TCP_PORT" "$LIVEKIT_UDP_PORT" "$LIVEKIT_LOOPBACK_IFACE" "$LIVEKIT_INTERFACE" "$LIVEKIT_NODE_IP")"\n'
            '        if [ -n "${{LIVEKIT_NODE_IP_V6:-}}" ]; then\n'
            '            LIVEKIT_CONFIG_BODY="$(printf \'%s\\n      - %s/128\\n\' "$LIVEKIT_CONFIG_BODY" "$LIVEKIT_NODE_IP_V6")"\n'
            "        fi\n"
            "        ;;\n"
            "    *)\n"
            '        echo "Unsupported LIVEKIT_NETWORK_MODE: $LIVEKIT_NETWORK_MODE" >&2\n'
            "        exit 1\n"
            "        ;;\n"
            "esac\n"
            'LIVEKIT_KEYS="$LIVEKIT_API_KEY: $LIVEKIT_API_SECRET"\n'
            'if [ -n "${{LIVEKIT_CLIENT_API_KEY:-}}" ] && [ -n "${{LIVEKIT_CLIENT_API_SECRET:-}}" ] && [ "$LIVEKIT_CLIENT_API_KEY" != "$LIVEKIT_API_KEY" ] && [ "$LIVEKIT_CLIENT_API_SECRET" != "$LIVEKIT_API_SECRET" ]; then\n'
            '    LIVEKIT_KEYS="$(printf \'%s\\n%s: %s\' "$LIVEKIT_KEYS" "$LIVEKIT_CLIENT_API_KEY" "$LIVEKIT_CLIENT_API_SECRET")"\n'
            "fi\n"
            'exec {livekit} --dev --bind "$LIVEKIT_BIND_IP" --config-body "$LIVEKIT_CONFIG_BODY" "${{NODE_IP_ARGS[@]}}" --keys "$LIVEKIT_KEYS"'
        ),
        workdir_template="{workspace}",
        port=7880,
        cleanup_ports=(7880, 7881),
        cleanup_command_substrings=("livekit-server",),
    ),
    ServiceDefinition(
        name="codex-app-server",
        description="Codex App Server",
        command_template=(
            'export CODEX_HOME="{data_dir}/codex_home"\n'
            'export DISABLE_AUTOUPDATER="${{DISABLE_AUTOUPDATER:-1}}"\n'
            'mkdir -p "$CODEX_HOME"\n'
            'OPENBASE_CODING_BACKEND="${{OPENBASE_CODING_BACKEND:-codex}}"\n'
            'CODEX_MODEL_REASONING_EFFORT="${{CODEX_MODEL_REASONING_EFFORT:-high}}"\n'
            'CODEX_SERVICE_TIER="${{CODEX_SERVICE_TIER:-standard}}"\n'
            'if [ "$OPENBASE_CODING_BACKEND" = "openbase_cloud_codex" ] || [ "$OPENBASE_CODING_BACKEND" = "openbase-cloud-codex" ]; then\n'
            '    if [ -z "${{OPENBASE_CLOUD_CODEX_API_KEY:-}}" ]; then\n'
            '        if ! OPENBASE_CLOUD_CODEX_API_KEY="$({openbase_coder} auth print-machine-token)"; then\n'
            '            echo "Unable to get an Openbase Cloud machine token. Run openbase-coder login, then restart services." >&2\n'
            "            exit 1\n"
            "        fi\n"
            "        export OPENBASE_CLOUD_CODEX_API_KEY\n"
            "    fi\n"
            "fi\n"
            "exec {codex} app-server "
            '-c "model_reasoning_effort=\\"$CODEX_MODEL_REASONING_EFFORT\\"" '
            '-c "service_tier=\\"$CODEX_SERVICE_TIER\\"" '
            "--listen ws://127.0.0.1:4500"
        ),
        workdir_template="{workspace}",
        port=4500,
        backends=(CODEX_BACKEND, OPENBASE_CLOUD_CODEX_BACKEND),
    ),
    ServiceDefinition(
        name="sync-workers",
        description="Sync Workers (thread, device, and code-sync reconcile)",
        # One process runs every periodic sync job on its own thread — the
        # jobs read their historical interval/max-age env overrides
        # (CODEX_THREAD_SYNC_INTERVAL etc.) from the wrapper-sourced env file
        # themselves, and state-dependent jobs (device sync, reconcile) gate
        # at runtime on code-sync enablement instead of being installed and
        # removed as companion services.
        command_template="exec {openbase_coder} sync-workers run",
        workdir_template="{data_dir}",
    ),
    ServiceDefinition(
        name="openbase-routines",
        description="Openbase Routines",
        command_template=(
            'OPENBASE_CODER_ROUTINES_INTERVAL="${{OPENBASE_CODER_ROUTINES_INTERVAL:-60}}"\n'
            'exec {openbase_coder} routines run-loop --interval "$OPENBASE_CODER_ROUTINES_INTERVAL"'
        ),
        workdir_template="{data_dir}",
    ),
    ServiceDefinition(
        name="livekit-agent",
        description="LiveKit Agent Worker",
        command_template=(
            'LIVEKIT_NETWORK_MODE="${{LIVEKIT_NETWORK_MODE:-tailscale}}"\n'
            'if [ -z "${{LIVEKIT_NODE_IP:-}}" ] && command -v tailscale >/dev/null 2>&1; then\n'
            '    LIVEKIT_NODE_IP="$(tailscale ip -4 2>/dev/null | head -n 1)"\n'
            "fi\n"
            'if [ "$LIVEKIT_NETWORK_MODE" = "tailscale" ]; then\n'
            '    export LIVEKIT_URL="${{LIVEKIT_AGENT_URL:-ws://localhost:7880}}"\n'
            'elif [ "$LIVEKIT_NETWORK_MODE" = "local" ] || [ "$LIVEKIT_NETWORK_MODE" = "lan" ]; then\n'
            '    export LIVEKIT_URL="${{LIVEKIT_URL:-ws://localhost:7880}}"\n'
            "else\n"
            '    echo "Unsupported LIVEKIT_NETWORK_MODE: $LIVEKIT_NETWORK_MODE" >&2\n'
            "    exit 1\n"
            "fi\n"
            'export LIVEKIT_AGENT_LOAD_THRESHOLD="${{LIVEKIT_AGENT_LOAD_THRESHOLD:-2.0}}"\n'
            "exec {python} -m openbase_coder_cli.livekit_agent.livekit start"
        ),
        workdir_template="{runtime_workdir}",
        cleanup_ports=(8081,),
        cleanup_command_substrings=("openbase_coder_cli.livekit_agent.livekit",),
    ),
    ServiceDefinition(
        name="django-cli",
        description="Django CLI Server",
        command_template=(
            'LIVEKIT_NETWORK_MODE="${{LIVEKIT_NETWORK_MODE:-tailscale}}"\n'
            'if [ -z "${{LIVEKIT_NODE_IP:-}}" ] && command -v tailscale >/dev/null 2>&1; then\n'
            '    LIVEKIT_NODE_IP="$(tailscale ip -4 2>/dev/null | head -n 1)"\n'
            "fi\n"
            'if [ -n "${{LIVEKIT_NODE_IP:-}}" ] && ! [[ "$LIVEKIT_NODE_IP" =~ ^([0-9]{{1,3}}\\.){{3}}[0-9]{{1,3}}$ ]]; then\n'
            '    echo "Ignoring invalid Tailscale IPv4 value: $LIVEKIT_NODE_IP" >&2\n'
            "    LIVEKIT_NODE_IP=\n"
            "fi\n"
            'if [ "$LIVEKIT_NETWORK_MODE" = "tailscale" ]; then\n'
            '    case "${{LIVEKIT_URL:-}}" in\n'
            '        ""|ws://localhost:*|ws://127.0.0.1:*|http://localhost:*|http://127.0.0.1:*)\n'
            '            if [ -z "${{LIVEKIT_NODE_IP:-}}" ]; then\n'
            '                echo "LIVEKIT_NODE_IP is required to derive LIVEKIT_URL in Tailscale mode." >&2\n'
            "                exit 1\n"
            "            fi\n"
            '            export LIVEKIT_URL="ws://${{LIVEKIT_NODE_IP}}:7880"\n'
            "            ;;\n"
            "    esac\n"
            'elif [ "$LIVEKIT_NETWORK_MODE" = "local" ]; then\n'
            '    export LIVEKIT_URL="${{LIVEKIT_URL:-ws://localhost:7880}}"\n'
            'elif [ "$LIVEKIT_NETWORK_MODE" = "lan" ]; then\n'
            '    if [ -z "${{LIVEKIT_NODE_IP:-}}" ]; then\n'
            '        LIVEKIT_NODE_IP="$(ipconfig getifaddr "${{LIVEKIT_INTERFACE:-$(route -n get default 2>/dev/null | sed -n \'s/.*interface: //p\' | head -n 1)}}" 2>/dev/null || true)"\n'
            "    fi\n"
            '    if [ -z "${{LIVEKIT_NODE_IP:-}}" ]; then\n'
            '        echo "LIVEKIT_NODE_IP is required to derive LIVEKIT_URL in LAN mode." >&2\n'
            "        exit 1\n"
            "    fi\n"
            '    export LIVEKIT_URL="${{LIVEKIT_URL:-ws://${{LIVEKIT_NODE_IP}}:7880}}"\n'
            "else\n"
            '    echo "Unsupported LIVEKIT_NETWORK_MODE: $LIVEKIT_NETWORK_MODE" >&2\n'
            "    exit 1\n"
            "fi\n"
            'OPENBASE_CODER_CLI_HOST="${{OPENBASE_CODER_CLI_HOST:-127.0.0.1}}"\n'
            'OPENBASE_CODER_CLI_PORT="${{OPENBASE_CODER_CLI_PORT:-7999}}"\n'
            'exec {openbase_coder} server --host "$OPENBASE_CODER_CLI_HOST" --port "$OPENBASE_CODER_CLI_PORT"'
        ),
        workdir_template="{data_dir}",
        port=7999,
    ),
    ServiceDefinition(
        name="code-sync",
        description="Code Sync (managed Syncthing)",
        command_template=(
            # --home sets config+data together (Syncthing v2 requires both).
            'exec {syncthing} serve --home "{data_dir}/code-sync" '
            "--no-browser --no-restart --no-upgrade"
        ),
        workdir_template="{data_dir}",
        # Installed only when code sync is enabled (openbase-coder sync
        # enable or the sync settings API); never on plain installs.
        install_by_default=False,
        cleanup_command_substrings=("syncthing",),
    ),
    ServiceDefinition(
        name="openbase-cloud-auth-rehydrate",
        description="Openbase Cloud workspace auth rehydrate",
        command_template="exec {openbase_coder} cloud rehydrate-auth",
        workdir_template="{data_dir}",
        install_by_default=False,
        service_type="oneshot",
        restart_policy="on-failure",
        keep_alive=False,
    ),
    ServiceDefinition(
        name="openbase-cloud-heartbeat",
        description="Openbase Cloud idle heartbeat",
        command_template=(
            'OPENBASE_CLOUD_HEARTBEAT_INTERVAL="${{OPENBASE_CLOUD_HEARTBEAT_INTERVAL:-60}}"\n'
            "{openbase_coder} cloud rehydrate-auth\n"
            'exec {openbase_coder} cloud heartbeat --interval "$OPENBASE_CLOUD_HEARTBEAT_INTERVAL"'
        ),
        workdir_template="{data_dir}",
        # Only meaningful on openbase-cloud workspaces; installed explicitly by
        # `openbase-coder provision`, never on normal local installs.
        install_by_default=False,
    ),
]


def default_services(coding_backend: str | None = None) -> list[ServiceDefinition]:
    """Services installed by default, optionally filtered to a coding backend."""
    services = [service for service in SERVICES if service.install_by_default]
    if coding_backend is None:
        return services
    return [service for service in services if service.supports_backend(coding_backend)]


# Services that no longer exist; installs remove any leftover units/plists so
# upgrades don't strand old processes running retired commands. Their periodic
# jobs now run inside the consolidated ``sync-workers`` service.
RETIRED_SERVICE_NAMES: tuple[str, ...] = (
    "codex-thread-sync",
    "claude-thread-sync",
    "codex-thread-device-sync",
    "claude-thread-device-sync",
)


def retired_service_stub(name: str) -> ServiceDefinition:
    """A minimal definition for a retired service, for unload/removal only."""
    return ServiceDefinition(
        name=name,
        description=f"Retired service {name}",
        command_template="",
        workdir_template="{data_dir}",
        install_by_default=False,
    )

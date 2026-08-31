"""Runner functions invoked as ``python -m openbase_coder_cli.services.runners
<name>``, one per ``services.definitions.SERVICES`` entry.

Replaces the previous per-service bash ``command_template`` bodies (OS-branchy
``ifconfig``/``ip``/``route``/``ipconfig`` shell logic) with pure Python that
runs identically on macOS, Linux, and Windows. Each ``build_*`` function is a
pure ``(env, binaries) -> (argv, env)`` transform so it's testable without
actually exec'ing anything; ``run()``/``main()`` do the real process exec.
"""

from __future__ import annotations

import ipaddress
import os
import platform
import subprocess
import sys
from pathlib import Path

from openbase_coder_cli.env_file import env_file_values
from openbase_coder_cli.paths import OPENBASE_BASE_DIR
from openbase_coder_cli.services import network
from openbase_coder_cli.services.installation import InstallationConfig

RunnerArgvEnv = tuple[list[str], dict[str, str]]


def _is_ip_version(value: str, version: int) -> bool:
    try:
        return ipaddress.ip_address(value).version == version
    except ValueError:
        return False


def _livekit_config_body(
    tcp_port: str,
    udp_port: str,
    loopback_iface: str,
    extra_ifaces: list[str],
    extra_ips: list[str],
) -> str:
    ifaces = [loopback_iface, *extra_ifaces]
    ips = ["127.0.0.1/32", *extra_ips]
    iface_lines = "\n".join(f"      - {iface}" for iface in ifaces)
    ip_lines = "\n".join(f"      - {ip}" for ip in ips)
    return (
        "rtc:\n"
        f"  tcp_port: {tcp_port}\n"
        f"  udp_port: {udp_port}\n"
        "  enable_loopback_candidate: true\n"
        "  interfaces:\n"
        "    includes:\n"
        f"{iface_lines}\n"
        "  ips:\n"
        "    includes:\n"
        f"{ip_lines}\n"
    )


def build_livekit_server(
    env: dict[str, str], binaries: dict[str, str]
) -> RunnerArgvEnv:
    mode = env.get("LIVEKIT_NETWORK_MODE", "tailscale")
    tcp_port = env.get("LIVEKIT_TCP_PORT", "7881")
    udp_port = env.get("LIVEKIT_UDP_PORT", "7882")
    loopback_iface = "lo0" if platform.system() == "Darwin" else "lo"

    if mode == "local":
        bind_ip = env.get("LIVEKIT_BIND_IP", "127.0.0.1")
        node_ip_args = ["--node-ip", bind_ip]
        config_body = _livekit_config_body(tcp_port, udp_port, loopback_iface, [], [])
    elif mode == "tailscale":
        node_ip = env.get("LIVEKIT_NODE_IP") or network.tailscale_ip("4") or ""
        node_ip_v6 = env.get("LIVEKIT_NODE_IP_V6") or network.tailscale_ip("6") or ""
        if node_ip and not _is_ip_version(node_ip, 4):
            print(f"Ignoring invalid Tailscale IPv4 value: {node_ip}", file=sys.stderr)
            node_ip = ""
        if node_ip_v6 and not _is_ip_version(node_ip_v6, 6):
            print(
                f"Ignoring invalid Tailscale IPv6 value: {node_ip_v6}",
                file=sys.stderr,
            )
            node_ip_v6 = ""
        if not node_ip:
            print(
                "LIVEKIT_NODE_IP is required for Tailscale LiveKit signaling "
                "and media.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        interface = env.get("LIVEKIT_INTERFACE") or network.resolve_interface(node_ip)
        if not interface:
            print(
                "LIVEKIT_INTERFACE is required for Tailscale LiveKit media.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        bind_ip = env.get("LIVEKIT_BIND_IP", "127.0.0.1")
        node_ip_args = ["--node-ip", node_ip]
        extra_ips = [f"{node_ip}/32"]
        if node_ip_v6:
            extra_ips.append(f"{node_ip_v6}/128")
        config_body = _livekit_config_body(
            tcp_port, udp_port, loopback_iface, [interface], extra_ips
        )
    else:
        print(f"Unsupported LIVEKIT_NETWORK_MODE: {mode}", file=sys.stderr)
        raise SystemExit(1)

    api_key = env.get("LIVEKIT_API_KEY", "")
    api_secret = env.get("LIVEKIT_API_SECRET", "")
    keys = f"{api_key}: {api_secret}"
    client_key = env.get("LIVEKIT_CLIENT_API_KEY")
    client_secret = env.get("LIVEKIT_CLIENT_API_SECRET")
    if (
        client_key
        and client_secret
        and client_key != api_key
        and client_secret != api_secret
    ):
        keys = f"{keys}\n{client_key}: {client_secret}"

    argv = [
        binaries["livekit"],
        "--dev",
        "--bind",
        bind_ip,
        "--config-body",
        config_body,
        *node_ip_args,
        "--keys",
        keys,
    ]
    return argv, env


def build_codex_app_server(
    env: dict[str, str], binaries: dict[str, str]
) -> RunnerArgvEnv:
    from openbase_coder_cli.backend_config import normalize_backend
    from openbase_coder_cli.codex_backend_config import codex_backend_cli_overrides
    from openbase_coder_cli.codex_control_plane import (
        apply_managed_codex_app_server_endpoint,
    )
    from openbase_coder_cli.paths import CODEX_HOME_DIR

    CODEX_HOME_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(env)
    # The shared ~/.codex is the default home, but pin it so a stray
    # CODEX_HOME in the service environment can't retarget the service.
    env["CODEX_HOME"] = str(CODEX_HOME_DIR)
    env, endpoint = apply_managed_codex_app_server_endpoint(env)
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    backend = env.get("OPENBASE_CODING_BACKEND", "codex")
    reasoning_effort = env.get("CODEX_MODEL_REASONING_EFFORT", "high")
    service_tier = env.get("CODEX_SERVICE_TIER", "standard")

    if backend in ("openbase_cloud_codex", "openbase-cloud-codex") and not env.get(
        "OPENBASE_CLOUD_CODEX_API_KEY"
    ):
        result = subprocess.run(
            [binaries["openbase_coder"], "auth", "print-machine-token"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(
                "Unable to get an Openbase Cloud machine token. Run "
                "openbase-coder login, then restart services.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        env["OPENBASE_CLOUD_CODEX_API_KEY"] = result.stdout.strip()

    try:
        backend_overrides = codex_backend_cli_overrides(
            normalize_backend(backend),
            web_backend_url=env.get("OPENBASE_CODER_CLI_WEB_BACKEND_URL"),
        )
    except ValueError:
        backend_overrides = []

    argv = [
        binaries["codex"],
        "app-server",
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "-c",
        f'service_tier="{service_tier}"',
        *backend_overrides,
        "--listen",
        endpoint.value,
    ]
    return argv, env


def build_sync_workers(env: dict[str, str], binaries: dict[str, str]) -> RunnerArgvEnv:
    return [binaries["openbase_coder"], "sync-workers", "run"], env


def build_openbase_routines(
    env: dict[str, str], binaries: dict[str, str]
) -> RunnerArgvEnv:
    interval = env.get("OPENBASE_CODER_ROUTINES_INTERVAL", "60")
    return (
        [binaries["openbase_coder"], "routines", "run-loop", "--interval", interval],
        env,
    )


def build_livekit_agent(env: dict[str, str], binaries: dict[str, str]) -> RunnerArgvEnv:
    mode = env.get("LIVEKIT_NETWORK_MODE", "tailscale")
    node_ip = env.get("LIVEKIT_NODE_IP") or network.tailscale_ip("4") or ""
    env = dict(env)
    if node_ip:
        env["LIVEKIT_NODE_IP"] = node_ip

    if mode == "tailscale":
        env["LIVEKIT_URL"] = env.get("LIVEKIT_AGENT_URL", "ws://localhost:7880")
    elif mode in ("local", "lan"):
        env["LIVEKIT_URL"] = env.get("LIVEKIT_URL", "ws://localhost:7880")
    else:
        print(f"Unsupported LIVEKIT_NETWORK_MODE: {mode}", file=sys.stderr)
        raise SystemExit(1)

    env.setdefault("LIVEKIT_AGENT_LOAD_THRESHOLD", "2.0")
    argv = [
        binaries["python"],
        "-m",
        "openbase_coder_cli.livekit_agent.livekit",
        "start",
    ]
    return argv, env


_LOCALHOST_URL_PREFIXES = (
    "ws://localhost:",
    "ws://127.0.0.1:",
    "http://localhost:",
    "http://127.0.0.1:",
)


def build_django_cli(env: dict[str, str], binaries: dict[str, str]) -> RunnerArgvEnv:
    mode = env.get("LIVEKIT_NETWORK_MODE", "tailscale")
    node_ip = env.get("LIVEKIT_NODE_IP") or network.tailscale_ip("4") or ""
    env = dict(env)
    if node_ip and not _is_ip_version(node_ip, 4):
        print(f"Ignoring invalid Tailscale IPv4 value: {node_ip}", file=sys.stderr)
        node_ip = ""

    existing_url = env.get("LIVEKIT_URL", "")
    if mode == "tailscale":
        if node_ip:
            env["LIVEKIT_NODE_IP"] = node_ip
        if existing_url == "" or existing_url.startswith(_LOCALHOST_URL_PREFIXES):
            if not node_ip:
                print(
                    "LIVEKIT_NODE_IP is required to derive LIVEKIT_URL in "
                    "Tailscale mode.",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            env["LIVEKIT_URL"] = f"ws://{node_ip}:7880"
    elif mode == "local":
        env["LIVEKIT_URL"] = existing_url or "ws://localhost:7880"
    elif mode == "lan":
        if not node_ip:
            node_ip = network.default_lan_ip() or ""
        if not node_ip:
            print(
                "LIVEKIT_NODE_IP is required to derive LIVEKIT_URL in LAN mode.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        env["LIVEKIT_NODE_IP"] = node_ip
        env["LIVEKIT_URL"] = existing_url or f"ws://{node_ip}:7880"
    else:
        print(f"Unsupported LIVEKIT_NETWORK_MODE: {mode}", file=sys.stderr)
        raise SystemExit(1)

    host = env.get("OPENBASE_CODER_CLI_HOST", "127.0.0.1")
    port = env.get("OPENBASE_CODER_CLI_PORT", "7999")
    argv = [binaries["openbase_coder"], "server", "--host", host, "--port", port]
    return argv, env


def build_code_sync(env: dict[str, str], binaries: dict[str, str]) -> RunnerArgvEnv:
    home = str(OPENBASE_BASE_DIR / "code-sync")
    argv = [
        binaries["syncthing"],
        "serve",
        "--home",
        home,
        "--no-browser",
        "--no-restart",
        "--no-upgrade",
    ]
    return argv, env


def build_openbase_tunneld(
    env: dict[str, str], binaries: dict[str, str]
) -> RunnerArgvEnv:
    env = dict(env)
    # The daemon's flag defaults point at Tailscale's hosted control plane;
    # the embedded transport always rides Openbase's headscale.
    env.setdefault("OPENBASE_TSNET_CONTROL_URL", "https://net.openbase.cloud")
    return [binaries["tunneld"], "serve"], env


def build_openbase_cloud_auth_rehydrate(
    env: dict[str, str], binaries: dict[str, str]
) -> RunnerArgvEnv:
    return [binaries["openbase_coder"], "cloud", "rehydrate-auth"], env


def build_openbase_cloud_heartbeat(
    env: dict[str, str], binaries: dict[str, str]
) -> RunnerArgvEnv:
    interval = env.get("OPENBASE_CLOUD_HEARTBEAT_INTERVAL", "60")
    subprocess.run([binaries["openbase_coder"], "cloud", "rehydrate-auth"], check=False)
    argv = [
        binaries["openbase_coder"],
        "cloud",
        "heartbeat",
        "--interval",
        interval,
    ]
    return argv, env


# Runner-key -> (build fn, binary keys it needs resolved). The key is what
# ``ServiceDefinition.command_template`` holds after the definitions.py
# rewrite, and what the wrapper/task passes as ``sys.argv[1]``.
RUNNERS: dict[str, tuple[callable, tuple[str, ...]]] = {
    "livekit-server": (build_livekit_server, ("livekit",)),
    "codex-app-server": (build_codex_app_server, ("codex", "openbase_coder")),
    "sync-workers": (build_sync_workers, ("openbase_coder",)),
    "openbase-routines": (build_openbase_routines, ("openbase_coder",)),
    "livekit-agent": (build_livekit_agent, ("python",)),
    "django-cli": (build_django_cli, ("openbase_coder",)),
    "code-sync": (build_code_sync, ("syncthing",)),
    "openbase-tunneld": (build_openbase_tunneld, ("tunneld",)),
    "openbase-cloud-auth-rehydrate": (
        build_openbase_cloud_auth_rehydrate,
        ("openbase_coder",),
    ),
    "openbase-cloud-heartbeat": (
        build_openbase_cloud_heartbeat,
        ("openbase_coder",),
    ),
}


def _resolve_binaries(name: str, config: InstallationConfig) -> dict[str, str]:
    from openbase_coder_cli.services.launchd import _binary_resolvers

    resolvers = _binary_resolvers(config)
    _, keys = RUNNERS[name]
    return {key: resolvers[key]() for key in keys}


def _load_env(config: InstallationConfig) -> dict[str, str]:
    """Process env, with the installation's env file layered on top.

    On macOS/Linux the bash wrapper already does ``set -a; source
    "$env_file"`` before exec'ing into the runner, so this just repeats that
    (idempotent). On Windows there is no shell wrapper at all, so this is the
    only place the env file gets applied.
    """
    env = dict(os.environ)
    if config.env_file:
        env.update(env_file_values(Path(config.env_file).expanduser()))
    from openbase_coder_cli.codex_control_plane import (
        apply_managed_codex_app_server_endpoint,
    )
    from openbase_coder_cli.paths import CODEX_HOME_DIR

    env, _endpoint = apply_managed_codex_app_server_endpoint(env)
    env["CODEX_HOME"] = str(CODEX_HOME_DIR)
    return env


def run(name: str) -> None:
    if name not in RUNNERS:
        raise SystemExit(f"Unknown service runner: {name}")
    config = (
        InstallationConfig.load()
        if InstallationConfig.exists()
        else InstallationConfig()
    )
    binaries = _resolve_binaries(name, config)
    build, _ = RUNNERS[name]
    argv, env = build(_load_env(config), binaries)
    if name == "codex-app-server":
        from openbase_coder_cli.codex_control_plane import (
            managed_codex_app_server_endpoint,
            prepare_codex_app_server_start,
        )

        prepare_codex_app_server_start(
            managed_codex_app_server_endpoint(env),
            binaries["codex"],
        )
    os.execvpe(argv[0], argv, env)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        raise SystemExit("Usage: python -m openbase_coder_cli.services.runners <name>")
    run(args[0])


if __name__ == "__main__":
    main()

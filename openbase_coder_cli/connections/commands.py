"""Shared behaviour of the ``db`` and ``storage`` command groups: leasing
through Cloud, running or detaching a local proxy, and formatting output."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from . import cloud, state

GROUP_FOR_KIND = {cloud.KIND_POSTGRES: "db", cloud.KIND_S3: "storage"}


def device_label() -> str:
    from openbase_coder_cli.backend_auth import computer_name

    try:
        return computer_name()
    except Exception:
        import socket

        return socket.gethostname()


def lease(
    kind: str, name: str, role: str, purpose: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve ``name`` and lease ``role`` credentials; raises ``ClickException``."""
    try:
        connection = cloud.find_connection(kind, name)
        if connection.get("revoked_at"):
            raise cloud.ConnectionsError(
                f"Access to '{name}' is revoked. Restore it with "
                f"'openbase-coder {GROUP_FOR_KIND[kind]} restore {name}'."
            )
        if role not in (connection.get("roles") or []):
            raise cloud.ConnectionsError(
                f"'{name}' has no {role} credentials (available: "
                f"{', '.join(connection.get('roles') or []) or 'none'})."
            )
        leased = cloud.lease_connection(
            connection["id"], role=role, purpose=purpose, device=device_label()
        )
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    state.audit(kind, name, "leased", purpose, role=role)
    return connection, leased


def resolve(kind: str, name: str) -> dict[str, Any]:
    try:
        return cloud.find_connection(kind, name)
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc


def read_secret_option(value: str | None, env_var: str, prompt: str) -> str:
    """A secret from the option, else the environment, else a hidden prompt.

    Command-line arguments show up in process listings and shell history, so
    the environment variable or the prompt is the recommended path.
    """
    if value:
        return value
    from_env = os.environ.get(env_var, "")
    if from_env:
        return from_env
    if sys.stdin.isatty():
        return click.prompt(prompt, hide_input=True)
    raise click.ClickException(
        f"Provide the secret via the {env_var} environment variable (preferred) "
        "or the corresponding option."
    )


def proxy_log_path(kind: str, name: str) -> Path:
    return state.CONNECTIONS_DIR / f"{kind}-{name}.log"


def spawn_background_proxy(kind: str, name: str, argv: list[str]) -> state.RunningProxy:
    """Start ``openbase-coder <argv>`` detached and wait for it to register."""
    existing = state.find_proxy(kind, name)
    if existing is not None:
        raise click.ClickException(
            f"A proxy for '{name}' is already running on port {existing.port} "
            f"(pid {existing.pid}); stop it with "
            f"'openbase-coder {GROUP_FOR_KIND[kind]} stop {name}'."
        )
    state.CONNECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = proxy_log_path(kind, name)
    with log_path.open("ab") as log_handle:
        os.chmod(log_path, 0o600)
        process = subprocess.Popen(
            [sys.executable, "-m", "openbase_coder_cli", *argv],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    import time

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        proxy = state.find_proxy(kind, name)
        if proxy is not None and proxy.pid == process.pid:
            return proxy
        if process.poll() is not None:
            break
        time.sleep(0.2)
    tail = ""
    try:
        tail = log_path.read_text()[-2000:]
    except OSError:
        pass
    raise click.ClickException(
        f"The background proxy did not start. Log: {log_path}\n{tail}".rstrip()
    )


def run_proxy_until_stopped(
    kind: str,
    name: str,
    role: str,
    start: Callable[[Callable[[str], None]], Any],
) -> None:
    """Run a proxy in the foreground: ``start(log)`` returns an object with
    ``port``, ``local_url``, ``wait()`` and ``close()``.

    Registers the proxy for ``list``/``stop``, logs connection events to
    stdout, and shuts down cleanly on SIGINT/SIGTERM.
    """

    def log(message: str) -> None:
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        click.echo(f"[{stamp}] {message}")
        sys.stdout.flush()

    server = start(log)
    proxy = state.register_proxy(kind, name, server.port, role, server.local_url)
    state.audit(
        kind, name, "proxy_started", server.local_url, role=role, port=server.port
    )
    click.echo(f"Proxying {kind} '{name}' ({role}) at {server.local_url}")
    click.echo("  Credentials stay inside this process; press Ctrl-C to stop.")
    sys.stdout.flush()
    stop_event = threading.Event()

    def handle_signal(_signum, _frame):
        stop_event.set()
        server.close()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handle_signal)
    try:
        server.wait()
    finally:
        state.unregister_proxy(kind, name, proxy.pid)
        state.audit(kind, name, "proxy_stopped", role=role, port=server.port)
        log("proxy stopped")


def stop_proxy_command(kind: str, name: str) -> None:
    if state.stop_proxy(kind, name):
        click.echo(f"Stopped the proxy for '{name}'.")
        return
    raise click.ClickException(f"No running proxy for '{name}' on this machine.")


def running_proxies(kind: str) -> dict[str, state.RunningProxy]:
    return {proxy.name: proxy for proxy in state.load_proxies() if proxy.kind == kind}


def print_connections(
    kind: str, connections: list[dict[str, Any]], *, as_json: bool
) -> None:
    proxies = running_proxies(kind)
    if as_json:
        payload = [
            {
                **item,
                "proxy": (
                    proxies[item["name"]].__dict__
                    if item.get("name") in proxies
                    else None
                ),
            }
            for item in connections
        ]
        click.echo(json.dumps(payload, indent=2))
        return
    if not connections:
        click.echo(
            f"No {kind} connections. Add one with 'openbase-coder {GROUP_FOR_KIND[kind]} add'."
        )
        return
    for item in connections:
        target = item.get("target") or {}
        where = (
            f"{target.get('host')}:{target.get('port')}/{target.get('database')}"
            if kind == cloud.KIND_POSTGRES
            else f"{target.get('endpoint')} bucket {target.get('bucket')}"
        )
        flags = [item.get("provider", "external"), ", ".join(item.get("roles") or [])]
        if item.get("revoked_at"):
            flags.append("REVOKED")
        proxy = proxies.get(item.get("name", ""))
        if proxy is not None:
            flags.append(f"proxy {proxy.url} ({proxy.role}, pid {proxy.pid})")
        click.echo(f"{item.get('name', ''):<20} {where} ({'; '.join(flags)})")


def print_events(events: list[dict[str, Any]], *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(events, indent=2))
        return
    if not events:
        click.echo("No events.")
        return
    for event in events:
        parts = [event.get("created_at", ""), event.get("action", "")]
        if event.get("role"):
            parts.append(event["role"])
        parts.append(event.get("actor", ""))
        if event.get("device"):
            parts.append(f"on {event['device']}")
        if event.get("purpose"):
            parts.append(f"for {event['purpose']}")
        if event.get("metadata"):
            parts.append(json.dumps(event["metadata"], sort_keys=True))
        click.echo("  ".join(str(part) for part in parts if part))


def confirm_or_yes(message: str, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise click.ClickException(f"{message} Re-run with --yes to confirm.")
    if not click.confirm(message, default=False):
        raise click.Abort()

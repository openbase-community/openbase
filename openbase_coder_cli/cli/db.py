"""``openbase-coder db``: Postgres databases reached through Openbase.

Credentials live in Openbase Cloud's vault; this machine leases them into a
process that either proxies the wire protocol on a loopback port or runs the
query itself. Agents therefore work with ``postgresql://127.0.0.1:<port>/db``
and never see a password."""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import click

from openbase_coder_cli.connections import cloud, commands, state
from openbase_coder_cli.connections.pg_client import (
    QueryOutcome,
    quote_identifier,
    quote_literal,
    run_sql,
)
from openbase_coder_cli.connections.pg_protocol import (
    SSL_MODES,
    ProtocolError,
    UpstreamError,
    UpstreamTarget,
)
from openbase_coder_cli.connections.pg_proxy import PostgresProxy

KIND = cloud.KIND_POSTGRES
URL_ENV_VAR = "OPENBASE_DATABASE_URL"
READONLY_URL_ENV_VAR = "OPENBASE_DATABASE_READONLY_URL"
DEFAULT_SCHEMA = "public"


def parse_postgres_url(url: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Split a ``postgresql://`` URL into the vault's target and credentials."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("postgres", "postgresql"):
        raise click.ClickException("The URL must start with postgresql://.")
    if not parts.hostname:
        raise click.ClickException("The URL has no host.")
    database = unquote(parts.path.lstrip("/"))
    if not database:
        raise click.ClickException(
            "The URL has no database name (postgresql://host/<db>)."
        )
    query = dict(item.split("=", 1) for item in parts.query.split("&") if "=" in item)
    ssl_mode = unquote(query.get("sslmode", "require"))
    if ssl_mode not in SSL_MODES:
        raise click.ClickException(f"Unknown sslmode '{ssl_mode}'.")
    if not parts.username:
        raise click.ClickException("The URL has no user.")
    target = {
        "host": parts.hostname,
        "port": parts.port or 5432,
        "database": database,
        "ssl_mode": ssl_mode,
    }
    credentials = {
        "user": unquote(parts.username),
        "password": unquote(parts.password or ""),
    }
    return target, credentials


def upstream_from_lease(
    leased: dict[str, Any], **startup_params: str
) -> UpstreamTarget:
    target = leased["target"]
    credentials = leased["credentials"]
    return UpstreamTarget(
        host=target["host"],
        port=int(target.get("port") or 5432),
        database=target["database"],
        user=credentials["user"],
        password=credentials["password"],
        ssl_mode=target.get("ssl_mode") or "require",
        startup_params=dict(startup_params),
    )


def _role(read_only: bool) -> str:
    return cloud.ROLE_READONLY if read_only else cloud.ROLE_READWRITE


def _readonly_role_name(name: str) -> str:
    return "openbase_ro_" + re.sub(r"[^a-z0-9_]", "_", name.lower())


@click.group()
def db() -> None:
    """Use Postgres databases through Openbase without seeing their passwords."""


@db.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def list_command(as_json: bool) -> None:
    """List the databases in this account's vault and any local proxies."""
    try:
        connections = cloud.list_connections(KIND)
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    commands.print_connections(KIND, connections, as_json=as_json)


@db.command()
@click.argument("name")
@click.option(
    "--url", help=f"postgresql:// URL with the read-write user (or set {URL_ENV_VAR})."
)
@click.option(
    "--readonly-url",
    help=f"Optional URL for an existing read-only role (or set {READONLY_URL_ENV_VAR}).",
)
def add(name: str, url: str | None, readonly_url: str | None) -> None:
    """Store an existing database's URL in the Openbase Cloud vault as NAME."""
    url = commands.read_secret_option(url, URL_ENV_VAR, "Database URL")
    target, readwrite = parse_postgres_url(url)
    credentials = {cloud.ROLE_READWRITE: readwrite}
    readonly_url = readonly_url or __import__("os").environ.get(
        READONLY_URL_ENV_VAR, ""
    )
    if readonly_url:
        readonly_target, readonly = parse_postgres_url(readonly_url)
        if (readonly_target["host"], readonly_target["database"]) != (
            target["host"],
            target["database"],
        ):
            raise click.ClickException(
                "The read-only URL must point at the same host and database."
            )
        credentials[cloud.ROLE_READONLY] = readonly
    try:
        created = cloud.add_connection(KIND, name, target, credentials)
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    state.audit(KIND, name, "added", f"{target['host']}/{target['database']}")
    click.echo(
        f"Added database '{name}' ({target['host']}:{target['port']}/{target['database']}, "
        f"roles: {', '.join(created.get('roles') or [])})."
    )
    click.echo(f"  Use it: openbase-coder db proxy {name} --port 5433")


@db.command()
@click.argument("name")
@click.option(
    "--region", default="", help="Provider region id (default: the Cloud's default)."
)
def provision(name: str, region: str) -> None:
    """Create a new small Postgres database on Openbase's provider and store it as NAME."""
    try:
        created = cloud.provision_connection(name, region=region)
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    target = created.get("target") or {}
    state.audit(
        KIND, name, "provisioned", f"{target.get('host')}/{target.get('database')}"
    )
    click.echo(
        f"Provisioned database '{name}' on {created.get('provider')} "
        f"({target.get('host')}/{target.get('database')})."
    )
    click.echo(f"  Use it: openbase-coder db proxy {name} --port 5433")


@db.command()
@click.argument("name")
@click.option(
    "--teardown",
    is_flag=True,
    help="Also delete the provider project for a database Openbase provisioned.",
)
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def remove(name: str, teardown: bool, yes: bool) -> None:
    """Forget NAME (stop any local proxy first). The database itself is kept unless --teardown."""
    connection = commands.resolve(KIND, name)
    what = (
        "and DELETE the provider database"
        if teardown
        else "(the database itself is kept)"
    )
    commands.confirm_or_yes(f"Remove '{name}' from the vault {what}?", yes)
    state.stop_proxy(KIND, name)
    try:
        cloud.remove_connection(connection["id"], teardown=teardown)
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    state.audit(KIND, name, "removed", "teardown" if teardown else "")
    click.echo(f"Removed '{name}'.")


@db.command()
@click.argument("name")
def revoke(name: str) -> None:
    """Block all further leases of NAME; running proxies close within a minute."""
    connection = commands.resolve(KIND, name)
    try:
        cloud.revoke_connection(connection["id"])
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    state.stop_proxy(KIND, name)
    state.audit(KIND, name, "revoked")
    click.echo(
        f"Revoked access to '{name}'. Restore with 'openbase-coder db restore {name}'."
    )


@db.command()
@click.argument("name")
def restore(name: str) -> None:
    """Allow leases of NAME again after a revoke."""
    connection = commands.resolve(KIND, name)
    try:
        cloud.restore_connection(connection["id"])
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    state.audit(KIND, name, "restored")
    click.echo(f"Restored access to '{name}'.")


@db.command()
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def audit(name: str, as_json: bool) -> None:
    """Show who leased, changed or revoked NAME (from Openbase Cloud)."""
    connection = commands.resolve(KIND, name)
    try:
        events = cloud.connection_events(connection["id"])
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    commands.print_events(events, as_json=as_json)


@db.command()
@click.argument("name")
@click.option(
    "--port",
    type=click.IntRange(0, 65535),
    default=0,
    help="Loopback port (0 = any free port).",
)
@click.option("--read-only", is_flag=True, help="Use the database's read-only role.")
@click.option("--purpose", default="db proxy", help="Recorded in the audit log.")
@click.option("--background", is_flag=True, help="Detach; stop later with 'db stop'.")
def proxy(
    name: str, port: int, read_only: bool, purpose: str, background: bool
) -> None:
    """Serve NAME on 127.0.0.1:PORT; clients connect with any user and no password."""
    role = _role(read_only)
    if background:
        argv = ["db", "proxy", name, "--port", str(port), "--purpose", purpose]
        if read_only:
            argv.append("--read-only")
        running = commands.spawn_background_proxy(KIND, name, argv)
        click.echo(
            f"Proxying database '{name}' ({role}) at {running.url} (pid {running.pid})"
        )
        click.echo(f"  Stop it: openbase-coder db stop {name}")
        return
    _connection, leased = commands.lease(KIND, name, role, purpose)
    connection_id = leased["id"]

    def start(log):
        server = PostgresProxy(
            port,
            upstream_from_lease(leased),
            log=log,
            still_allowed=lambda: cloud.connection_allowed(connection_id),
        )
        try:
            server.start()
        except OSError as exc:
            raise click.ClickException(
                f"Cannot listen on 127.0.0.1:{port}: {exc}"
            ) from exc
        return server

    commands.run_proxy_until_stopped(KIND, name, role, start)


@db.command()
@click.argument("name")
def stop(name: str) -> None:
    """Stop the background proxy for NAME."""
    commands.stop_proxy_command(KIND, name)


def _format_outcome(outcome: QueryOutcome, *, as_json: bool) -> str:
    if as_json:
        payload = [
            {
                "columns": result.columns,
                "rows": [
                    dict(zip(result.columns, row, strict=False)) for row in result.rows
                ],
                "status": result.status,
            }
            for result in outcome.results
        ]
        return json.dumps(payload, indent=2)
    lines: list[str] = []
    for notice in outcome.notices:
        lines.append(f"NOTICE: {notice}")
    for result in outcome.results:
        if result.columns:
            cells = [
                [("" if value is None else value) for value in row]
                for row in result.rows
            ]
            widths = [
                max([len(name), *(len(row[index]) for row in cells)])
                for index, name in enumerate(result.columns)
            ]
            lines.append(
                " | ".join(
                    name.ljust(widths[i]) for i, name in enumerate(result.columns)
                )
            )
            lines.append("-+-".join("-" * width for width in widths))
            for row in cells:
                lines.append(
                    " | ".join(value.ljust(widths[i]) for i, value in enumerate(row))
                )
            lines.append(
                f"({len(result.rows)} row{'s' if len(result.rows) != 1 else ''})"
            )
        elif result.status:
            lines.append(result.status)
    return "\n".join(lines)


@db.command()
@click.argument("name")
@click.argument("sql", required=False)
@click.option(
    "--file",
    "sql_file",
    type=click.Path(exists=True, dir_okay=False),
    help="Read SQL from a file.",
)
@click.option("--read-only", is_flag=True, help="Run as the read-only role.")
@click.option("--json", "as_json", is_flag=True, help="Rows as JSON.")
@click.option("--purpose", default="db query", help="Recorded in the audit log.")
def query(
    name: str,
    sql: str | None,
    sql_file: str | None,
    read_only: bool,
    as_json: bool,
    purpose: str,
) -> None:
    """Run SQL against NAME and print the result (text format, like psql)."""
    if sql_file:
        sql = Path(sql_file).read_text()
    elif sql is None and not sys.stdin.isatty():
        sql = sys.stdin.read()
    if not sql or not sql.strip():
        raise click.ClickException("Give SQL as an argument, with --file, or on stdin.")
    _connection, leased = commands.lease(KIND, name, _role(read_only), purpose)
    try:
        outcome = run_sql(
            upstream_from_lease(leased, application_name="openbase-coder db query"), sql
        )
    except UpstreamError as exc:
        raise click.ClickException(f"Postgres error: {exc}") from exc
    except (ProtocolError, OSError) as exc:
        raise click.ClickException(f"Could not reach the database: {exc}") from exc
    click.echo(_format_outcome(outcome, as_json=as_json))


@db.command()
@click.argument("name")
@click.option("--read-only", is_flag=True, help="Connect as the read-only role.")
@click.argument("psql_args", nargs=-1, type=click.UNPROCESSED)
def connect(name: str, read_only: bool, psql_args: tuple[str, ...]) -> None:
    """Open psql on NAME through a temporary local proxy."""
    psql = shutil.which("psql")
    if psql is None:
        raise click.ClickException(
            "psql is not installed. Use 'openbase-coder db query' or start "
            "'openbase-coder db proxy' and point your own client at it."
        )
    role = _role(read_only)
    _connection, leased = commands.lease(KIND, name, role, "db connect")
    server = PostgresProxy(0, upstream_from_lease(leased))
    server.start()
    state.audit(KIND, name, "connect", role=role, port=server.port)
    try:
        completed = subprocess.run([psql, server.local_url, *psql_args], check=False)
    finally:
        server.close()
    sys.exit(completed.returncode)


@db.group()
def readonly() -> None:
    """Manage a dedicated read-only Postgres role for NAME."""


def _run_admin_sql(name: str, statements: list[str], purpose: str) -> None:
    _connection, leased = commands.lease(KIND, name, cloud.ROLE_READWRITE, purpose)
    try:
        run_sql(
            upstream_from_lease(leased, application_name="openbase-coder db readonly"),
            "\n".join(statements),
        )
    except UpstreamError as exc:
        raise click.ClickException(f"Postgres error: {exc}") from exc
    except (ProtocolError, OSError) as exc:
        raise click.ClickException(f"Could not reach the database: {exc}") from exc


def _store_readonly(connection: dict[str, Any], role_name: str, password: str) -> None:
    try:
        cloud.set_role_credentials(
            connection["id"],
            cloud.ROLE_READONLY,
            {"user": role_name, "password": password},
        )
    except cloud.ConnectionsError as exc:
        raise click.ClickException(
            f"The role was changed in Postgres but Cloud rejected the credentials: {exc}"
        ) from exc


@readonly.command("create")
@click.argument("name")
@click.option(
    "--role-name",
    default=None,
    help="Postgres role to create (default openbase_ro_<name>).",
)
@click.option(
    "--schema",
    default=DEFAULT_SCHEMA,
    show_default=True,
    help="Schema to grant SELECT on.",
)
def readonly_create(name: str, role_name: str | None, schema: str) -> None:
    """Create a SELECT-only role in the database and store it as NAME's read-only credentials."""
    connection = commands.resolve(KIND, name)
    role_name = role_name or _readonly_role_name(name)
    password = secrets.token_urlsafe(24)
    role = quote_identifier(role_name)
    database = quote_identifier(connection["target"]["database"])
    schema_identifier = quote_identifier(schema)
    _run_admin_sql(
        name,
        [
            f"CREATE ROLE {role} LOGIN PASSWORD {quote_literal(password)};",
            f"GRANT CONNECT ON DATABASE {database} TO {role};",
            f"GRANT USAGE ON SCHEMA {schema_identifier} TO {role};",
            f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema_identifier} TO {role};",
            f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA {schema_identifier} TO {role};",
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema_identifier} GRANT SELECT ON TABLES TO {role};",
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema_identifier} GRANT SELECT ON SEQUENCES TO {role};",
        ],
        "db readonly create",
    )
    _store_readonly(connection, role_name, password)
    state.audit(KIND, name, "readonly_created", role_name)
    click.echo(
        f"Created read-only role {role_name} for '{name}'. Use --read-only on proxy/query/connect."
    )


@readonly.command("rotate")
@click.argument("name")
def readonly_rotate(name: str) -> None:
    """Give NAME's read-only role a new password (invalidates leaked copies)."""
    connection = commands.resolve(KIND, name)
    _connection, leased = commands.lease(
        KIND, name, cloud.ROLE_READONLY, "db readonly rotate"
    )
    role_name = leased["credentials"]["user"]
    password = secrets.token_urlsafe(24)
    _run_admin_sql(
        name,
        [
            f"ALTER ROLE {quote_identifier(role_name)} PASSWORD {quote_literal(password)};"
        ],
        "db readonly rotate",
    )
    _store_readonly(connection, role_name, password)
    state.audit(KIND, name, "readonly_rotated", role_name)
    click.echo(f"Rotated the password of {role_name}.")


@readonly.command("drop")
@click.argument("name")
@click.option(
    "--schema",
    default=DEFAULT_SCHEMA,
    show_default=True,
    help="Schema the role was granted SELECT on.",
)
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def readonly_drop(name: str, schema: str, yes: bool) -> None:
    """Drop NAME's read-only role from the database and forget its credentials."""
    connection = commands.resolve(KIND, name)
    _connection, leased = commands.lease(
        KIND, name, cloud.ROLE_READONLY, "db readonly drop"
    )
    role_name = leased["credentials"]["user"]
    commands.confirm_or_yes(f"Drop Postgres role {role_name}?", yes)
    role = quote_identifier(role_name)
    database = quote_identifier(connection["target"]["database"])
    schema_identifier = quote_identifier(schema)
    # The read-write role revokes its own grants; it need not be a member of
    # the read-only role (which DROP OWNED would require).
    _run_admin_sql(
        name,
        [
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema_identifier} REVOKE SELECT ON TABLES FROM {role};",
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema_identifier} REVOKE SELECT ON SEQUENCES FROM {role};",
            f"REVOKE SELECT ON ALL TABLES IN SCHEMA {schema_identifier} FROM {role};",
            f"REVOKE SELECT ON ALL SEQUENCES IN SCHEMA {schema_identifier} FROM {role};",
            f"REVOKE USAGE ON SCHEMA {schema_identifier} FROM {role};",
            f"REVOKE CONNECT ON DATABASE {database} FROM {role};",
            f"DROP ROLE {role};",
        ],
        "db readonly drop",
    )
    try:
        cloud.set_role_credentials(connection["id"], cloud.ROLE_READONLY, None)
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    state.audit(KIND, name, "readonly_dropped", role_name)
    click.echo(f"Dropped {role_name}.")

"""`openbase-coder pty`: persistent terminals for interactive CLI logins.

An agent runs a login command in a session, reads what it prints, and types
answers, deciding as it goes what the login needs (see the bundled
openbase-cli-logins skill).
"""

from __future__ import annotations

import json
import sys

import click

from openbase_coder_cli import pty_session


def _fail(exc: Exception) -> click.ClickException:
    return click.ClickException(str(exc))


@click.group()
def pty() -> None:
    """Run an interactive command in a terminal you can read and type into."""


@pty.command("start", context_settings={"ignore_unknown_options": True})
@click.argument("name")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@click.option(
    "--notify-thread",
    default=None,
    help="Queue a follow-up turn on this thread when the command ends, so you "
    "can confirm the result to the user. Defaults to the agent's own thread "
    "(SUPER_AGENTS_THREAD_ID or CODEX_THREAD_ID).",
)
@click.option("--no-notify", is_flag=True, help="Do not queue a follow-up turn.")
def start_command(
    name: str, command: tuple[str, ...], notify_thread: str | None, no_notify: bool
) -> None:
    """Start COMMAND in session NAME (put the command after --).

    Example: openbase-coder pty start gcloud --notify-thread <id> -- gcloud auth login
    """
    if no_notify:
        notify_thread = None
    elif notify_thread is None:
        notify_thread = pty_session.own_thread_id()
    try:
        status = pty_session.start(name, list(command), notify_thread=notify_thread)
    except (pty_session.PtySessionError, OSError) as exc:
        raise _fail(exc) from exc
    state = "running" if status["running"] else f"ended (exit {status['exit_code']})"
    click.echo(
        f"Session {name}: {state}. Read it with `openbase-coder pty read {name}`."
    )
    if notify_thread:
        click.echo("A follow-up turn will arrive on this thread when it ends.")


@pty.command("read")
@click.argument("name")
@click.option(
    "--wait",
    type=click.FloatRange(0, 120),
    default=2.0,
    show_default=True,
    help="Seconds to wait for new output.",
)
@click.option(
    "--all", "everything", is_flag=True, help="Show all output, not only new output."
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def read_command(name: str, wait: float, everything: bool, as_json: bool) -> None:
    """Print what session NAME printed since the last read."""
    try:
        result = pty_session.read(name, wait=wait, everything=everything)
    except pty_session.PtySessionError as exc:
        raise _fail(exc) from exc
    if as_json:
        click.echo(json.dumps(result))
        return
    if result["output"]:
        click.echo(result["output"], nl=not result["output"].endswith("\n"))
    if not result["running"]:
        reason = result.get("ended_reason") or "exited"
        click.echo(f"[session {name} ended: {reason}, exit {result['exit_code']}]")


@pty.command("send")
@click.argument("name")
@click.argument("text", required=False)
@click.option(
    "--secret",
    is_flag=True,
    help="Treat TEXT as a credential: redacted from the session output. "
    "Without TEXT, read it from stdin so it stays out of argv.",
)
@click.option("--no-enter", is_flag=True, help="Do not press Enter after the text.")
def send_command(name: str, text: str | None, secret: bool, no_enter: bool) -> None:
    """Type TEXT into session NAME (Enter is pressed unless --no-enter)."""
    if text is None:
        text = (
            sys.stdin.readline().rstrip("\r\n")
            if not sys.stdin.isatty()
            else (
                click.prompt("Text", hide_input=secret, default="", show_default=False)
            )
        )
    try:
        pty_session.send(name, text, enter=not no_enter, secret=secret)
    except (pty_session.PtySessionError, OSError) as exc:
        raise _fail(exc) from exc
    click.echo("Sent [secret]." if secret else "Sent.")


@pty.command("status")
@click.argument("name", required=False)
def status_command(name: str | None) -> None:
    """Show session NAME, or every session."""
    try:
        sessions = (
            [pty_session.session_status(name)] if name else pty_session.list_sessions()
        )
    except pty_session.PtySessionError as exc:
        raise _fail(exc) from exc
    if not sessions:
        click.echo("No pty sessions.")
    for item in sessions:
        state = (
            "running"
            if item["running"]
            else f"ended ({item.get('ended_reason') or 'exited'}, exit {item['exit_code']})"
        )
        click.echo(
            f"{item['name']:<20} {state:<28} {' '.join(item.get('command') or [])}"
        )


@pty.command("stop")
@click.argument("name")
def stop_command(name: str) -> None:
    """Stop session NAME and its command."""
    try:
        pty_session.stop(name)
    except pty_session.PtySessionError as exc:
        raise _fail(exc) from exc
    click.echo(f"Stopped {name}.")

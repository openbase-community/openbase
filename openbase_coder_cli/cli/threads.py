from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import click
from super_agents.app_server_client import CodexAppServerClient

from openbase_coder_cli.cli.threads_terminal import list_sessions, send

# Sources the Codex TUI scans when resolving `codex resume <name>`: it pages
# thread/list (archived=false, sources cli+vscode) 100 at a time and refuses
# any name match when the listing spans more than one page. Keeping this
# active interactive set within a single page is what makes resume-by-name
# usable on agent-heavy installs.
INTERACTIVE_SOURCES = ("cli", "vscode")
RESUME_LOOKUP_PAGE_SIZE = 100
THREAD_LIST_PAGE_LIMIT = 100


def _json_echo(value: dict[str, Any]) -> None:
    click.echo(json.dumps(value, indent=2, sort_keys=True))


def _run_client(coro):
    async def runner():
        client = CodexAppServerClient()
        try:
            await client.ensure_connected()
            return await coro(client)
        finally:
            await client.close()

    try:
        return asyncio.run(runner())
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from None


async def _list_active_threads(
    client: CodexAppServerClient, sources: tuple[str, ...]
) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {
            "archived": False,
            "limit": THREAD_LIST_PAGE_LIMIT,
            "sortKey": "updated_at",
            "useStateDbOnly": False,
            "modelProviders": [],
        }
        if cursor:
            params["cursor"] = cursor
        response = await client.request("thread/list", params)
        for thread in response.get("data") or []:
            if isinstance(thread, dict) and thread.get("source") in sources:
                threads.append(thread)
        cursor = response.get("nextCursor")
        if not cursor:
            return threads


@click.group()
def threads() -> None:
    """Terminal sessions and thread maintenance.

    `list` and `send` reach the Codex and Claude Code sessions open in
    terminals on this computer; `archive-stale` and `push` maintain threads.
    """


threads.add_command(list_sessions)
threads.add_command(send)


@threads.command("archive-stale")
@click.option(
    "--days",
    default=10.0,
    show_default=True,
    type=float,
    help="Archive threads not updated within this many days.",
)
@click.option(
    "--source",
    "sources",
    multiple=True,
    default=INTERACTIVE_SOURCES,
    show_default=True,
    help="Thread sources to prune. Repeatable.",
)
@click.option("--dry-run", is_flag=True, help="Report without archiving anything.")
def archive_stale(days: float, sources: tuple[str, ...], dry_run: bool) -> None:
    """Archive stale interactive threads so resume-by-name stays usable.

    `codex resume <name>` against the app server rejects every name match as
    unverifiable once the active cli/vscode thread listing spans more than one
    page (100 threads). Openbase names each dispatched agent thread, so those
    accumulate far past that limit; archiving the stale ones restores name
    resolution. Archived threads stay resumable by UUID and can be unarchived.
    """
    cutoff = time.time() - days * 86400.0

    async def run(client: CodexAppServerClient) -> dict[str, Any]:
        active = await _list_active_threads(client, sources)
        stale = [
            thread
            for thread in active
            if not thread.get("isPinned")
            and float(thread.get("updatedAt") or 0) < cutoff
        ]
        archived = 0
        errors: list[dict[str, Any]] = []
        if not dry_run:
            for thread in stale:
                try:
                    await client.request("thread/archive", {"threadId": thread["id"]})
                    archived += 1
                except (RuntimeError, ValueError) as exc:
                    errors.append({"threadId": thread["id"], "error": str(exc)})
        remaining = len(active) - (len(stale) if dry_run else archived)
        result: dict[str, Any] = {
            "active": len(active),
            "stale": len(stale),
            "archived": archived,
            "dryRun": dry_run,
            "remaining": remaining,
            "resumeByNameUsable": remaining <= RESUME_LOOKUP_PAGE_SIZE,
        }
        if not dry_run:
            from super_agents.state import prune_state_file_sessions

            result["statePrune"] = prune_state_file_sessions(client.state_file)
        if errors:
            result["errors"] = errors
        if remaining > RESUME_LOOKUP_PAGE_SIZE:
            result["warning"] = (
                f"{remaining} active interactive threads remain; codex "
                f"resume-by-name needs at most {RESUME_LOOKUP_PAGE_SIZE}. "
                "Re-run with a smaller --days."
            )
        return result

    _json_echo(_run_client(run))


PUSH_TIMEOUT_SECONDS = 300.0
PUSH_WAIT_LIMIT_SECONDS = 30 * 60.0
PUSH_WAIT_POLL_SECONDS = 10.0


def _push_api(method: str, path: str, **kwargs) -> tuple[int, dict[str, Any]]:
    from openbase_coder_cli.cli.local_server import local_server_request

    response = local_server_request(
        method,
        path,
        ok_statuses=(400, 404, 409),
        timeout=PUSH_TIMEOUT_SECONDS,
        **kwargs,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": response.text.strip() or f"HTTP {response.status_code}"}
    return response.status_code, payload if isinstance(payload, dict) else {}


def _push_failure(payload: dict[str, Any]) -> click.ClickException:
    message = str(payload.get("error") or payload.get("detail") or "Push failed.")
    if payload.get("safe_to_retry"):
        message += " (safe to retry)"
    return click.ClickException(message)


def _print_push_options(payload: dict[str, Any]) -> None:
    moved = payload.get("moved_to")
    if moved:
        click.echo(f"Moved to {moved.get('device')} ({moved.get('state')}).")
    if payload.get("blocked_reason"):
        click.echo(f"Cannot push now: {payload['blocked_reason']}")
    targets = payload.get("targets") or []
    if not targets:
        if payload.get("this_is_durable"):
            click.echo("This computer is your durable machine.")
        else:
            click.echo(
                "No durable machine is set up. Pair this computer with an "
                "always-on computer first (openbase-coder sync)."
            )
        return
    click.echo("Durable machines:")
    for target in targets:
        status = "online" if target.get("online") else "offline"
        line = f"  {target.get('name')} [{target.get('key')}] {status}"
        if target.get("reason"):
            line += f" - {target['reason']}"
        click.echo(line)


@threads.command("push")
@click.argument("thread_id")
@click.option(
    "--to",
    "to",
    metavar="DEVICE",
    help="Durable machine to push to (name or host). Defaults to the hub.",
)
@click.option(
    "-m",
    "--message",
    help="Follow-up message to send once the thread continues there.",
)
@click.option(
    "--wait",
    is_flag=True,
    help="If a turn is running, wait for it to finish (up to 30 minutes).",
)
@click.option("--list", "list_only", is_flag=True, help="Show where it can go.")
@click.option(
    "--cancel",
    is_flag=True,
    help="Make an unfinished push usable here again (asks the target first).",
)
@click.option(
    "--force",
    is_flag=True,
    help="With --cancel: do not wait for the target to confirm.",
)
@click.option(
    "--release",
    is_flag=True,
    help="Make a moved thread's copy here usable again.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the raw API result.")
def push(
    thread_id: str,
    to: str | None,
    message: str | None,
    wait: bool,
    list_only: bool,
    cancel: bool,
    force: bool,
    release: bool,
    as_json: bool,
) -> None:
    """Push a thread to a durable machine and continue it there.

    The thread pauses here, its transcript moves to the durable machine
    (the computer that stays on, such as your sync hub) and it continues
    there under the same conversation. The copy here becomes read-only.
    The thread's folder must be in a synced folder.
    """
    import uuid
    from urllib.parse import quote

    base = f"/api/threads/{quote(thread_id, safe='')}/push/"
    if sum(bool(flag) for flag in (list_only, cancel, release)) > 1:
        raise click.UsageError("Use only one of --list, --cancel and --release.")
    if force and not cancel:
        raise click.UsageError("--force only applies to --cancel.")

    if list_only:
        status_code, payload = _push_api("GET", base)
        if status_code != 200:
            raise _push_failure(payload)
        if as_json:
            _json_echo(payload)
        else:
            _print_push_options(payload)
        return
    if cancel or release:
        path = f"{base}cancel/" if cancel else f"{base}release/"
        body = {"force": force} if cancel else {}
        status_code, payload = _push_api("POST", path, json=body)
        if status_code != 200:
            raise _push_failure(payload)
        if as_json:
            _json_echo(payload)
        elif payload.get("state") == "moved":
            moved = payload.get("moved_to") or {}
            click.echo(
                f"The push had finished: the thread is on {moved.get('device')}."
            )
        else:
            click.echo("The thread is usable on this computer again.")
        return

    body: dict[str, Any] = {"request_id": str(uuid.uuid4())}
    if to:
        body["to"] = to
    if message:
        body["message"] = message
    deadline = time.monotonic() + PUSH_WAIT_LIMIT_SECONDS
    announced = False
    while True:
        status_code, payload = _push_api("POST", base, json=body)
        if status_code == 200:
            break
        if (
            wait
            and payload.get("code") == "thread_busy"
            and time.monotonic() < deadline
        ):
            if not announced:
                click.echo(f"{payload.get('error')} Waiting...", err=True)
                announced = True
            time.sleep(PUSH_WAIT_POLL_SECONDS)
            continue
        raise _push_failure(payload)
    if as_json:
        _json_echo(payload)
        return
    moved = payload.get("moved_to") or {}
    click.echo(
        f"Pushed to {moved.get('device')}. It continues there as thread "
        f"{moved.get('thread_id')}; the copy here is read-only."
    )
    if payload.get("turn_started"):
        click.echo("Your message was sent there.")
    elif payload.get("turn_error"):
        click.echo(
            f"The thread moved, but the message was not sent: {payload['turn_error']}",
            err=True,
        )

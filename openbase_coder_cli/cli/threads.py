from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import click
from super_agents.app_server_client import CodexAppServerClient

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
    """Maintain Codex app-server threads."""


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

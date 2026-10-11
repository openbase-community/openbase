from __future__ import annotations

import json
from typing import Any

import click

from openbase_coder_cli.problems_service import (
    capture_problem,
    list_problem_records,
    resolve_problem_record,
    write_problem_record,
)


def _json_echo(value: Any) -> None:
    click.echo(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _preview(text: str, max_length: int = 200) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 1]}…"


@click.group()
def report() -> None:
    """Report and review problematic interactions for diagnostics."""


@report.command("issue")
@click.option(
    "--thread-id",
    help="Thread to capture. Defaults to the most recently active thread.",
)
@click.option(
    "--note",
    "-m",
    help="Optional description of what went wrong.",
)
@click.option(
    "--reported-by",
    help="Identifier of the agent/user filing the report.",
)
@click.option("--json", "json_output", is_flag=True, help="Print JSON.")
def report_issue(
    thread_id: str | None,
    note: str | None,
    reported_by: str | None,
    json_output: bool,
) -> None:
    """Record the most recent user interaction of a thread as a problem.

    Captures the latest user message, the surrounding turn context, and the
    thread's metadata into the local problems datastore so the interaction can
    be reviewed later.
    """
    try:
        record = capture_problem(
            thread_id=thread_id,
            note=note,
            reported_by=reported_by,
        )
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc

    path = write_problem_record(record)

    if json_output:
        _json_echo({"id": record.id, "path": str(path), "record": record.to_json()})
        return

    click.echo(f"Recorded problem {record.id}")
    click.echo(f"  thread: {record.thread.thread_id}")
    if record.thread.label:
        click.echo(f"  label: {record.thread.label}")
    click.echo(f"  captured via: {record.user_message.source}")
    click.echo(f"  user message: {_preview(record.user_message.text)}")
    click.echo(f"  saved to: {path}")


@report.command("list")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "json_output", is_flag=True, help="Print JSON.")
def report_list(limit: int, json_output: bool) -> None:
    """List recorded problems, newest first."""
    records = list_problem_records()
    limited = records[: max(limit, 0)]

    if json_output:
        _json_echo({"items": limited, "count": len(records)})
        return

    if not limited:
        click.echo("No problems recorded.")
        return

    for record in limited:
        thread = record.get("thread") or {}
        message = record.get("user_message") or {}
        label = thread.get("label")
        label_suffix = f" · {label}" if label else ""
        click.echo(
            f"{record.get('id')}\n"
            f"  {record.get('created_at')} · thread {thread.get('thread_id')}"
            f"{label_suffix}\n"
            f"  {_preview(message.get('text') or '')}"
        )


@report.command("show")
@click.argument("identifier")
@click.option("--json", "json_output", is_flag=True, help="Print JSON.")
def report_show(identifier: str, json_output: bool) -> None:
    """Show a recorded problem by id or file path."""
    try:
        record = resolve_problem_record(identifier)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    if json_output:
        _json_echo(record)
        return

    thread = record.get("thread") or {}
    message = record.get("user_message") or {}
    click.echo(f"id: {record.get('id')}")
    click.echo(f"created_at: {record.get('created_at')}")
    click.echo(f"thread_id: {thread.get('thread_id')}")
    if thread.get("label"):
        click.echo(f"label: {thread.get('label')}")
    if thread.get("agent_name"):
        click.echo(f"agent_name: {thread.get('agent_name')}")
    if thread.get("cwd"):
        click.echo(f"cwd: {thread.get('cwd')}")
    if record.get("reported_by"):
        click.echo(f"reported_by: {record.get('reported_by')}")
    if record.get("note"):
        click.echo(f"note: {record.get('note')}")
    click.echo(f"captured_via: {message.get('source')}")
    click.echo("user_message:")
    click.echo(message.get("text") or "")

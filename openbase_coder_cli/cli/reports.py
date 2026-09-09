from __future__ import annotations

import json
import platform
import subprocess
import webbrowser
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import click

from openbase_coder_cli.reports_service import (
    ReportQuery,
    list_report_items,
    parse_report_date_range,
    read_report_item,
    resolve_report_item,
)


def _json_echo(value: Any) -> None:
    click.echo(json.dumps(value, indent=2, sort_keys=True))


def _format_date(value: float) -> str:
    return date.fromtimestamp(value).isoformat()


def _query(
    *,
    project: str | None,
    repo: str | None,
    tag: str | None,
    when: str | None,
    since: str | None,
    until: str | None,
) -> ReportQuery:
    date_from, date_to = parse_report_date_range(when)
    since_date = parse_report_date_range(since)[0] if since else None
    until_date = parse_report_date_range(until)[1] if until else None
    return ReportQuery(
        project=project,
        repo=repo,
        tag=tag,
        date_from=since_date or date_from,
        date_to=until_date or date_to,
    )


def _summarize(content: str, *, max_lines: int = 24) -> str:
    lines = [line.rstrip() for line in content.splitlines()]
    selected: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or not selected:
            selected.append(line)
        if len(selected) >= max_lines:
            break
    if not selected:
        return ""
    return "\n".join(selected)


@click.group()
def reports() -> None:
    """List, filter, show, and read Openbase Coder reports."""


@reports.command("list")
@click.option("--project", help="Filter by project path or project name substring.")
@click.option("--repo", help="Filter by exact repository directory name.")
@click.option("--tag", help="Filter by report tag label.")
@click.option(
    "--when",
    help="Filter by modified date: today, yesterday, Nd, YYYY-MM-DD, or A..B.",
)
@click.option("--since", help="Filter reports modified on or after this date/range.")
@click.option("--until", help="Filter reports modified on or before this date/range.")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "json_output", is_flag=True, help="Print JSON.")
def list_reports(
    project: str | None,
    repo: str | None,
    tag: str | None,
    when: str | None,
    since: str | None,
    until: str | None,
    limit: int,
    json_output: bool,
) -> None:
    """List reports across known project .reports folders."""
    try:
        items = list_report_items(
            _query(
                project=project,
                repo=repo,
                tag=tag,
                when=when,
                since=since,
                until=until,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    limited = items[: max(limit, 0)]
    if json_output:
        _json_echo(
            {"items": limited, "count": len(items), "date_basis": "modified_time"}
        )
        return

    if not limited:
        click.echo("No reports found.")
        return

    for item in limited:
        file_payload = item["file"]
        project_path = item["project"]["path"]
        tags = ", ".join(file_payload.get("tags") or [])
        tag_suffix = f" [{tags}]" if tags else ""
        filename_date = file_payload.get("filename_date")
        filename_note = f" filename-date={filename_date}" if filename_date else ""
        click.echo(
            f"{item['id']}\n"
            f"  {_format_date(item['updated_at'])} modified"
            f"{filename_note} · {file_payload['kind']} · {file_payload['size']} bytes"
            f"{tag_suffix}\n"
            f"  project: {project_path}"
        )
    click.echo(
        "Date filters use filesystem modified time; filename dates are metadata only."
    )


@reports.command("show")
@click.argument("identifier")
@click.option("--json", "json_output", is_flag=True, help="Print JSON.")
def show_report(identifier: str, json_output: bool) -> None:
    """Show metadata for a report id, relative path, or absolute path."""
    try:
        item = resolve_report_item(identifier)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if json_output:
        _json_echo(item)
        return
    file_payload = item["file"]
    absolute_path = (
        Path(item["project"]["path"]) / ".reports" / str(file_payload["path"])
    )
    click.echo(f"id: {item['id']}")
    click.echo(f"path: {absolute_path}")
    click.echo(f"project: {item['project']['path']}")
    click.echo(f"modified: {_format_date(item['updated_at'])}")
    if file_payload.get("filename_date"):
        click.echo(f"filename_date: {file_payload['filename_date']}")
    click.echo(f"date_basis: {file_payload.get('date_basis', 'modified_time')}")
    click.echo(f"kind: {file_payload['kind']}")
    click.echo(f"size: {file_payload['size']}")
    click.echo(f"tags: {', '.join(file_payload.get('tags') or [])}")


@reports.command("read")
@click.argument("identifier")
@click.option("--summary", "summary", is_flag=True, help="Print a short text summary.")
@click.option("--json", "json_output", is_flag=True, help="Print JSON.")
def read_report(identifier: str, summary: bool, json_output: bool) -> None:
    """Read a markdown or text report by id, relative path, or absolute path."""
    try:
        payload = read_report_item(identifier)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    content = payload.get("content")
    if json_output:
        _json_echo(payload)
        return
    if not isinstance(content, str):
        raise click.ClickException(str(payload.get("error", "Report is not readable.")))
    click.echo(_summarize(content) if summary else content)


def _open_deep_link(url: str) -> None:
    """Hand a custom-scheme URL to the OS so LaunchServices routes it to the
    Openbase desktop app."""
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["open", url], check=False)
    elif system == "Windows":
        subprocess.run(["cmd", "/c", "start", "", url], check=False)
    elif not webbrowser.open(url):
        subprocess.run(["xdg-open", url], check=False)


@reports.command("open")
@click.argument("identifier")
def open_report(identifier: str) -> None:
    """Open a report in the Openbase desktop app via its openbase:// deep link."""
    try:
        item = resolve_report_item(identifier)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    query = urlencode(
        {
            "intent": "report",
            "source": "cli-reports",
            "project": item["project"]["path"],
            "report": item["file"]["path"],
        }
    )
    _open_deep_link(f"openbase://open?{query}")
    click.echo("Opened the report in the Openbase desktop app.")

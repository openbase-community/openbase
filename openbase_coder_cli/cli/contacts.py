"""`openbase-coder contacts`: categories for personal-messaging contacts.

Agents and the messaging skills use these commands to see how the user
categorized a WhatsApp or iMessage contact before acting on it. Output is
metadata only; message text never passes through here.
"""

from __future__ import annotations

import json

import click

from openbase_coder_cli.cli.local_server import local_server_request


@click.group()
def contacts() -> None:
    """Categories for WhatsApp and iMessage contacts."""


@contacts.command("list")
@click.option(
    "--pending", is_flag=True, help="Only contacts still waiting for a category."
)
@click.option("--refresh", is_flag=True, help="Re-read the messaging sources first.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def list_contacts(pending: bool, refresh: bool, as_json: bool) -> None:
    """List people with messages in the last year and their categories."""
    query = "?refresh=1" if refresh else ""
    data = local_server_request("GET", f"/api/messaging-contacts/{query}").json()
    people = data.get("people") or []
    if pending:
        people = [row for row in people if not row.get("category")]
    if as_json:
        click.echo(json.dumps({**data, "people": people}, indent=2))
        return
    progress = data.get("progress") or {}
    click.echo(
        f"{progress.get('categorized', 0)} of {progress.get('total', 0)} contacts categorized"
        f" ({progress.get('pending', 0)} pending)."
    )
    for row in people:
        category = row.get("category") or {}
        label = category.get("label") or "uncategorized"
        kinds = ",".join(row.get("kinds") or [])
        click.echo(f"{row.get('key')}\t{label}\t{kinds}")


@contacts.command("categories")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def list_categories(as_json: bool) -> None:
    """Show the categories and the agent-access policy of each."""
    data = local_server_request("GET", "/api/messaging-contacts/categories/").json()
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    for category in data.get("categories") or []:
        click.echo(
            f"{category.get('id')}\t{category.get('label')}\t{category.get('policy')}"
        )


@contacts.command("lookup")
@click.argument("kind", type=click.Choice(["whatsapp", "imessage"]))
@click.argument("contact_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def lookup(kind: str, contact_id: str, as_json: bool) -> None:
    """Show the category of one source contact (a WhatsApp JID or iMessage handle)."""
    response = local_server_request(
        "GET",
        f"/api/messaging-contacts/lookup/?kind={kind}&contact_id={_query(contact_id)}",
        ok_statuses=(404,),
    )
    data = response.json()
    if response.status_code == 404:
        raise click.ClickException(str(data.get("error") or "contact not found"))
    row = data.get("contact") or {}
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    category = row.get("category") or {}
    click.echo(f"category: {category.get('label') or 'uncategorized'}")
    click.echo(f"policy: {category.get('policy') or 'ask'}")


@contacts.command("set")
@click.argument("keys", nargs=-1, required=True)
@click.option("--category", required=True, help="Category id, or 'none' to clear.")
def set_category(keys: tuple[str, ...], category: str) -> None:
    """Assign a category to one or more person keys."""
    payload = {"keys": list(keys), "category": None if category == "none" else category}
    data = local_server_request(
        "POST", "/api/messaging-contacts/assign/", json=payload
    ).json()
    progress = data.get("progress") or {}
    click.echo(
        f"Updated {len(keys)} contact(s); {progress.get('pending', 0)} still pending."
    )


def _query(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")

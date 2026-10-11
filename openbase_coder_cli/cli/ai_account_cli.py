"""`openbase-coder ai-account`: which AI account this computer's agents use."""

from __future__ import annotations

import click

from openbase_coder_cli import ai_account


@click.group("ai-account")
def ai_account_group() -> None:
    """Show or switch between Openbase Cloud and a linked Codex or Claude Code account."""


@ai_account_group.command("status")
def status_command() -> None:
    """Show the account in use and which accounts are linked."""
    payload = ai_account.status()
    for option in payload["options"]:
        marks = []
        if option["selected"]:
            marks.append("in use")
        if option["id"] != ai_account.OPENBASE_CLOUD:
            marks.append("linked" if option["linked"] else "not linked")
        click.echo(f"{option['label']:<18} {', '.join(marks)}")


@ai_account_group.command("select")
@click.argument("account", type=click.Choice(ai_account.CHOICES))
def select_command(account: str) -> None:
    """Make ACCOUNT the one new agent work uses (restarts the dispatcher)."""
    try:
        changed = ai_account.select(account)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    label = ai_account.LABELS[account]
    click.echo(f"Now using {label}." if changed else f"Already using {label}.")


@ai_account_group.command("unlink")
@click.argument("account", type=click.Choice(ai_account.PROVIDERS))
def unlink_command(account: str) -> None:
    """Sign out of ACCOUNT; Openbase Cloud is used if it was in use."""
    ai_account.unlink(account)
    click.echo(f"Unlinked {ai_account.LABELS[account]}.")

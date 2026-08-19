"""Local-only management commands for one-action voice lockdown."""

from __future__ import annotations

import base64
import json
import secrets

import click

from openbase_coder_cli.services.restart import RestartRequest, schedule_restart
from openbase_coder_cli.voice_lockdown.broker import (
    LockdownDeniedError,
    get_voice_lockdown_broker,
    require_safe_baseline,
)
from openbase_coder_cli.voice_lockdown.keychain import KeychainRecord
from openbase_coder_cli.voice_lockdown.policy import check_managed_mcp_registration
from openbase_coder_cli.voice_lockdown.verifier import (
    derive_verifier,
    validate_new_phrase,
    verify_phrase,
)


def _new_phrase() -> str:
    value = click.prompt("New safe phrase", hide_input=True, confirmation_prompt=True)
    try:
        return validate_new_phrase(value)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _require_record_and_phrase():
    broker = get_voice_lockdown_broker()
    _health, record = broker.health()
    if record is None:
        raise click.ClickException("Voice lockdown has not been configured safely.")
    phrase = click.prompt("Safe phrase", hide_input=True)
    if not verify_phrase(phrase, salt_b64=record.salt, verifier_b64=record.verifier):
        raise click.ClickException("Safe phrase did not match.")
    return broker, record


def _schedule_security_restart() -> None:
    try:
        schedule_restart(RestartRequest(recreate_dispatcher=True, delay_seconds=1.0), warn=False)
    except Exception as exc:
        raise click.ClickException(
            "Lockdown state changed, but managed services could not be restarted. "
            "Execution remains fail closed; run 'openbase-coder restart --recreate-dispatcher'."
        ) from exc


@click.group("lockdown")
def lockdown() -> None:
    """Manage one-action voice safe-phrase authorization."""


@lockdown.command("setup")
def setup_lockdown() -> None:
    """Create the Keychain-backed verifier without enabling lockdown."""
    if not click.get_text_stream("stdin").isatty():
        raise click.ClickException("Lockdown setup requires an interactive local terminal.")
    broker = get_voice_lockdown_broker()
    health, existing = broker.health()
    if existing is not None or health == "indeterminate":
        raise click.ClickException("Lockdown is already configured or needs repair; use rotate/status.")
    phrase = _new_phrase()
    salt = secrets.token_bytes(16)
    record = KeychainRecord(
        enabled=False,
        salt=base64.b64encode(salt).decode("ascii"),
        verifier=derive_verifier(phrase, salt),
        audit_key=base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
    )
    broker.set_configuration(record, event_type="configured")
    click.echo("Voice lockdown was configured in macOS Keychain and remains disabled.")


@lockdown.command("enable")
def enable_lockdown() -> None:
    """Enable fail-closed, one-approval safe-phrase authorization."""
    broker, record = _require_record_and_phrase()
    try:
        check = require_safe_baseline()
    except LockdownDeniedError as exc:
        raise click.ClickException(f"Refusing to enable lockdown because the coding baseline is unsafe: {exc}") from exc
    managed = check_managed_mcp_registration()
    if not managed.ready:
        raise click.ClickException(
            "Refusing to enable lockdown until Openbase-managed MCP registrations are refreshed: "
            + "; ".join(managed.reasons)
        )
    broker.revoke_all(reason="enable")
    broker.set_configuration(
        KeychainRecord(True, record.salt, record.verifier, record.audit_key),
        event_type="enabled",
    )
    _schedule_security_restart()
    click.echo(
        "Voice lockdown enabled for one pending approval at a time. "
        f"Safe baseline verified for: {', '.join(check.configured_backends)}."
    )


@lockdown.command("disable")
def disable_lockdown() -> None:
    """Disable lockdown after local phrase verification."""
    broker, record = _require_record_and_phrase()
    broker.revoke_all(reason="disable")
    broker.set_configuration(
        KeychainRecord(False, record.salt, record.verifier, record.audit_key),
        event_type="disabled",
    )
    _schedule_security_restart()
    click.echo("Voice lockdown disabled; all outstanding capabilities were revoked.")


@lockdown.command("rotate")
def rotate_lockdown() -> None:
    """Replace the phrase without ever returning it through an API."""
    broker, record = _require_record_and_phrase()
    phrase = _new_phrase()
    salt = secrets.token_bytes(16)
    broker.revoke_all(reason="phrase_rotation")
    broker.set_configuration(
        KeychainRecord(
            record.enabled,
            base64.b64encode(salt).decode("ascii"),
            derive_verifier(phrase, salt),
            record.audit_key,
        ),
        event_type="phrase_rotated",
    )
    _schedule_security_restart()
    click.echo("Voice-lockdown phrase rotated; all outstanding capabilities were revoked.")


@lockdown.command("status")
def lockdown_status() -> None:
    """Show non-secret lockdown state."""
    click.echo(json.dumps(get_voice_lockdown_broker().status(), indent=2, sort_keys=True))


@lockdown.command("audit")
@click.option("--limit", type=click.IntRange(1, 200), default=50, show_default=True)
def lockdown_audit(limit: int) -> None:
    """Show bounded, redacted lockdown audit events."""
    click.echo(json.dumps(get_voice_lockdown_broker().recent_audit(limit=limit), indent=2, sort_keys=True))

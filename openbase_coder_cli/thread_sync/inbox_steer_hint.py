"""User-facing hint when Openbase steers a terminal-started Claude session.

A steer delivered into a Claude Code session's inbox socket reaches the model
between tool calls — but only if that session accepts external messages. A
session running with permissions bypassed *holds* an unattested external
message until the user approves it in that terminal or sets
``crossSessionInbound`` to ``accept``. That policy cannot be confirmed from
outside and Openbase deliberately does not set it silently, so when a steer is
delivered this way we surface a one-time notification telling the user the
setting exists and what to do if a steer does not seem to land.

The notification rides the existing ``thread`` feed kind (no new client-side
handling) and is deduped per thread so it informs once rather than nagging.
"""

from __future__ import annotations

from openbase_coder_cli.openbase_coder_cli_app import notification_store
from openbase_coder_cli.openbase_coder_cli_app.notification_store import KIND_THREAD

_HINT_TITLE = "Steering your terminal Claude Code session"
_HINT_BODY = (
    "Openbase sent your steer to a Claude Code session running in a terminal. "
    "It is read between tool calls while that session works. If the session is "
    "running with permissions bypassed, Claude Code holds messages from other "
    'sessions until you approve them there — set "crossSessionInbound" to '
    '"accept" in your Claude settings to let Openbase steers land automatically.'
)


def hint_entity_id(thread_id: str) -> str:
    return f"steer-inbox-hint:{thread_id}"


def notify_inbox_steer_hint(
    thread_id: str,
    *,
    project_path: str | None = None,
) -> None:
    """Fire the crossSessionInbound hint once for a thread. Best-effort."""
    try:
        notification_store.upsert_notification(
            KIND_THREAD,
            hint_entity_id(thread_id),
            title=_HINT_TITLE,
            body=_HINT_BODY,
            thread_id=thread_id,
            project_path=project_path,
            # Fire once per thread; do not reopen after the user has seen and
            # dismissed it.
            reopen_if_read=False,
        )
    except Exception:  # noqa: BLE001 - a hint must never break steering
        pass

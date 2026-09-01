"""The thread websocket must surface backend error text verbatim.

The iOS app renders the ``error`` event's ``message`` unchanged in its alert,
so descriptive server-side denials (e.g. the Openbase Cloud LLM proxy's
"spend limit reached … app.openbase.cloud" 403 detail) reach users only if
this consumer passes them through rather than genericizing.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from asgiref.testing import ApplicationCommunicator  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app.consumers import (  # noqa: E402
    ThreadConsumer,
    _friendly_error,
)

CLOUD_LIMIT_DETAIL = (
    "Monthly Openbase model proxy spend limit reached. Model requests are "
    "blocked until next month. Subscribe at app.openbase.cloud to raise "
    "your monthly limits."
)


def test_friendly_error_unwraps_codex_json_rpc_message():
    exc = RuntimeError(json.dumps({"code": -32000, "message": CLOUD_LIMIT_DETAIL}))

    assert _friendly_error(exc) == CLOUD_LIMIT_DETAIL


def test_friendly_error_passes_plain_text_through():
    exc = RuntimeError(CLOUD_LIMIT_DETAIL)

    assert _friendly_error(exc) == CLOUD_LIMIT_DETAIL


def test_friendly_error_keeps_json_without_message_field_raw():
    raw = json.dumps({"code": -32000})

    assert _friendly_error(RuntimeError(raw)) == raw


def test_friendly_error_hides_unreadable_rollout_path():
    exc = RuntimeError(
        json.dumps(
            {
                "message": (
                    "failed to read thread: failed to read session metadata "
                    "private-rollout-location"
                )
            }
        )
    )

    message = _friendly_error(exc)

    assert "current Codex version" in message
    assert "private-rollout-location" not in message


async def _connected_communicator(manager):
    # channels.testing pulls in daphne, which is not a dependency here, so
    # drive the consumer with the underlying asgiref communicator directly.
    scope = {
        "type": "websocket",
        "path": "/ws/threads/t1/",
        "headers": [],
        "user": "authenticated",
        "url_route": {"kwargs": {"thread_id": "t1"}},
    }
    communicator = ApplicationCommunicator(ThreadConsumer.as_asgi(), scope)
    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=5)
    assert accepted["type"] == "websocket.accept"
    # get_thread_state -> None sends no initial state frame.
    return communicator


async def _send_json(communicator, payload):
    await communicator.send_input(
        {"type": "websocket.receive", "text": json.dumps(payload)}
    )


async def _receive_json(communicator):
    frame = await communicator.receive_output(timeout=5)
    assert frame["type"] == "websocket.send"
    return json.loads(frame["text"])


async def test_start_turn_error_surfaces_cloud_limit_detail():
    manager = AsyncMock()
    manager.get_thread_state.return_value = None
    manager.start_turn.side_effect = RuntimeError(
        json.dumps({"message": CLOUD_LIMIT_DETAIL})
    )

    with patch(
        "openbase_coder_cli.openbase_coder_cli_app.consumers.get_session_manager",
        return_value=manager,
    ):
        communicator = await _connected_communicator(manager)
        await _send_json(communicator, {"action": "start_turn", "prompt": "hi"})
        event = await _receive_json(communicator)

    assert event["type"] == "error"
    assert event["data"]["message"] == CLOUD_LIMIT_DETAIL
    assert "app.openbase.cloud" in event["data"]["message"]
    await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
    await communicator.wait(timeout=5)


async def test_queue_turn_error_surfaces_plain_runtime_error():
    manager = AsyncMock()
    manager.get_thread_state.return_value = None
    manager.queue_turn.side_effect = RuntimeError(CLOUD_LIMIT_DETAIL)

    with patch(
        "openbase_coder_cli.openbase_coder_cli_app.consumers.get_session_manager",
        return_value=manager,
    ):
        communicator = await _connected_communicator(manager)
        await _send_json(communicator, {"action": "queue_turn", "prompt": "hi"})
        event = await _receive_json(communicator)

    assert event["type"] == "error"
    assert event["data"]["message"] == CLOUD_LIMIT_DETAIL
    await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
    await communicator.wait(timeout=5)

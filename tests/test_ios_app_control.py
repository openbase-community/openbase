from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    ios_app_control as views,
)
from openbase_coder_cli.openbase_coder_cli_app.consumers import (  # noqa: E402
    IOSAppControlConsumer,
)


class FakeChannelLayer:
    def __init__(self, ack: bool = False) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.groups: dict[str, set[str]] = {}
        self.ack = ack

    async def new_channel(self) -> str:
        return "specific.test!channel"

    async def group_add(self, group: str, channel: str) -> None:
        self.groups.setdefault(group, set()).add(channel)

    async def group_discard(self, group: str, channel: str) -> None:
        self.groups.get(group, set()).discard(channel)

    async def group_send(self, group: str, event: dict) -> None:
        self.sent.append((group, event))

    async def receive(self, channel: str) -> dict:
        if self.ack:
            return {"type": "ios_app_control_ack"}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.fixture(autouse=True)
def fast_ack_timeout(monkeypatch):
    monkeypatch.setattr(views, "IOS_APP_CONTROL_ACK_TIMEOUT_SECONDS", 0.01)


def _request(payload: dict):
    request = APIRequestFactory().post(
        "/api/user/ios-app-control/",
        payload,
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def test_ios_app_control_open_url_broadcasts(monkeypatch):
    channel_layer = FakeChannelLayer()
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)

    response = views.ios_app_control(
        _request({"action": "open_url", "url": "openbase://threads/123"})
    )

    assert response.status_code == 202
    assert response.data["status"] == "published"
    assert response.data["delivered"] is False
    assert channel_layer.sent[0][0] == "ios_app_control"
    assert channel_layer.sent[0][1]["type"] == "ios_app_control"
    assert channel_layer.sent[0][1]["data"]["action"] == "open_url"
    assert channel_layer.sent[0][1]["data"]["url"] == "openbase://threads/123"
    assert channel_layer.sent[0][1]["data"]["command_id"].startswith("ios-app-control-")


def test_ios_app_control_mute_broadcasts(monkeypatch):
    channel_layer = FakeChannelLayer()
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)

    response = views.ios_app_control(
        _request({"action": "set_call_muted", "muted": True})
    )

    assert response.status_code == 202
    assert channel_layer.sent[0][1]["data"]["muted"] is True


def test_ios_app_control_start_livekit_voice_test_call_broadcasts(monkeypatch):
    channel_layer = FakeChannelLayer()
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)

    response = views.ios_app_control(
        _request({"action": "start_livekit_voice_test_call"})
    )

    assert response.status_code == 202
    assert channel_layer.sent[0][1]["data"]["action"] == (
        "start_livekit_voice_test_call"
    )


def test_ios_app_control_start_developer_call_broadcasts(monkeypatch):
    channel_layer = FakeChannelLayer()
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)

    response = views.ios_app_control(_request({"action": "start_developer_call"}))

    assert response.status_code == 202
    assert channel_layer.sent[0][1]["data"]["action"] == "start_developer_call"


def test_ios_app_control_upload_diagnostics_broadcasts(monkeypatch):
    channel_layer = FakeChannelLayer()
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)

    response = views.ios_app_control(
        _request({"action": "upload_diagnostics", "limit": 500})
    )

    assert response.status_code == 202
    assert channel_layer.sent[0][1]["data"]["action"] == "upload_diagnostics"
    assert channel_layer.sent[0][1]["data"]["limit"] == 500


@pytest.mark.parametrize("url", ["example.com", "javascript:alert(1)", "file:///tmp/a"])
def test_ios_app_control_rejects_invalid_urls(url):
    response = views.ios_app_control(_request({"action": "open_url", "url": url}))

    assert response.status_code == 400


@pytest.mark.parametrize("limit", [0, 2001])
def test_ios_app_control_rejects_invalid_upload_diagnostics_limit(limit):
    response = views.ios_app_control(
        _request({"action": "upload_diagnostics", "limit": limit})
    )

    assert response.status_code == 400


def test_ios_app_control_reports_delivered_on_device_ack(monkeypatch):
    channel_layer = FakeChannelLayer(ack=True)
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)

    response = views.ios_app_control(
        _request({"action": "open_url", "url": "openbase://threads/123"})
    )

    assert response.status_code == 202
    assert response.data["status"] == "delivered"
    assert response.data["delivered"] is True
    command_id = response.data["command_id"]
    # The ack subscription must be joined (then cleaned up) on the
    # per-command group.
    ack_group = views.ack_group_name(command_id)
    assert ack_group in channel_layer.groups
    assert channel_layer.groups[ack_group] == set()


def test_consumer_forwards_device_ack_to_command_group():
    consumer = IOSAppControlConsumer()
    consumer.channel_layer = FakeChannelLayer()

    asyncio.run(
        consumer.receive_json(
            {"type": "ios_app_control_ack", "command_id": "ios-app-control-abc123"}
        )
    )

    assert consumer.channel_layer.sent == [
        (
            "ios_app_control_ack.ios-app-control-abc123",
            {
                "type": "ios_app_control_ack",
                "command_id": "ios-app-control-abc123",
            },
        )
    ]


@pytest.mark.parametrize(
    "content",
    [
        {"type": "other"},
        {"type": "ios_app_control_ack"},
        {"type": "ios_app_control_ack", "command_id": ""},
        {"type": "ios_app_control_ack", "command_id": "bad id!"},
        {"type": "ios_app_control_ack", "command_id": "x" * 65},
        {"type": "ios_app_control_ack", "command_id": 42},
    ],
)
def test_consumer_ignores_invalid_acks(content):
    consumer = IOSAppControlConsumer()
    consumer.channel_layer = FakeChannelLayer()

    asyncio.run(consumer.receive_json(content))

    assert consumer.channel_layer.sent == []

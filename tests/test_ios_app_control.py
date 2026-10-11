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
            return (
                self.ack
                if isinstance(self.ack, dict)
                else {"type": "ios_app_control_ack"}
            )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.fixture(autouse=True)
def fast_ack_timeout(monkeypatch):
    monkeypatch.setattr(views, "IOS_APP_CONTROL_ACK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(views, "IOS_CALL_CONTROL_ACK_TIMEOUT_SECONDS", 0.01)


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


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "set_speaker"},
        {"action": "set_speaker", "speaker": "invalid"},
        {"action": "start_call"},
        {"action": "start_call", "thread_id": "  "},
        {"action": "start_call", "thread_id": "x" * 257},
    ],
)
def test_call_commands_validate_arguments(payload):
    assert views.ios_app_control(_request(payload)).status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "set_speaker", "speaker": True},
        {"action": "set_speaker", "speaker": False},
        {"action": "end_call"},
        {"action": "start_call", "thread_id": "dispatcher"},
        {"action": "start_call", "thread_id": "thread-123"},
    ],
)
@pytest.mark.parametrize("applied", [True, False])
def test_call_command_returns_result_and_state(monkeypatch, payload, applied):
    state = {"connected": True, "muted": False, "speaker": True, "active": True}
    ack = {"applied": applied, "call_state": state}
    if not applied:
        ack["error"] = "Audio route failed"
    layer = FakeChannelLayer(ack=ack)
    monkeypatch.setattr(views, "get_channel_layer", lambda: layer)
    response = views.ios_app_control(_request(payload))
    assert response.data["delivered"] is True
    assert response.data["applied"] is applied
    assert response.data["call_state"] == state
    for key, value in payload.items():
        assert layer.sent[0][1]["data"][key] == value
    if not applied:
        assert response.data["error"] == ack["error"]


@pytest.mark.parametrize("ack", [True, False])
def test_call_command_never_claims_success_without_result(monkeypatch, ack):
    monkeypatch.setattr(views, "get_channel_layer", lambda: FakeChannelLayer(ack=ack))
    response = views.ios_app_control(_request({"action": "end_call"}))
    assert response.data["applied"] is False
    assert "call_state" not in response.data


def test_consumer_preserves_valid_result_and_rejects_malformed_state():
    consumer = IOSAppControlConsumer()
    consumer.channel_layer = FakeChannelLayer()
    state = {"connected": False, "muted": True, "speaker": False, "active": False}
    content = {
        "type": "ios_app_control_ack",
        "command_id": "abc",
        "applied": False,
        "call_state": state,
        "error": "failed",
    }
    asyncio.run(consumer.receive_json(content))
    assert consumer.channel_layer.sent[-1][1] == content
    asyncio.run(
        consumer.receive_json(
            {**content, "call_state": {**state, "connected": "false"}}
        )
    )
    assert "applied" not in consumer.channel_layer.sent[-1][1]


FORWARD = {"port": 1455, "target": "100.64.0.12", "ttl_seconds": 600, "token": "t" * 24}


def test_ios_app_control_open_url_carries_loopback_forward(monkeypatch):
    channel_layer = FakeChannelLayer()
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)

    response = views.ios_app_control(
        _request(
            {
                "action": "open_url",
                "url": "https://a.example/",
                "loopback_forward": FORWARD,
            }
        )
    )

    assert response.status_code == 202
    assert channel_layer.sent[0][1]["data"]["loopback_forward"] == FORWARD


@pytest.mark.parametrize(
    "forward",
    [
        {**FORWARD, "port": 80},
        {**FORWARD, "port": 70000},
        {**FORWARD, "ttl_seconds": 0},
        {**FORWARD, "ttl_seconds": 7200},
        {**FORWARD, "token": "short"},
        {**FORWARD, "target": "bad host/with/path"},
        {**FORWARD, "target": "example.com"},
        {**FORWARD, "target": "workspace.net.obs.so"},
        {**FORWARD, "target": "127.0.0.1"},
        {**FORWARD, "target": "192.168.1.1"},
        {**FORWARD, "target": "8.8.8.8"},
        {**FORWARD, "target": "::1"},
        {"port": 1455},
    ],
)
def test_ios_app_control_rejects_bad_loopback_forwards(monkeypatch, forward):
    channel_layer = FakeChannelLayer()
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)

    response = views.ios_app_control(
        _request(
            {
                "action": "open_url",
                "url": "https://a.example/",
                "loopback_forward": forward,
            }
        )
    )

    assert response.status_code == 400
    assert channel_layer.sent == []


def test_ios_app_control_rejects_loopback_forward_for_other_actions(monkeypatch):
    channel_layer = FakeChannelLayer()
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)

    response = views.ios_app_control(
        _request(
            {"action": "set_call_muted", "muted": True, "loopback_forward": FORWARD}
        )
    )

    assert response.status_code == 400


def test_ios_app_control_reports_opened_from_device_ack(monkeypatch):
    channel_layer = FakeChannelLayer(
        ack={
            "type": "ios_app_control_ack",
            "opened": False,
            "notified": True,
            "error": "app not active",
        }
    )
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)

    response = views.ios_app_control(
        _request({"action": "open_url", "url": "https://a.example/"})
    )

    assert response.status_code == 202
    assert response.data["delivered"] is True
    assert response.data["opened"] is False
    assert response.data["notified"] is True
    assert response.data["error"] == "app not active"


def test_consumer_forwards_opened_in_ack():
    consumer = IOSAppControlConsumer()
    consumer.channel_layer = FakeChannelLayer()

    asyncio.run(
        consumer.receive_json(
            {
                "type": "ios_app_control_ack",
                "command_id": "cmd-1",
                "opened": True,
                "notified": False,
            }
        )
    )

    group, message = consumer.channel_layer.sent[-1]
    assert group == views.ack_group_name("cmd-1")
    assert message["opened"] is True
    assert message["notified"] is False


def test_consumer_and_view_surface_the_forward_outcome(monkeypatch):
    consumer = IOSAppControlConsumer()
    consumer.channel_layer = FakeChannelLayer()
    asyncio.run(
        consumer.receive_json(
            {
                "type": "ios_app_control_ack",
                "command_id": "cmd-2",
                "opened": True,
                "forward": "vpn_down",
                "forward_error": "the Openbase VPN is not connected on the phone",
                "ignored": "x",
            }
        )
    )
    _group, message = consumer.channel_layer.sent[-1]
    assert message["forward"] == "vpn_down"
    assert message["forward_error"].startswith("the Openbase VPN")
    assert "ignored" not in message

    channel_layer = FakeChannelLayer(
        ack={"type": "ios_app_control_ack", "opened": True, "forward": "started"}
    )
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)
    response = views.ios_app_control(
        _request(
            {
                "action": "open_url",
                "url": "https://a.example/",
                "loopback_forward": FORWARD,
            }
        )
    )
    assert response.status_code == 202
    assert response.data["forward"] == "started"
    assert "forward_error" not in response.data


@pytest.mark.parametrize(
    "fields, expected",
    [
        (
            {"forward": "x" * 100, "forward_error": "y" * 2000},
            {"forward": "x" * 32, "forward_error": "y" * 1024},
        ),
        ({"forward": "future_status"}, {"forward": "future_status"}),
        ({"forward": "failed", "forward_error": []}, {"forward": "failed"}),
        ({"forward": [], "forward_error": "ignored"}, {}),
        ({"forward": None, "forward_error": "ignored"}, {}),
        ({"forward_error": "ignored"}, {}),
    ],
)
def test_forward_receipt_validation_survives_the_api_round_trip(
    monkeypatch, fields, expected
):
    consumer = IOSAppControlConsumer()
    consumer.channel_layer = FakeChannelLayer()
    asyncio.run(
        consumer.receive_json(
            {
                "type": "ios_app_control_ack",
                "command_id": "cmd-bounded",
                "opened": True,
                **fields,
            }
        )
    )
    _group, message = consumer.channel_layer.sent[-1]
    channel_layer = FakeChannelLayer(ack=message)
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)
    response = views.ios_app_control(
        _request(
            {
                "action": "open_url",
                "url": "https://a.example/",
                "loopback_forward": FORWARD,
            }
        )
    )
    assert response.status_code == 202
    assert {
        key: response.data[key]
        for key in ("forward", "forward_error")
        if key in response.data
    } == expected


def test_authenticated_callback_capability_survives_serialization():
    from openbase_coder_cli.openbase_coder_cli_app.ios_app_control import (
        LoopbackForwardSerializer,
    )

    payload = {**FORWARD, "token": "OBR1_49152_2000000000_" + "a" * 32}
    serializer = LoopbackForwardSerializer(data=payload)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == payload


def test_copy_text_reaches_the_phone_and_reports_copied(monkeypatch):
    channel_layer = FakeChannelLayer(
        ack={"type": "ios_app_control_ack", "copied": True, "opened": True}
    )
    monkeypatch.setattr(views, "get_channel_layer", lambda: channel_layer)
    response = views.ios_app_control(
        _request(
            {
                "action": "copy_text",
                "text": "ABCD-1234",
                "label": "GitHub code",
                "url": "https://github.com/login/device",
            }
        )
    )
    assert response.status_code == 202
    assert response.data["copied"] is True and response.data["opened"] is True
    assert "ABCD-1234" not in str(response.data)
    sent = channel_layer.sent[0][1]["data"]
    assert sent["text"] == "ABCD-1234" and sent["label"] == "GitHub code"


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "copy_text"},
        {"action": "copy_text", "text": "two\nlines"},
        {"action": "copy_text", "text": "x" * 257},
        {"action": "copy_text", "text": "x", "url": "javascript:alert(1)"},
        {"action": "open_url", "url": "https://example.com", "text": "x"},
    ],
)
def test_copy_text_validation(monkeypatch, payload):
    monkeypatch.setattr(views, "get_channel_layer", lambda: FakeChannelLayer())
    assert views.ios_app_control(_request(payload)).status_code == 400


def test_phone_copy_reads_stdin_and_reports(monkeypatch):
    import importlib

    from click.testing import CliRunner

    user_cli = importlib.import_module("openbase_coder_cli.cli.user")
    sent = []
    monkeypatch.setattr(
        user_cli,
        "_publish_ios_app_control",
        lambda payload: sent.append(payload) or {"delivered": True, "copied": True},
    )
    result = CliRunner().invoke(
        user_cli.user,
        ["phone", "copy", "--text-stdin", "--label", "GitHub code"],
        input="ABCD-1234\n",
    )
    assert result.exit_code == 0, result.output
    assert "ABCD-1234" not in result.output
    assert sent == [
        {"action": "copy_text", "text": "ABCD-1234", "label": "GitHub code"}
    ]

    monkeypatch.setattr(
        user_cli, "_publish_ios_app_control", lambda payload: {"delivered": False}
    )
    result = CliRunner().invoke(
        user_cli.user, ["phone", "copy", "--text-stdin"], input="ABCD-1234\n"
    )
    assert result.exit_code != 0
    assert "tell the user the code" in result.output


def test_show_text_reports_shown_or_notified(monkeypatch):
    for ack, key in (
        ({"shown": True}, "shown"),
        ({"shown": False, "notified": True}, "notified"),
    ):
        layer = FakeChannelLayer(ack={"type": "ios_app_control_ack", **ack})
        monkeypatch.setattr(views, "get_channel_layer", lambda layer=layer: layer)
        response = views.ios_app_control(
            _request({"action": "show_text", "text": "x" * 300, "label": "Code"})
        )
        assert response.status_code == 202
        assert response.data[key] is True
    monkeypatch.setattr(views, "get_channel_layer", lambda: FakeChannelLayer())
    too_long = views.ios_app_control(
        _request({"action": "show_text", "text": "x" * 513})
    )
    assert too_long.status_code == 400
    copy_long = views.ios_app_control(
        _request({"action": "copy_text", "text": "x" * 300})
    )
    assert copy_long.status_code == 400


def test_phone_show_text_cli(monkeypatch):
    import importlib

    from click.testing import CliRunner

    user_cli = importlib.import_module("openbase_coder_cli.cli.user")
    sent = []
    monkeypatch.setattr(
        user_cli,
        "_publish_ios_app_control",
        lambda payload: (
            sent.append(payload)
            or {"delivered": True, "notified": True, "shown": False}
        ),
    )
    result = CliRunner().invoke(
        user_cli.user,
        [
            "phone",
            "show-text",
            "--text-stdin",
            "--open",
            "https://github.com/login/device",
        ],
        input="ABCD-1234\n",
    )
    assert result.exit_code == 0, result.output
    assert "notification" in result.output and "ABCD-1234" not in result.output
    assert sent[0]["action"] == "show_text" and sent[0]["url"].startswith(
        "https://github.com"
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ({"shown": True}, {"shown": True}),
        ({"shown": False, "notified": True}, {"shown": False, "notified": True}),
        ({"copied": True, "opened": True}, {"copied": True, "opened": True}),
        (
            {"copied": False, "error": "text is missing"},
            {"copied": False, "error": "text is missing"},
        ),
        ({"shown": "yes", "text": "ABCD-1234"}, {}),
    ],
)
def test_consumer_forwards_copy_and_show_outcomes(content, expected):
    """The consumer whitelists ack fields; copy/show outcomes must pass, the text never."""
    consumer = IOSAppControlConsumer()
    consumer.channel_layer = FakeChannelLayer()
    asyncio.run(
        consumer.receive_json(
            {
                "type": "ios_app_control_ack",
                "command_id": "ios-app-control-x1",
                **content,
            }
        )
    )
    forwarded = consumer.channel_layer.sent[0][1]
    assert forwarded == {
        "type": "ios_app_control_ack",
        "command_id": "ios-app-control-x1",
        **expected,
    }
    assert "text" not in forwarded

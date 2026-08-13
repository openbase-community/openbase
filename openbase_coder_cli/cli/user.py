from __future__ import annotations

from pathlib import Path

import click

from openbase_coder_cli.cli.local_server import local_server_request
from openbase_coder_cli.livekit_announcer import (
    MAX_ANNOUNCER_TEXT_LENGTH,
    SUPPORTED_AUDIO_EXTENSIONS,
)
from openbase_coder_cli.paths import OPENBASE_SOUNDS_DIR
from openbase_coder_cli.skill_approvals import request_approval


@click.group()
def user() -> None:
    """Commands for the active Openbase Coder user session."""


@user.command()
@click.argument("words", nargs=-1, metavar="AGENT_NAME MESSAGE")
@click.option(
    "--room",
    "room_name",
    default="",
    help="Explicit LiveKit room name. Defaults to the latest active voice room.",
)
def say(
    words: tuple[str, ...],
    room_name: str,
) -> None:
    """Speak an announcer message in the active voice session."""
    if len(words) < 2:
        raise click.ClickException(
            "Agent name is required. Usage: openbase-coder user say AGENT_NAME MESSAGE"
        )
    agent_name, *message = words
    normalized_agent_name = " ".join(agent_name.split())
    if not normalized_agent_name:
        raise click.ClickException("Agent name is required and cannot be blank.")
    text = " ".join(message).strip()
    if not text:
        raise click.ClickException("Message text is required.")
    if len(text) > MAX_ANNOUNCER_TEXT_LENGTH:
        raise click.ClickException(
            f"Message text must be {MAX_ANNOUNCER_TEXT_LENGTH} characters or fewer."
        )

    payload: dict[str, str] = {"agent_name": normalized_agent_name, "text": text}
    if room_name.strip():
        payload["room_name"] = room_name.strip()

    response = local_server_request("POST", "/api/user/say/", json=payload)

    data = response.json()
    target_room = data.get("room_name") or "active room"
    click.echo(f"Announcer message sent to {target_room}.")


@user.command()
@click.argument("sound_or_path")
@click.option(
    "--room",
    "room_name",
    default="",
    help="Explicit LiveKit room name. Defaults to the latest active voice room.",
)
def play(sound_or_path: str, room_name: str) -> None:
    """Play a local audio file in the active voice session."""
    try:
        audio_path = resolve_sound_path(sound_or_path)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None

    payload: dict[str, str] = {"audio_path": str(audio_path)}
    if room_name.strip():
        payload["room_name"] = room_name.strip()

    response = local_server_request("POST", "/api/user/play/", json=payload)

    data = response.json()
    target_room = data.get("room_name") or "active room"
    click.echo(f"Audio playback sent to {target_room}.")


@user.group("approval")
def approval() -> None:
    """Request and answer user approvals."""


@approval.command("request")
@click.option("--skill", required=True, help="Skill name requesting approval.")
@click.option("--action", required=True, help="Action that needs approval.")
@click.option("--description", required=True, help="Human-readable approval prompt.")
@click.option("--command", "command_text", default="", help="Command being gated.")
@click.option(
    "--detail",
    "detail_items",
    multiple=True,
    help="Additional context as KEY=VALUE. May be repeated.",
)
@click.option("--timeout", "timeout_seconds", default=300.0, show_default=True)
@click.option("--poll-interval", default=1.0, show_default=True)
def approval_request(
    skill: str,
    action: str,
    description: str,
    command_text: str,
    detail_items: tuple[str, ...],
    timeout_seconds: float,
    poll_interval: float,
) -> None:
    """Ask the user to approve a skill action and wait for the answer."""
    details = _parse_approval_details(detail_items)
    decision = request_approval(
        skill=skill,
        action=action,
        description=description,
        details=details,
        command=command_text.strip() or None,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval,
    )
    decision_value = decision.get("decision")
    if decision.get("accepted"):
        click.echo("Approval accepted.")
        return
    if decision_value == "timeout":
        raise click.ClickException("Approval timed out.")
    raise click.ClickException(f"Approval {decision_value}.")


def _parse_approval_details(detail_items: tuple[str, ...]) -> dict[str, str]:
    details: dict[str, str] = {}
    for item in detail_items:
        if "=" not in item:
            raise click.ClickException("--detail values must use KEY=VALUE.")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise click.ClickException("--detail keys cannot be blank.")
        details[key] = value.strip()
    return details


def resolve_sound_path(sound_or_path: str) -> Path:
    value = sound_or_path.strip()
    if not value:
        raise ValueError("Sound name or path is required.")

    candidate = Path(value).expanduser()
    if candidate.is_file():
        return _validate_audio_file(candidate.resolve())

    if candidate.is_absolute() or _looks_like_path(value):
        raise ValueError(f"Audio file not found: {value}")

    if not _is_safe_sound_name(value):
        raise ValueError(
            "Named sounds must be simple file names without path separators."
        )

    sounds_dir = OPENBASE_SOUNDS_DIR.expanduser()
    name_path = Path(value)
    candidates = (
        [sounds_dir / name_path.name]
        if name_path.suffix
        else [
            sounds_dir / f"{name_path.name}{extension}"
            for extension in SUPPORTED_AUDIO_EXTENSIONS
        ]
    )
    for sound_path in candidates:
        if sound_path.is_file():
            return _validate_audio_file(sound_path.resolve())

    tried = ", ".join(path.name for path in candidates)
    raise ValueError(f"Named sound not found in {sounds_dir}: {value}. Tried: {tried}")


def _validate_audio_file(path: Path) -> Path:
    if path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
        supported = ", ".join(SUPPORTED_AUDIO_EXTENSIONS)
        raise ValueError(
            f"Unsupported audio file extension {path.suffix!r}. Supported: {supported}."
        )
    return path


def _looks_like_path(value: str) -> bool:
    return "/" in value or "\\" in value or value.startswith(".")


def _is_safe_sound_name(value: str) -> bool:
    path = Path(value)
    return path.name == value and value not in {".", ".."} and ".." not in path.parts


@user.command("voice-route")
def voice_route() -> None:
    """Show the active LiveKit voice route."""
    response = local_server_request("GET", "/api/livekit-voice-route/")
    data = response.json()
    click.echo(f"Active route: {data.get('active_route') or 'dispatcher'}")
    dispatcher_thread_id = data.get("dispatcher_thread_id")
    if dispatcher_thread_id:
        click.echo(f"Dispatcher thread: {dispatcher_thread_id}")
    active_target_thread_id = data.get("active_target_thread_id")
    if active_target_thread_id:
        click.echo(f"Active target thread: {active_target_thread_id}")
    if not data.get("instruction_override_supported"):
        click.echo(f"Target transfer blocked: {data.get('blocked_reason')}")


@user.group("ios")
def ios() -> None:
    """Control the foreground Openbase iOS app."""


@ios.command("open-url")
@click.argument("url")
def ios_open_url(url: str) -> None:
    """Ask the foreground iOS app to open a URL or deep link."""
    data = _publish_ios_app_control({"action": "open_url", "url": url})
    _report_ios_command_result(data, "open-url")


@ios.command("mute")
def ios_mute() -> None:
    """Mute the active iOS voice call."""
    data = _publish_ios_app_control({"action": "set_call_muted", "muted": True})
    _report_ios_command_result(data, "mute")


@ios.command("unmute")
def ios_unmute() -> None:
    """Unmute the active iOS voice call."""
    data = _publish_ios_app_control({"action": "set_call_muted", "muted": False})
    _report_ios_command_result(data, "unmute")


@ios.command("start-livekit-voice-test")
def ios_start_livekit_voice_test() -> None:
    """Switch iOS to the debug LiveKit tab and start its configured call."""
    data = _publish_ios_app_control({"action": "start_livekit_voice_test_call"})
    _report_ios_command_result(data, "LiveKit voice test")


@ios.command("start-developer-call")
def ios_start_developer_call() -> None:
    """Switch iOS to the normal call tab and start the developer call."""
    data = _publish_ios_app_control({"action": "start_developer_call"})
    _report_ios_command_result(data, "developer call")


@ios.command("upload-logs")
@click.option(
    "--limit",
    type=click.IntRange(1, 2000),
    default=None,
    help="Maximum number of recent buffered iOS log entries to upload.",
)
def ios_upload_logs(limit: int | None) -> None:
    """Ask the foreground iOS app to upload its recent diagnostics logs."""
    payload: dict[str, object] = {"action": "upload_diagnostics"}
    if limit is not None:
        payload["limit"] = limit
    data = _publish_ios_app_control(payload)
    _report_ios_command_result(data, "diagnostics upload")


def _publish_ios_app_control(payload: dict[str, object]) -> dict:
    response = local_server_request("POST", "/api/user/ios-app-control/", json=payload)
    return response.json()


def _report_ios_command_result(data: dict, label: str) -> None:
    command_id = data.get("command_id")
    if data.get("delivered"):
        click.echo(f"iOS {label} command delivered: {command_id}")
        return
    click.echo(f"iOS {label} command published (unconfirmed): {command_id}")
    raise click.ClickException(
        "No iOS app confirmed receipt. The Openbase app is likely closed, "
        "backgrounded, or signed out; bring it to the foreground and retry."
    )


ios.add_command(ios_open_url, "open-link")
ios.add_command(ios_start_livekit_voice_test, "debug-livekit-call")
ios.add_command(ios_start_developer_call, "developer-call")


@user.command("exit-to-dispatch")
@click.option(
    "--room",
    "room_name",
    default="",
    help="Explicit LiveKit room name. Defaults to the latest active voice room.",
)
def exit_to_dispatch(room_name: str) -> None:
    """Route the active voice session back to the dispatcher."""
    payload: dict[str, str] = {}
    if room_name.strip():
        payload["room_name"] = room_name.strip()
    response = local_server_request(
        "POST", "/api/livekit-voice-route/exit/", json=payload
    )
    data = response.json()
    target_room = data.get("room_name") or "active room"
    click.echo(f"Voice route returned to dispatcher in {target_room}.")


@user.command("transfer-to-thread")
@click.argument("thread_id")
@click.option(
    "--room",
    "room_name",
    default="",
    help="Explicit LiveKit room name. Defaults to the latest active voice room.",
)
@click.option("--label", default="", help="Optional display label for the target.")
@click.option(
    "--agent-name",
    default="",
    help="Optional agent name used to choose the target voice.",
)
def transfer_to_thread(
    thread_id: str, room_name: str, label: str, agent_name: str
) -> None:
    """Route the active voice session to a Codex thread if instruction-safe."""
    payload: dict[str, str] = {"thread_id": thread_id}
    if room_name.strip():
        payload["room_name"] = room_name.strip()
    if label.strip():
        payload["label"] = label.strip()
    if agent_name.strip():
        payload["agent_name"] = agent_name.strip()
    response = local_server_request(
        "POST", "/api/livekit-voice-route/transfer/", json=payload
    )
    data = response.json()
    target_room = data.get("room_name") or "active room"
    click.echo(f"Voice route transferred to {thread_id} in {target_room}.")


@user.command("transfer-to-agent")
@click.argument("agent_name")
@click.option(
    "--room",
    "room_name",
    default="",
    help="Explicit LiveKit room name. Defaults to the latest active voice room.",
)
def transfer_to_agent(agent_name: str, room_name: str) -> None:
    """Route the active voice session to a named Super Agent."""
    payload: dict[str, str] = {"agent_name": agent_name}
    if room_name.strip():
        payload["room_name"] = room_name.strip()
    response = local_server_request(
        "POST", "/api/livekit-voice-route/transfer/", json=payload
    )
    data = response.json()
    target_room = data.get("room_name") or "active room"
    active_target = data.get("state", {}).get("active_target_thread_id") or agent_name
    click.echo(f"Voice route transferred to {active_target} in {target_room}.")

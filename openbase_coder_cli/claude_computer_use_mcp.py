"""Stdio MCP server exposing Openbase computer use to Claude Code sessions.

Claude Code's built-in `computer-use` server is interactive-only, so headless
Agent SDK sessions (the Claude backend's dispatcher and Super Agents threads)
get desktop control from this server instead. Every tool call proxies to the
desktop app's control server, which resolves fresh from
`~/.openbase/desktop-control.json` on each call so the shim survives desktop
app restarts (the port and secret rotate per launch); without the desktop
app it falls back to the screen-share companion's own IPC, so developer
installs get computer use too. Safety is enforced in the companion: actions
are refused unless screen sharing is active, so the user always sees what
the agent does.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from openbase_coder_cli.paths import DESKTOP_CONTROL_JSON_PATH

JsonObject = dict[str, Any]

SERVER_NAME = "openbase-computer-use"
INSTRUCTIONS = (
    "Control the user's visible desktop through Openbase Coder. Computer use "
    "is intentionally visible: actions are refused unless the user's screen "
    "is being shared, and the user can stop at any time. When an action is "
    "refused because sharing is inactive, start the share yourself and retry "
    "(macOS: `openbase-coder desktop screen-share start`; Linux: "
    "`openbase-coder computer-use screen-share start`) — do not ask the user "
    "to start it; the live share is what keeps them in control. Take a "
    "screenshot before acting; click coordinates map 1:1 to the most recent "
    "screenshot. Foreground the target app with open_application before "
    "interacting with it."
)

DESKTOP_NOT_RUNNING_ERROR = (
    "No desktop control endpoint is reachable — computer use needs screen "
    "sharing to be active. Ask the user to start screen sharing (from the "
    "Openbase desktop app, the phone, or `openbase-coder screen-share "
    "start`), then retry."
)

_XY_SCHEMA = {
    "type": "object",
    "properties": {
        "x": {"type": "integer", "description": "X coordinate in screenshot pixels."},
        "y": {"type": "integer", "description": "Y coordinate in screenshot pixels."},
    },
    "required": ["x", "y"],
    "additionalProperties": False,
}

_ACTION_PROPERTIES = {
    "action": {
        "type": "string",
        "enum": [
            "left_click",
            "double_click",
            "right_click",
            "mouse_move",
            "type",
            "key",
            "scroll",
        ],
    },
    "x": {"type": "integer"},
    "y": {"type": "integer"},
    "text": {"type": "string"},
    "combo": {"type": "string"},
    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
    "amount": {"type": "integer"},
}

TOOL_DEFINITIONS: tuple[tuple[str, str, JsonObject], ...] = (
    (
        "screenshot",
        "Capture the shared screen. Returns a PNG; later click coordinates map "
        "1:1 to this image's pixels.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ("left_click", "Left click at screenshot coordinates.", _XY_SCHEMA),
    ("double_click", "Double click at screenshot coordinates.", _XY_SCHEMA),
    ("right_click", "Right click at screenshot coordinates.", _XY_SCHEMA),
    ("mouse_move", "Move the cursor to screenshot coordinates.", _XY_SCHEMA),
    (
        "type",
        "Type text into the focused control.",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    ),
    (
        "key",
        "Press a key or chord, e.g. 'return', 'cmd+n', 'cmd+shift+t'. Use cmd "
        "(not ctrl) for macOS shortcuts.",
        {
            "type": "object",
            "properties": {"combo": {"type": "string"}},
            "required": ["combo"],
            "additionalProperties": False,
        },
    ),
    (
        "scroll",
        "Scroll at screenshot coordinates.",
        {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                },
                "amount": {
                    "type": "integer",
                    "description": "Scroll amount in wheel ticks (default 3).",
                },
            },
            "required": ["x", "y", "direction"],
            "additionalProperties": False,
        },
    ),
    (
        "cursor_position",
        "Get the current cursor position in screenshot coordinates.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    (
        "open_application",
        "Open an application (or bring it to the foreground) by name. Always "
        "foreground the target app before interacting with it.",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
    (
        "computer_batch",
        "Run a sequence of input actions in order. Faster than individual "
        "calls when the full sequence is predictable. Stops at the first "
        "failing action.",
        {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": _ACTION_PROPERTIES,
                        "required": ["action"],
                    },
                    "minItems": 1,
                }
            },
            "required": ["actions"],
            "additionalProperties": False,
        },
    ),
)

ACTION_TOOLS = {
    "left_click",
    "double_click",
    "right_click",
    "mouse_move",
    "type",
    "key",
    "scroll",
}


COMPANION_NOT_RUNNING_ERROR = (
    "The Openbase desktop companion is not running, so computer use is "
    "unavailable. Start screen sharing first with `openbase-coder "
    "computer-use screen-share start` (it launches the companion), then retry."
)


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _endpoint() -> JsonObject:
    """Resolve the platform's desktop-control endpoint.

    When the Electron desktop app is running it owns the endpoint (dynamic
    port/secret from the control file). Otherwise — developer installs on
    macOS, and always on Linux — the shim talks directly to the screen-share
    companion's own IPC (fixed port, shared local secret) via the same
    /desktop-control contract; the companion itself enforces the
    only-while-screen-sharing policy either way.
    """
    if _is_macos():
        try:
            payload = json.loads(DESKTOP_CONTROL_JSON_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            payload = {}
        port = payload.get("port")
        secret = payload.get("secret")
        if isinstance(port, int) and port > 0 and isinstance(secret, str):
            return {
                "port": port,
                "headers": {"X-Openbase-Desktop-Secret": secret},
                "prefix": "/computer-use",
                "unreachable_error": DESKTOP_NOT_RUNNING_ERROR,
            }
        # No desktop app running: talk to the screen-share companion's own
        # IPC directly, exactly like the Linux path below — the companion
        # serves the same /desktop-control contract and enforces the
        # only-while-screen-sharing policy itself, so developer installs get
        # computer use without the Electron app.

    from openbase_coder_cli.cli.computer_use import (
        DEFAULT_COMPANION_PORT,
        DEFAULT_COMPANION_SECRET,
    )

    try:
        port = int(
            os.environ.get(
                "OPENBASE_LIVEKIT_COMPANION_IPC_PORT", DEFAULT_COMPANION_PORT
            )
        )
    except ValueError:
        port = DEFAULT_COMPANION_PORT
    secret = os.environ.get(
        "OPENBASE_LIVEKIT_COMPANION_IPC_SECRET", DEFAULT_COMPANION_SECRET
    )
    return {
        "port": port,
        "headers": {"X-Openbase-Companion-Secret": secret},
        "prefix": "/desktop-control",
        "unreachable_error": COMPANION_NOT_RUNNING_ERROR,
    }


async def _desktop_request(
    method: str, route: str, *, json_body: JsonObject | None = None
) -> JsonObject:
    endpoint = _endpoint()
    url = f"http://127.0.0.1:{endpoint['port']}{endpoint['prefix']}{route}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                method, url, headers=endpoint["headers"], json=json_body
            )
    except httpx.HTTPError:
        raise RuntimeError(endpoint["unreachable_error"]) from None

    payload: JsonObject = {}
    if response.content:
        try:
            parsed = response.json()
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            payload = parsed
    if response.status_code >= 400 or payload.get("ok") is False:
        error = payload.get("error")
        raise RuntimeError(
            error
            if isinstance(error, str) and error
            else f"Desktop computer-use request failed ({response.status_code})."
        )
    return payload


async def _run_action(arguments: JsonObject) -> JsonObject:
    return await _desktop_request("POST", "/action", json_body=arguments)


async def _call_tool(name: str, arguments: JsonObject) -> list[types.ContentBlock]:
    if name == "screenshot":
        payload = await _desktop_request("POST", "/screenshot")
        image = payload.get("image")
        if not isinstance(image, str) or not image:
            raise RuntimeError("Desktop app returned no screenshot image.")
        summary = (
            f"Screenshot captured at {payload.get('width')}x{payload.get('height')} "
            "pixels. Click coordinates map 1:1 to this image."
        )
        return [
            types.ImageContent(type="image", data=image, mimeType="image/png"),
            types.TextContent(type="text", text=summary),
        ]

    if name in ACTION_TOOLS:
        await _run_action({"type": name, **arguments})
        return [types.TextContent(type="text", text=f"Performed {name}.")]

    if name == "cursor_position":
        payload = await _desktop_request("GET", "/cursor")
        return [
            types.TextContent(
                type="text",
                text=f"Cursor is at ({payload.get('x')}, {payload.get('y')}) in "
                "screenshot coordinates.",
            )
        ]

    if name == "open_application":
        await _desktop_request(
            "POST", "/open-app", json_body={"name": arguments["name"]}
        )
        return [
            types.TextContent(
                type="text",
                text=f"Opened and foregrounded {arguments['name']}.",
            )
        ]

    if name == "computer_batch":
        actions = arguments.get("actions") or []
        for index, action in enumerate(actions):
            action_type = action.get("action")
            try:
                await _run_action({**action, "type": action_type})
            except RuntimeError as exc:
                raise RuntimeError(
                    f"Batch stopped at action {index + 1}/{len(actions)} "
                    f"({action_type}): {exc}"
                ) from None
        return [
            types.TextContent(
                type="text", text=f"Performed {len(actions)} batched actions."
            )
        ]

    raise RuntimeError(f"Unknown tool: {name}")


def create_server() -> Server:
    server = Server(SERVER_NAME, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=name, description=description, inputSchema=schema)
            for name, description, schema in TOOL_DEFINITIONS
        ]

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[types.ContentBlock]:
        return await _call_tool(name, dict(arguments or {}))

    return server


async def run_stdio() -> None:
    server = create_server()
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version="0.1.0",
                instructions=INSTRUCTIONS,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    print("Openbase computer-use MCP running on stdio.", file=sys.stderr)
    asyncio.run(run_stdio())

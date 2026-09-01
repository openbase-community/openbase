"""Coding backend settings API views."""

from __future__ import annotations

import json
import os
import subprocess

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import claude_plugins
from openbase_coder_cli.backend_binaries import find_backend_binary
from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    DEFAULT_CODING_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
    SELECTABLE_BACKENDS,
    normalize_backend,
)
from openbase_coder_cli.claude_auth import verified_claude_auth_status
from openbase_coder_cli.cli.backend import read_backend, write_backend
from openbase_coder_cli.paths import CODEX_HOME_DIR, DEFAULT_ENV_FILE_PATH

BACKEND_OPTIONS = {
    "codex": {
        "label": "Codex",
        "summary": "Native OpenAI Codex app-server.",
        "description": "Uses Codex app-server directly with OpenAI models.",
    },
    "openbase_cloud": {
        "label": "Openbase Cloud",
        "summary": "Managed Claude Code through Openbase Cloud.",
        "description": "Uses your Openbase login with the Openbase Cloud Claude proxy; no personal Anthropic account is required.",
    },
    "claude_code": {
        "label": "Claude Code",
        "summary": "Claude Code Agent SDK.",
        "description": "Uses local Claude auth and billing through the Claude Code Agent SDK for Super Agents UI-driver sessions.",
    },
}

CODEX_PLUGIN_MARKETPLACE = "openai-bundled"
CODEX_PLUGIN_TOGGLES = {
    "computer-use": {
        "id": "computer-use",
        "plugin_id": "computer-use@openai-bundled",
        "label": "Computer Use",
        "description": "Adds the $computer-use skill and computer-use tools to Codex dispatcher sessions.",
    },
    "chrome": {
        "id": "chrome",
        "plugin_id": "chrome@openai-bundled",
        "label": "Chrome",
        "description": "Adds the $chrome skill and Chrome browser-control tools to Codex dispatcher sessions.",
    },
}
# Claude Code's built-in computer-use server is interactive-only, so the
# Claude backend uses an Openbase MCP server proxied through the desktop app.
# Chrome is different: --chrome works headlessly, so the toggle passes the
# flag to every session via SUPER_AGENTS_CLAUDE_EXTRA_ARGS.
CLAUDE_PLUGIN_TOGGLES = {
    "computer-use": {
        "id": "computer-use",
        "plugin_id": claude_plugins.COMPUTER_USE_SERVER_NAME,
        "label": "Computer Use",
        "description": (
            "Adds Openbase computer-use tools (hosted by the desktop app) to "
            "Claude Code dispatcher sessions."
        ),
    },
    "chrome": {
        "id": "chrome",
        "plugin_id": "claude-in-chrome",
        "label": "Chrome",
        "description": (
            "Adds the Claude in Chrome browser-control tools to Claude Code "
            "dispatcher sessions (requires the Claude Chrome extension)."
        ),
    },
}


def _claude_plugin_enabled(plugin_name: str) -> bool:
    if plugin_name == "chrome":
        return claude_plugins.chrome_enabled()
    return claude_plugins.computer_use_enabled()


def _set_claude_plugin_enabled(plugin_name: str, enabled: bool) -> bool:
    if plugin_name == "chrome":
        return claude_plugins.set_chrome_enabled(enabled)
    return claude_plugins.set_computer_use_enabled(enabled)


class CodingBackendSerializer(serializers.Serializer):
    backend = serializers.ChoiceField(choices=SELECTABLE_BACKENDS, required=False)
    location = serializers.ChoiceField(choices=("local", "cloud"), required=False)

    def validate(self, attrs):
        if not attrs.get("backend") and not attrs.get("location"):
            raise serializers.ValidationError(
                "Provide either 'location' (local|cloud) or a legacy 'backend'."
            )
        return attrs


class CodexPluginToggleSerializer(serializers.Serializer):
    plugin = serializers.ChoiceField(choices=tuple(CODEX_PLUGIN_TOGGLES))
    enabled = serializers.BooleanField()


class ClaudePluginToggleSerializer(serializers.Serializer):
    plugin = serializers.ChoiceField(choices=tuple(CLAUDE_PLUGIN_TOGGLES))
    enabled = serializers.BooleanField()


def _restart_hint(backend: str) -> str:
    if backend == CODEX_BACKEND:
        return "Restart or recreate the dispatcher/MCP host for Super Agents to pick up the change."
    return "Restart or recreate the dispatcher/MCP host for Claude Code to pick up the change; keep Openbase services running."


def _backend_note(backend: str) -> str | None:
    if backend == OPENBASE_CLOUD_BACKEND:
        return "Claude Code is configured to use the Openbase Cloud model proxy."
    return None


def _claude_auth_payload(*, verify: bool = False) -> dict:
    # Sessions share the user's own ~/.claude login; there is nothing to
    # bridge or sync — just report whether that login works.
    status_result = verified_claude_auth_status()
    return {
        "command": "claude login",
        "logged_in": status_result.logged_in,
        "raw_output": status_result.raw_output,
        "returncode": status_result.returncode,
        "verified": verify,
    }


def _backend_payload(*, changed: bool = False, sync_claude_auth: bool = False) -> dict:
    from openbase_coder_cli.dispatcher_config import backend_location

    configured_backend = read_backend(DEFAULT_ENV_FILE_PATH)
    payload = {
        "backend": configured_backend,
        "configured_backend": configured_backend,
        # Where code runs is the user-facing choice; the engine (Claude Code
        # vs Codex) follows the model picked per role, not a global setting.
        "location": backend_location(configured_backend),
        "location_options": [
            {
                "id": "local",
                "label": "Local CLI",
                "description": (
                    "Agents run on this machine. Both engines (Claude Code "
                    "and Codex) are available; each launch's model picks the "
                    "engine."
                ),
            },
            {
                "id": "cloud",
                "label": "Openbase Cloud",
                "description": (
                    "Agents run through Openbase Cloud with your Openbase "
                    "login (Claude Code engine). Existing local Codex threads "
                    "stay visible read-only."
                ),
            },
        ],
        "execution_backend": CLAUDE_CODE_BACKEND
        if configured_backend == OPENBASE_CLOUD_BACKEND
        else CODEX_BACKEND
        if configured_backend == OPENBASE_CLOUD_CODEX_BACKEND
        else configured_backend,
        "codex_provider": "openbase_cloud"
        if configured_backend == OPENBASE_CLOUD_CODEX_BACKEND
        else "direct",
        "claude_provider": "openbase_cloud"
        if configured_backend == OPENBASE_CLOUD_BACKEND
        else "direct",
        "backend_note": _backend_note(configured_backend),
        "default_backend": DEFAULT_CODING_BACKEND,
        "supported_backends": [
            {
                "id": backend_name,
                **BACKEND_OPTIONS[backend_name],
            }
            for backend_name in SELECTABLE_BACKENDS
        ],
        "env_file_exists": DEFAULT_ENV_FILE_PATH.is_file(),
        "changed": changed,
        "restart_required": changed,
        "restart_hint": _restart_hint(configured_backend),
    }
    if configured_backend == CLAUDE_CODE_BACKEND:
        payload["claude_auth"] = _claude_auth_payload(verify=sync_claude_auth)
    return payload


def _codex_command() -> str:
    return str(find_backend_binary("codex") or "codex")


def _codex_plugin_env() -> dict[str, str]:
    return {
        **os.environ,
        "CODEX_HOME": str(CODEX_HOME_DIR),
    }


def _run_codex_plugin_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [_codex_command(), "plugin", *args],
            env=_codex_plugin_env(),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("codex CLI was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("codex plugin command timed out") from exc


def _codex_plugin_list() -> dict:
    result = _run_codex_plugin_command(
        ["list", "--marketplace", CODEX_PLUGIN_MARKETPLACE, "--json", "--available"]
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "codex plugin list failed").strip()
        raise RuntimeError(detail)
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("codex plugin list returned invalid JSON") from exc


def _codex_plugin_index() -> dict[str, dict]:
    payload = _codex_plugin_list()
    index: dict[str, dict] = {}
    for group_name in ("installed", "available"):
        for item in payload.get(group_name, []):
            if isinstance(item, dict) and isinstance(item.get("pluginId"), str):
                if group_name == "installed":
                    index[item["pluginId"]] = item
                else:
                    index.setdefault(item["pluginId"], item)
    return index


def _codex_plugins_payload(*, changed_plugin: str | None = None) -> dict:
    plugin_index = _codex_plugin_index()
    plugins = []
    for toggle in CODEX_PLUGIN_TOGGLES.values():
        plugin_info = plugin_index.get(toggle["plugin_id"], {})
        plugins.append(
            {
                **toggle,
                "installed": bool(plugin_info.get("installed")),
                "enabled": bool(plugin_info.get("installed"))
                and bool(plugin_info.get("enabled")),
                "version": plugin_info.get("version") or None,
            }
        )
    return {
        "backend": read_backend(DEFAULT_ENV_FILE_PATH),
        "codex_home": str(CODEX_HOME_DIR),
        "plugins": plugins,
        "changed": changed_plugin is not None,
        "changed_plugin": changed_plugin,
        "restart_required": changed_plugin is not None,
        "restart_hint": "Recreate the dispatcher thread so Codex reloads plugin skills and tools.",
    }


def _set_codex_plugin_enabled(plugin_name: str, enabled: bool) -> None:
    selector = CODEX_PLUGIN_TOGGLES[plugin_name]["plugin_id"]
    command = "add" if enabled else "remove"
    result = _run_codex_plugin_command([command, selector, "--json"])
    if result.returncode != 0:
        detail = (
            result.stderr or result.stdout or f"codex plugin {command} failed"
        ).strip()
        raise RuntimeError(detail)


@api_view(["GET", "PUT"])
def coding_backend_settings(request):
    """Read or update the coding backend used by Super Agents."""
    if request.method == "GET":
        return Response(_backend_payload())

    serializer = CodingBackendSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    previous_backend = read_backend(DEFAULT_ENV_FILE_PATH)
    location = serializer.validated_data.get("location")
    try:
        if location:
            from openbase_coder_cli.cli.backend import write_backend_location

            write_backend_location(DEFAULT_ENV_FILE_PATH, location)
        else:
            write_backend(
                DEFAULT_ENV_FILE_PATH,
                normalize_backend(serializer.validated_data["backend"]),
            )
    except ValueError as exc:
        return Response(
            {"error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    next_backend = read_backend(DEFAULT_ENV_FILE_PATH)
    return Response(
        _backend_payload(
            changed=previous_backend != next_backend,
            sync_claude_auth=True,
        )
    )


@api_view(["GET", "PUT"])
def codex_plugin_settings(request):
    """Read or toggle Codex plugins for Openbase's managed Codex home."""
    if request.method == "GET":
        try:
            return Response(_codex_plugins_payload())
        except RuntimeError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    serializer = CodexPluginToggleSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    plugin_name = serializer.validated_data["plugin"]
    enabled = serializer.validated_data["enabled"]

    try:
        before = _codex_plugin_index().get(
            CODEX_PLUGIN_TOGGLES[plugin_name]["plugin_id"], {}
        )
        current_enabled = bool(before.get("installed")) and bool(before.get("enabled"))
        if current_enabled != enabled:
            _set_codex_plugin_enabled(plugin_name, enabled)
            return Response(_codex_plugins_payload(changed_plugin=plugin_name))
        return Response(_codex_plugins_payload())
    except RuntimeError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


def _claude_plugins_payload(*, changed_plugin: str | None = None) -> dict:
    plugins = []
    for toggle in CLAUDE_PLUGIN_TOGGLES.values():
        enabled = _claude_plugin_enabled(toggle["id"])
        plugins.append(
            {
                **toggle,
                "installed": enabled,
                "enabled": enabled,
                "version": None,
            }
        )
    return {
        "backend": read_backend(DEFAULT_ENV_FILE_PATH),
        "claude_config_dir": str(claude_plugins.CLAUDE_STATE_PATH.parent),
        "plugins": plugins,
        "changed": changed_plugin is not None,
        "changed_plugin": changed_plugin,
        "restart_required": changed_plugin is not None,
        "restart_hint": (
            "Restart Openbase services and recreate the dispatcher so Claude "
            "Code sessions pick up plugin changes."
        ),
    }


@api_view(["GET", "PUT"])
def claude_plugin_settings(request):
    """Read or toggle Openbase plugins for Claude Code backend sessions."""
    if request.method == "GET":
        return Response(_claude_plugins_payload())

    serializer = ClaudePluginToggleSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    plugin_name = serializer.validated_data["plugin"]
    enabled = serializer.validated_data["enabled"]
    changed = _set_claude_plugin_enabled(plugin_name, enabled)
    return Response(
        _claude_plugins_payload(changed_plugin=plugin_name if changed else None)
    )


@api_view(["GET", "POST"])
def claude_auth_settings(request):
    """Read or sync Openbase's managed Claude Code auth status."""
    configured_backend = read_backend(DEFAULT_ENV_FILE_PATH)
    if configured_backend != CLAUDE_CODE_BACKEND:
        return Response(
            {
                "error": "Claude auth settings are available only when the coding backend is Claude Code.",
                "backend": configured_backend,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response(_claude_auth_payload(verify=request.method == "POST"))

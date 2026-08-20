"""Launchd, Openbase service, and status API views."""

from __future__ import annotations

import logging
import socket
import subprocess

import click
import httpx
from django.conf import settings
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.codex_home_instructions import (
    refresh_openbase_instruction_files_from_installation,
)
from openbase_coder_cli.openbase_coder_cli_app.common import _auth_debug_value
from openbase_coder_cli.services.console_settings import (
    DEFAULT_DANGEROUS_CONFIRMATION_PHRASE,
    DEFAULT_INCLUDE_NORMAL_CODEX_AGENTS,
    DEFAULT_KEEP_SYSTEM_AWAKE,
    DEFAULT_USER_ADDRESS_NAME,
    get_dangerous_confirmation_phrase,
    get_ignored_launchctl_labels,
    get_keep_system_awake_enabled,
    get_user_address_name,
    include_normal_codex_agents_in_openbase_agents,
    set_dangerous_confirmation_phrase,
    set_ignored_launchctl_labels,
    set_include_normal_codex_agents_in_openbase_agents,
    set_keep_system_awake_enabled,
    set_user_address_name,
)
from openbase_coder_cli.services.definitions import SERVICES
from openbase_coder_cli.services.keep_awake import keep_awake_status_payload
from openbase_coder_cli.services.launchctl_tools import (
    list_launchctl_services_payload,
    run_launchctl_service_action,
)
from openbase_coder_cli.services.launchd import launchctl_status
from openbase_coder_cli.services.openbase_services import (
    list_openbase_services_payload,
    run_openbase_service_action,
    schedule_openbase_restart_payload,
)
from openbase_coder_cli.services.restart import restart_target_names
from openbase_coder_cli.services.selection import configured_coding_backend
from openbase_coder_cli.services.tailscale_serve import tailscale_serve_health
from openbase_coder_cli.thread_sync.claude_thread_sync import (
    ClaudeConflictResolutionError,
    claude_thread_snapshot_conflicts_payload,
    claude_thread_snapshot_status,
    claude_thread_sync_conflicts_payload,
    resolve_claude_snapshot_conflict,
)
from openbase_coder_cli.thread_sync.thread_exchange import (
    ThreadConflictResolutionError,
    resolve_thread_snapshot_conflict,
    thread_snapshot_conflicts_payload,
    thread_snapshot_status,
    thread_sync_conflicts_payload,
)

logger = logging.getLogger(__name__)


class LaunchctlActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["start", "stop", "restart"])


class OpenbaseServiceActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["start", "stop", "restart"])


class OpenbaseRestartSerializer(serializers.Serializer):
    service = serializers.ChoiceField(
        choices=restart_target_names(),
        required=False,
        allow_null=True,
    )
    recreate_dispatcher = serializers.BooleanField(required=False, default=False)


class ThreadSyncConflictResolutionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=["accept_local", "accept_remote_latest"],
    )
    backend = serializers.ChoiceField(
        choices=["codex", "claude"],
        required=False,
        default="codex",
    )


class IgnoredLaunchctlLabelsSerializer(serializers.Serializer):
    ignored_labels = serializers.ListField(
        child=serializers.CharField(allow_blank=False, trim_whitespace=True),
        required=True,
    )


class DangerousConfirmationSettingsSerializer(serializers.Serializer):
    dangerous_confirmation_phrase = serializers.CharField(
        allow_blank=False,
        trim_whitespace=True,
        required=False,
    )
    user_address_name = serializers.CharField(
        allow_blank=False,
        trim_whitespace=True,
        required=False,
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("No settings were provided.")
        return attrs


class AgentsGenerationSettingsSerializer(serializers.Serializer):
    include_normal_codex_agents_in_openbase_agents = serializers.BooleanField()


class KeepAwakeSettingsSerializer(serializers.Serializer):
    keep_system_awake = serializers.BooleanField()


def _dangerous_confirmation_settings_payload(*, refreshed: bool = False) -> dict:
    return {
        "dangerous_confirmation_phrase": get_dangerous_confirmation_phrase(),
        "default_dangerous_confirmation_phrase": DEFAULT_DANGEROUS_CONFIRMATION_PHRASE,
        "user_address_name": get_user_address_name(),
        "default_user_address_name": DEFAULT_USER_ADDRESS_NAME,
        "refreshed": refreshed,
    }


def _agents_generation_settings_payload(*, refreshed: bool = False) -> dict:
    return {
        "include_normal_codex_agents_in_openbase_agents": (
            include_normal_codex_agents_in_openbase_agents()
        ),
        "default_include_normal_codex_agents_in_openbase_agents": (
            DEFAULT_INCLUDE_NORMAL_CODEX_AGENTS
        ),
        "refreshed": refreshed,
    }


def _keep_awake_settings_payload() -> dict:
    return {
        "keep_system_awake": get_keep_system_awake_enabled(),
        "default_keep_system_awake": DEFAULT_KEEP_SYSTEM_AWAKE,
        "restart_required": True,
        "restart_hint": (
            "Restart Openbase services for keep-awake changes to affect the "
            "running server process."
        ),
    }


@api_view(["GET"])
def launchctl_services_list(request):
    """List user LaunchAgents with current launchctl runtime state."""
    return Response(list_launchctl_services_payload())


@api_view(["GET", "PATCH"])
def launchctl_ignored_settings(request):
    """Read or update locally ignored LaunchAgent labels."""
    if request.method == "GET":
        return Response({"ignored_labels": get_ignored_launchctl_labels()})

    serializer = IgnoredLaunchctlLabelsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    ignored_labels = set_ignored_launchctl_labels(
        serializer.validated_data["ignored_labels"]
    )
    return Response({"ignored_labels": ignored_labels})


@api_view(["GET", "PATCH"])
def dangerous_confirmation_settings(request):
    """Read or update the exact phrase required for risky operations."""
    if request.method == "GET":
        return Response(_dangerous_confirmation_settings_payload())

    serializer = DangerousConfirmationSettingsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    phrase = None
    user_address_name = None
    if "dangerous_confirmation_phrase" in serializer.validated_data:
        phrase = set_dangerous_confirmation_phrase(
            serializer.validated_data["dangerous_confirmation_phrase"]
        )
    if "user_address_name" in serializer.validated_data:
        user_address_name = set_user_address_name(
            serializer.validated_data["user_address_name"]
        )
    refreshed = refresh_openbase_instruction_files_from_installation()
    payload = _dangerous_confirmation_settings_payload(refreshed=refreshed)
    if phrase is not None:
        payload["dangerous_confirmation_phrase"] = phrase
    if user_address_name is not None:
        payload["user_address_name"] = user_address_name
    return Response(payload)


@api_view(["GET", "PATCH"])
def agents_generation_settings(request):
    """Read or update generated Openbase instruction settings."""
    if request.method == "GET":
        return Response(_agents_generation_settings_payload())

    serializer = AgentsGenerationSettingsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    include_normal = set_include_normal_codex_agents_in_openbase_agents(
        serializer.validated_data["include_normal_codex_agents_in_openbase_agents"]
    )
    refreshed = refresh_openbase_instruction_files_from_installation()
    return Response(
        {
            **_agents_generation_settings_payload(refreshed=refreshed),
            "include_normal_codex_agents_in_openbase_agents": include_normal,
        }
    )


@api_view(["GET", "PATCH"])
def keep_awake_settings(request):
    """Read or update whether the server starts caffeinate."""
    if request.method == "GET":
        return Response(_keep_awake_settings_payload())

    serializer = KeepAwakeSettingsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    keep_awake = set_keep_system_awake_enabled(
        serializer.validated_data["keep_system_awake"]
    )
    return Response(
        {
            **_keep_awake_settings_payload(),
            "keep_system_awake": keep_awake,
        }
    )


@api_view(["POST"])
def launchctl_service_action(request, label):
    """Run a launchctl action for a LaunchAgent label."""
    serializer = LaunchctlActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        run_launchctl_service_action(label, serializer.validated_data["action"])
    except click.ClickException as exc:
        return Response(
            {"error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(list_launchctl_services_payload())


@api_view(["GET"])
def openbase_services_list(request):
    """List Openbase-managed launchd services."""
    return Response(list_openbase_services_payload())


def _with_conflict_backend(payload: dict, backend: str) -> dict:
    """Tag each conflict entry with its backend without touching other fields."""
    conflicts = payload.get("conflicts")
    if not isinstance(conflicts, list):
        return payload
    return {
        **payload,
        "conflicts": [
            {**conflict, "backend": backend} if isinstance(conflict, dict) else conflict
            for conflict in conflicts
        ],
    }


@api_view(["GET"])
def thread_device_sync_status(request):
    """Show cross-device thread snapshot sync status for both backends."""
    return Response(
        {
            **_with_conflict_backend(thread_snapshot_status(), "codex"),
            "claude": _with_conflict_backend(claude_thread_snapshot_status(), "claude"),
        }
    )


@api_view(["GET"])
def thread_device_sync_conflicts(request):
    """Show unresolved cross-device thread snapshot sync conflicts for both backends."""
    return Response(
        {
            **_with_conflict_backend(thread_snapshot_conflicts_payload(), "codex"),
            "claude": _with_conflict_backend(
                claude_thread_snapshot_conflicts_payload(), "claude"
            ),
        }
    )


@api_view(["GET"])
def thread_sync_conflicts(request):
    """Show unresolved thread sync conflicts across homes and devices for both backends."""
    return Response(
        {
            **_with_conflict_backend(thread_sync_conflicts_payload(), "codex"),
            "claude": _with_conflict_backend(
                claude_thread_sync_conflicts_payload(), "claude"
            ),
        }
    )


@api_view(["POST"])
def thread_device_sync_conflict_resolve(request, thread_id):
    """Resolve one cross-device thread snapshot sync conflict."""
    serializer = ThreadSyncConflictResolutionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    action = serializer.validated_data["action"]
    backend = serializer.validated_data["backend"]
    try:
        if backend == "claude":
            payload = resolve_claude_snapshot_conflict(thread_id, action=action)
        else:
            payload = resolve_thread_snapshot_conflict(thread_id, action=action)
        return Response(payload)
    except (ClaudeConflictResolutionError, ThreadConflictResolutionError) as exc:
        return Response(
            {"error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )


@api_view(["POST"])
def openbase_restart(request):
    """Schedule a unified Openbase-managed service restart."""
    serializer = OpenbaseRestartSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        payload = schedule_openbase_restart_payload(
            service_name=serializer.validated_data.get("service"),
            recreate_dispatcher=serializer.validated_data["recreate_dispatcher"],
        )
    except click.ClickException as exc:
        return Response(
            {"error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(payload)


@api_view(["POST"])
def openbase_service_action(request, service_name):
    """Run an Openbase-managed service command."""
    serializer = OpenbaseServiceActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        payload = run_openbase_service_action(
            service_name,
            serializer.validated_data["action"],
        )
    except click.ClickException as exc:
        return Response(
            {"error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(payload)


def _check_port(port: int) -> bool:
    """Check if a TCP port is listening on localhost."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _check_tailscale() -> bool:
    """Check if the active tailnet provider (official Tailscale or Openbase
    netmesh) is connected."""
    from openbase_coder_cli.services import tailscale_provider as tp

    if tp.tool_path() is None:
        return False
    status = tp.status_json()
    if status.get("error"):
        return False
    state = status.get("BackendState")
    if state:
        return state in ("Running", "Starting")
    # Fall back to "has a self tailnet IP".
    self_payload = status.get("Self") or {}
    return bool(self_payload.get("TailscaleIPs"))


def _check_web_backend() -> bool:
    """Check whether the configured web backend is reachable."""
    web_backend_url = getattr(settings, "WEB_BACKEND_URL", "").rstrip("/")
    if not web_backend_url:
        return False
    status_url = f"{web_backend_url}/_allauth/app/v1/config"
    try:
        resp = httpx.get(status_url, timeout=3, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return False
    return 200 <= resp.status_code < 500


@api_view(["GET"])
def service_status(request):
    """Check status of related services."""
    logger.info(
        "service_status start path=%s auth=%s",
        request.path,
        _auth_debug_value(request),
    )
    from openbase_coder_cli.services import tailscale_provider as tp

    _tailnet_label = "Openbase Netmesh" if tp.is_netmesh() else "Tailscale"
    services = {
        "django": {"name": "Django (Coder CLI)", "port": 7999, "optional": False},
        "codex_app_server": {
            "name": "Codex App Server",
            "port": 4500,
            "optional": False,
        },
        "livekit_server": {"name": "LiveKit Server", "port": 7880, "optional": False},
        "livekit_agent": {"name": "LiveKit Agent", "port": 8081, "optional": False},
        "web_backend": {
            "name": "Web Backend",
            "url": f"{getattr(settings, 'WEB_BACKEND_URL', '').rstrip('/')}/_allauth/app/v1/config",
            "port": None,
            "optional": False,
        },
        "tailscale": {"name": _tailnet_label, "port": None, "optional": False},
        "keep_awake": keep_awake_status_payload(),
    }
    # Backend-scoped services (e.g. the Codex App Server on the claude_code
    # backend) are intentionally not installed, so reporting them would raise
    # a false "stopped" warning in the apps.
    coding_backend = configured_coding_backend()
    codex_app_server = next(
        (svc for svc in SERVICES if svc.name == "codex-app-server"), None
    )
    if codex_app_server is not None and not codex_app_server.supports_backend(
        coding_backend
    ):
        del services["codex_app_server"]
    for service_name in (
        "sync-workers",
        "openbase-routines",
    ):
        service = next((svc for svc in SERVICES if svc.name == service_name), None)
        if not service or not service.supports_backend(coding_backend):
            continue
        status_payload = launchctl_status(service)
        services[service_name.replace("-", "_")] = {
            "name": service.description,
            "port": service.port,
            "running": bool(status_payload.get("pid")),
            "installed": bool(status_payload.get("installed")),
            "last_exit_code": status_payload.get("last_exit_code"),
            "optional": not service.install_by_default,
        }
    for key, svc in services.items():
        if "running" not in svc:
            if key == "tailscale":
                svc["running"] = _check_tailscale()
            elif key == "web_backend":
                svc["running"] = _check_web_backend()
            else:
                svc["running"] = _check_port(svc["port"])
        logger.info(
            "service_status probe service=%s running=%s port=%s",
            key,
            svc["running"],
            svc.get("port"),
        )
    serve_health = tailscale_serve_health()
    services["tailscale_serve"] = {
        "name": f"{_tailnet_label} Serve",
        "port": 18080,
        "running": serve_health.healthy,
        "host": serve_health.host,
        "url": serve_health.openbase_url,
        "openbase_configured": serve_health.openbase_configured,
        "livekit_configured": serve_health.livekit_configured,
        "openbase_reachable": serve_health.openbase_reachable,
        "error": serve_health.error,
        "optional": False,
    }
    logger.info(
        "service_status probe service=tailscale_serve running=%s url=%s error=%s",
        serve_health.healthy,
        serve_health.openbase_url,
        serve_health.error,
    )
    logger.info("service_status complete")
    return Response({"services": services})

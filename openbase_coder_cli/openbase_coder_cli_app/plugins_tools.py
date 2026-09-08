"""Plugin, bootstrapper, uv tool, and BoilerSync API views."""

from __future__ import annotations

import mimetypes
from pathlib import Path

import click
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.paths import PLUGIN_CONSOLE_ASSETS_DIR
from openbase_coder_cli.plugins.api import (
    get_console_registry_payload,
    get_plugin_payload,
    list_plugins_payload,
    run_bootstrapper_payload,
)
from openbase_coder_cli.services.boilersync import boilersync_templates_payload
from openbase_coder_cli.services.uv_tools import (
    list_uv_tools_payload,
    uninstall_uv_tool,
    uv_tool_help_payload,
)


@api_view(["GET"])
def plugins_list(request):
    """List installed plugins and their declarations."""
    return Response({"plugins": list_plugins_payload()})


@api_view(["GET"])
def plugin_detail(request, plugin_id):
    """Show one installed plugin."""
    payload = get_plugin_payload(plugin_id)
    if payload is None:
        return Response(
            {"error": f"Plugin '{plugin_id}' not found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(payload)


@api_view(["GET"])
def plugin_console_registry(request):
    """Return generated console registry payload."""
    return Response(get_console_registry_payload())


def plugin_console_asset(
    request: HttpRequest, plugin_id: str, page_key: str, path: str = ""
) -> HttpResponse:
    """Serve static iframe assets installed by console plugins."""
    del request
    relative = Path(path or "index.html")
    if relative.is_absolute() or ".." in relative.parts:
        raise Http404("Plugin asset not found")

    root = (PLUGIN_CONSOLE_ASSETS_DIR / plugin_id / page_key).resolve()
    target = (root / relative).resolve()
    if not target.is_file() or not target.is_relative_to(root):
        raise Http404("Plugin asset not found")

    content_type, _encoding = mimetypes.guess_type(str(target))
    return FileResponse(
        open(target, "rb"),
        content_type=content_type or "application/octet-stream",
    )


@api_view(["GET"])
def uv_tools_list(request):
    """List global uv tools and editable install metadata."""
    return Response(list_uv_tools_payload())


@api_view(["GET"])
def uv_tool_executable_help(request, tool_name, executable_name):
    """Run a listed uv tool executable with --help and return local output."""
    payload, response_status = uv_tool_help_payload(tool_name, executable_name)
    return Response(payload, status=response_status)


@api_view(["GET"])
def boilersync_templates(request):
    """List BoilerSync template sources/templates using the real CLI JSON output."""
    template_ref = request.query_params.get("template_ref") or None
    return Response(boilersync_templates_payload(template_ref=template_ref))


@api_view(["DELETE"])
def uv_tool_detail(request, tool_name):
    """Uninstall a global uv tool."""
    try:
        uninstall_uv_tool(tool_name)
    except RuntimeError as exc:
        return Response(
            {"error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response(list_uv_tools_payload())


@api_view(["POST"])
def bootstrap_run(request, bootstrapper_name):
    """Run a plugin bootstrapper by name."""
    params = request.data.get("params", {})
    if not isinstance(params, dict):
        return Response(
            {"error": "params must be an object"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        result = run_bootstrapper_payload(bootstrapper_name, params=params)
    except click.ClickException as exc:
        return Response(
            {"error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response({"result": result})

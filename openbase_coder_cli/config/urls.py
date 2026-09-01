"""
URL configuration for openbase_coder_cli.

The `urlpatterns` list routes URLs to views.
"""

import logging

from django.urls import include, path, re_path

from openbase_coder_cli.config.proxy import serve_console
from openbase_coder_cli.openbase_coder_cli_app.plugins_tools import (
    plugin_console_asset,
)
from openbase_coder_cli.plugins.store import load_registry

logger = logging.getLogger(__name__)

urlpatterns = [
    path("api/", include("openbase_coder_cli.openbase_coder_cli_app.urls")),
    re_path(
        r"^openbase-plugin-assets/(?P<plugin_id>[^/]+)/(?P<page_key>[^/]+)/(?P<path>.*)$",
        plugin_console_asset,
    ),
    # Catch-all: serve built React console (SPA)
    re_path(r"^(?P<path>.*)$", serve_console),
]


for installed_plugin in load_registry().plugins:
    for module_name in installed_plugin.capabilities.django_url_modules:
        try:
            urlpatterns.insert(
                2,
                path(
                    f"api/plugins/{installed_plugin.plugin_id}/",
                    include(module_name),
                ),
            )
        except Exception as exc:
            logger.error(
                "Failed to include plugin URL module %s for plugin %s: %s",
                module_name,
                installed_plugin.plugin_id,
                exc,
            )

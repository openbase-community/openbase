"""
ASGI config for web project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""

from __future__ import annotations

import logging
import os

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase.config.settings")
os.environ["ASGI_THREADS"] = "4"

logger = logging.getLogger(__name__)

django_asgi_app = get_asgi_application()

# Collect websocket patterns from enabled sites
all_websocket_patterns = []

# Import after Django settings are loaded to prevent issues
from openbase.coder.middleware import TokenAuthMiddleware  # noqa: E402, I001
from openbase.coder.routing import websocket_urlpatterns as coder_websocket_patterns  # noqa: E402

all_websocket_patterns.extend(coder_websocket_patterns)

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": TokenAuthMiddleware(URLRouter(all_websocket_patterns)),
    }
)

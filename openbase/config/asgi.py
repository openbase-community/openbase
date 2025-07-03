"""
ASGI config for web project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""

import logging
import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase.config.settings")
os.environ["ASGI_THREADS"] = "4"

logger = logging.getLogger(__name__)

django_asgi_app = get_asgi_application()

# Collect websocket patterns from enabled sites
all_websocket_patterns = []

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack((URLRouter(all_websocket_patterns)))
        ),
    }
)

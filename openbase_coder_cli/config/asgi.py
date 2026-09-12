"""
ASGI config for openbase_coder_cli.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

# Must call get_asgi_application() before importing channels routing
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from openbase_coder_cli.config.self_heal import (  # noqa: E402
    wrap_asgi_application,
)
from openbase_coder_cli.openbase_coder_cli_app.middleware import (  # noqa: E402
    TokenAuthMiddleware,
)
from openbase_coder_cli.openbase_coder_cli_app.routing import (  # noqa: E402
    websocket_urlpatterns,
)

_inner = wrap_asgi_application(
    ProtocolTypeRouter(
        {
            "http": django_asgi_app,
            "websocket": TokenAuthMiddleware(URLRouter(websocket_urlpatterns)),
        }
    )
)


async def application(scope, receive, send):
    """ASGI application with lifespan passthrough."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                # Runs only in the server process (not management commands):
                # keep recent-project metadata warm so the first Threads/
                # Projects visit does not pay the cold git-status cost.
                from openbase_coder_cli.openbase_coder_cli_app.projects import (
                    start_project_metadata_warmer,
                )

                start_project_metadata_warmer()
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    else:
        await _inner(scope, receive, send)

"""
ASGI config for openbase_coder_cli.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import asyncio
import os
from contextlib import suppress

from django.core.asgi import get_asgi_application

from openbase_coder_cli.logging_redaction import install_uvicorn_credential_redaction

install_uvicorn_credential_redaction()

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
from openbase_coder_cli.openbase_coder_cli_app.notification_runtime import (  # noqa: E402
    run_notification_sweeps,
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


_stack_dump_file = None


def _install_stack_dump_signal() -> None:
    """`kill -USR1 <pid>` appends every thread's Python stack to
    ~/.openbase/logs/django-cli-stacks.log: an in-process profiler for a hot or
    wedged API where py-spy would need root (macOS)."""
    global _stack_dump_file
    import faulthandler
    import signal
    from pathlib import Path

    if _stack_dump_file is not None or not hasattr(signal, "SIGUSR1"):
        return
    log_dir = Path.home() / ".openbase" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _stack_dump_file = open(log_dir / "django-cli-stacks.log", "a")  # noqa: SIM115
    faulthandler.register(signal.SIGUSR1, file=_stack_dump_file, all_threads=True)


async def application(scope, receive, send):
    """ASGI application with lifespan passthrough."""
    if scope["type"] == "lifespan":
        notification_task = None
        compaction_task = None
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                _install_stack_dump_signal()
                # Runs only in the server process (not management commands):
                # keep recent-project metadata warm so the first Threads/
                # Projects visit does not pay the cold git-status cost.
                from openbase_coder_cli.openbase_coder_cli_app.projects import (
                    start_project_metadata_warmer,
                )

                start_project_metadata_warmer()
                # Raise the macOS folder-consent dialogs now, at start-up,
                # rather than under the first agent turn (see folder_access).
                from openbase_coder_cli.folder_access import (
                    start_protected_folder_access_request,
                )

                start_protected_folder_access_request()
                notification_task = asyncio.create_task(
                    run_notification_sweeps(), name="notification-producers"
                )
                # Compact the Dispatcher between requests, not during one.
                from openbase_coder_cli.dispatcher_compaction import (
                    dispatcher_compaction_loop,
                )

                compaction_task = asyncio.create_task(
                    dispatcher_compaction_loop(), name="dispatcher-compaction"
                )
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if notification_task is not None:
                    notification_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await notification_task
                if compaction_task is not None:
                    compaction_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await compaction_task
                await send({"type": "lifespan.shutdown.complete"})
                return
    else:
        await _inner(scope, receive, send)

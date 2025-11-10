from __future__ import annotations

from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(
        r"^api/coder/messages/send-to-claude-ws/$",
        consumers.ClaudeCodeConsumer.as_asgi(),
    ),
]
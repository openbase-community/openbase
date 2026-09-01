"""WebSocket URL routing."""

from __future__ import annotations

from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/threads/$", consumers.AllThreadsConsumer.as_asgi()),
    re_path(r"ws/threads/(?P<thread_id>[^/]+)/$", consumers.ThreadConsumer.as_asgi()),
    re_path(r"ws/approval-requests/$", consumers.ApprovalRequestsConsumer.as_asgi()),
    re_path(r"ws/ios-app-control/$", consumers.IOSAppControlConsumer.as_asgi()),
]

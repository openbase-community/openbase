from django.urls import path

from . import views

urlpatterns = [
    path(
        "claude-code/message/",
        views.send_message_to_claude_code,
        name="claude_code_message",
    ),
]

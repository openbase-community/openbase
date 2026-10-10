"""Replay a login callback the phone's browser could not deliver.

A CLI login that redirects to ``http://localhost:<port>/...`` ends on the
phone's own loopback when the browser is on the phone. When the phone cannot
forward that port (no VPN, older app, or any failure), the user pastes the
final address back and this endpoint performs the same GET against this
host's loopback, so the waiting CLI receives its callback. Only loopback
targets are accepted and redirects are never followed; the address carries a
single-use code, so it is never logged.
"""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.login_callback import (
    loopback_replay_target,
    replay_loopback_callback,
)

MAX_PASTED_URL_LENGTH = 4096


class OAuthCallbackReplaySerializer(serializers.Serializer):
    url = serializers.CharField(max_length=MAX_PASTED_URL_LENGTH, trim_whitespace=True)

    def validate_url(self, value: str) -> str:
        if loopback_replay_target(value) is None:
            raise serializers.ValidationError(
                "url must be http://localhost:<port>/... (or 127.0.0.1 / [::1]) "
                "with a port of 1024 or higher."
            )
        return value


@api_view(["POST"])
def oauth_callback_replay(request):
    serializer = OAuthCallbackReplaySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    result = replay_loopback_callback(serializer.validated_data["url"])
    return Response(
        result,
        status=status.HTTP_200_OK if result["ok"] else status.HTTP_502_BAD_GATEWAY,
    )

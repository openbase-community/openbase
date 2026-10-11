"""Local API for the optional "AI account" step (link Codex or Claude Code).

GET returns which account the workspace uses, which are linked, and the
running sign-in, if any. POST takes one action:

* ``{"action": "link", "provider": "codex" | "claude_code"}`` starts the
  CLI's own login here; its browser step goes to the phone.
* ``{"action": "code", "code": "..."}`` types a code the sign-in page showed
  into the waiting login (Claude Code).
* ``{"action": "cancel"}`` stops the running sign-in.
* ``{"action": "select", "provider": "openbase_cloud" | "codex" | "claude_code"}``
  switches which account agents use (a linked one, or Openbase Cloud).
* ``{"action": "unlink", "provider": "codex" | "claude_code"}`` signs out and
  falls back to Openbase Cloud when that account was in use.
"""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import ai_account

ACTIONS = ("link", "code", "cancel", "select", "unlink")


class AIAccountActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=ACTIONS)
    provider = serializers.ChoiceField(choices=ai_account.CHOICES, required=False)
    code = serializers.CharField(
        required=False, max_length=ai_account.MAX_CODE_LENGTH, trim_whitespace=True
    )

    def validate(self, attrs):
        action = attrs["action"]
        provider = attrs.get("provider")
        if action in ("link", "unlink") and provider not in ai_account.PROVIDERS:
            raise serializers.ValidationError(
                {"provider": "Choose codex or claude_code."}
            )
        if action == "select" and provider is None:
            raise serializers.ValidationError({"provider": "Choose an account."})
        if action == "code" and not attrs.get("code"):
            raise serializers.ValidationError({"code": "Paste the code."})
        return attrs


@api_view(["GET", "POST"])
def ai_account_settings(request):
    if request.method == "GET":
        return Response(ai_account.status())
    serializer = AIAccountActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    action = serializer.validated_data["action"]
    provider = serializer.validated_data.get("provider")
    changed = False
    try:
        if action == "link":
            ai_account.LOGINS.start(provider)
        elif action == "code":
            ai_account.LOGINS.submit_code(serializer.validated_data["code"])
        elif action == "cancel":
            ai_account.LOGINS.cancel()
        elif action == "select":
            changed = ai_account.select(provider)
        else:
            ai_account.unlink(provider)
            changed = True
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except RuntimeError as exc:
        return Response(
            {"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    return Response({**ai_account.status(), "changed": changed})

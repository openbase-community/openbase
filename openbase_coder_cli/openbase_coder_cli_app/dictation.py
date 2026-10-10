"""Owner-authenticated native dictation preparation; provider keys stay here."""

from __future__ import annotations

import os

import httpx
from dotenv import dotenv_values
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import paths
from openbase_coder_cli.dispatcher_config import selected_stt_provider_id
from openbase_coder_cli.stt_providers import (
    ASSEMBLYAI_STT_PROVIDER_ID,
    OPENBASE_CLOUD_STT_PROVIDER_ID,
)


def _response(payload: dict, status: int = 200) -> Response:
    return Response(payload, status=status, headers={"Cache-Control": "no-store"})


@api_view(["POST"])
def dictation_session(request):
    """Read the backend's STT choice, independently of the call voice engine.

    The default local API authentication enforces installation ownership for
    cloud JWTs before this view can read a key or mint a provider capability.
    No caller-supplied provider, key, URL or Cloud identity is accepted.
    """
    provider = selected_stt_provider_id()
    if provider == OPENBASE_CLOUD_STT_PROVIDER_ID:
        return _response({"provider": provider})
    if provider != ASSEMBLYAI_STT_PROVIDER_ID:
        return _response(
            {
                "code": "unsupported_provider",
                "detail": "Dictation supports AssemblyAI or Openbase Cloud. Change STT on the selected backend.",
            },
            409,
        )

    # Settings edits write the canonical file. Prefer its current value,
    # including an explicitly cleared key, over an older process environment.
    values = dotenv_values(paths.DEFAULT_ENV_FILE_PATH)
    key = (
        values.get("ASSEMBLY_AI_API_KEY")
        if "ASSEMBLY_AI_API_KEY" in values
        else os.getenv("ASSEMBLY_AI_API_KEY")
    ) or ""
    if not key.strip():
        return _response(
            {
                "code": "missing_key",
                "detail": "Add an AssemblyAI API key on the selected backend to dictate with BYOK.",
            },
            409,
        )
    try:
        result = httpx.get(
            "https://streaming.assemblyai.com/v3/token",
            headers={"Authorization": key.strip()},
            params={"expires_in_seconds": 60, "max_session_duration_seconds": 300},
            timeout=8,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return _response(
            {
                "code": "provider_unavailable",
                "detail": "Couldn't reach AssemblyAI. Retry dictation or check the selected backend's connection.",
            },
            503,
        )
    # Provider auth failures are configuration errors, never an Openbase 401:
    # neither mobile client should refresh or discard its own login for these.
    if result.status_code in (401, 403):
        return _response(
            {
                "code": "invalid_key",
                "detail": "AssemblyAI rejected the selected backend's BYOK key. Check the key and AssemblyAI account.",
            },
            422,
        )
    if result.status_code != 200:
        return _response(
            {
                "code": "provider_unavailable",
                "detail": "AssemblyAI couldn't prepare dictation. Check its account limits or retry.",
            },
            503,
        )
    try:
        payload = result.json()
    except ValueError:
        payload = None
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.strip() or token == key.strip():
        return _response(
            {
                "code": "provider_unavailable",
                "detail": "AssemblyAI returned an invalid dictation token. Retry dictation.",
            },
            503,
        )
    return _response({"provider": provider, "token": token})

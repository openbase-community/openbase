"""Per-thread model switching API.

`GET /api/threads/<id>/models/` lists the models a thread can switch between —
only models on the thread's own execution backend (same-backend switching;
cross-backend moves are thread continuations, not model switches) — plus the
thread's current selection. `PUT` stores a model override that applies to
every subsequent turn of the thread without restarting the Codex app server
or any Openbase service: the session manager reads the override when it
builds each turn's input, and both backends accept a per-turn model (Claude
Code via `set_model` on the live SDK client, Codex via `turn/start` params).
"""

from __future__ import annotations

from asgiref.sync import async_to_sync
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import dispatcher_config
from openbase_coder_cli.openbase_coder_cli_app.thread_cache import (
    invalidate_thread_list_cache,
)
from openbase_coder_cli.thread_model_overrides import (
    get_thread_model_override,
    set_thread_model_override,
)
from openbase_coder_cli.thread_sync.session_manager import get_session_manager


class ThreadModelSerializer(serializers.Serializer):
    model = serializers.CharField(allow_null=True, allow_blank=True)


def thread_engine(backend: str | None) -> str | None:
    """The engine ("claude" / "codex") a thread's backend identity runs on."""
    from super_agents.backend_config import (
        CLAUDE_CODE_BACKEND,
        execution_backend,
        normalize_backend,
    )

    try:
        identity = normalize_backend(backend)
    except ValueError:
        return None
    return (
        dispatcher_config.CLAUDE_ENGINE
        if execution_backend(identity) == CLAUDE_CODE_BACKEND
        else dispatcher_config.CODEX_ENGINE
    )


def model_options_for_thread(backend: str | None) -> tuple[dict, ...]:
    """Selectable models for a thread: its own engine's options only."""
    engine = thread_engine(backend)
    if engine is None:
        return ()
    location = dispatcher_config.backend_location(backend)
    return tuple(
        option
        for option in dispatcher_config.combined_model_options(location)
        if option["engine"] == engine
    )


def validate_model_for_thread(backend: str | None, model: str) -> str:
    """Normalize `model` and require it to run on the thread's own backend.

    Returns the option id to use, or raises ValueError with a user-facing
    message (unknown model, or a cross-backend switch attempt).
    """
    normalized = " ".join(model.split()).lower()
    if not normalized:
        raise ValueError("model is required")
    options = model_options_for_thread(backend)
    for option in options:
        if normalized == option["id"].lower():
            if not option["available"]:
                raise ValueError(
                    option.get("unavailable_reason")
                    or f"Model {option['id']} is not available on this backend location."
                )
            return option["id"]
    model_engine = dispatcher_config.model_engine(normalized)
    engine = thread_engine(backend)
    if model_engine is not None and engine is not None and model_engine != engine:
        raise ValueError(
            f"Model {normalized} runs on the {model_engine} backend; this thread "
            f"runs on {engine}. Threads can only switch between models of the "
            "same backend."
        )
    allowed = ", ".join(option["id"] for option in options if option["available"])
    raise ValueError(f"Unknown model {normalized}. Model must be one of: {allowed}.")


def _selected_model(
    thread_model: str | None, override: str | None, options: tuple[dict, ...]
) -> str | None:
    """The option id the thread currently resolves to, if any."""
    for candidate in (override, thread_model):
        if not candidate:
            continue
        normalized = " ".join(candidate.split()).lower()
        for option in options:
            if normalized == option["id"].lower():
                return option["id"]
    return None


def _thread_models_payload(thread_id: str, thread) -> dict:
    options = model_options_for_thread(thread.backend)
    override = get_thread_model_override(thread_id)
    return {
        "thread_id": thread_id,
        "backend": thread.backend,
        "engine": thread_engine(thread.backend),
        "model": _selected_model(thread.model, override, options),
        "model_override": override,
        "options": [dict(option) for option in options],
    }


@api_view(["GET", "PUT"])
def thread_model_settings(request, thread_id):
    """Read or switch the model used by one thread's subsequent turns."""
    manager = get_session_manager()
    thread = async_to_sync(manager.get_thread_state)(thread_id)
    if thread is None:
        return Response(
            {"error": f"Thread {thread_id} not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    if request.method == "GET":
        return Response(_thread_models_payload(thread_id, thread))

    serializer = ThreadModelSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    model = serializer.validated_data["model"]
    if model:
        try:
            model = validate_model_for_thread(thread.backend, model)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    set_thread_model_override(thread_id, model or None)
    invalidate_thread_list_cache()
    return Response(_thread_models_payload(thread_id, thread))

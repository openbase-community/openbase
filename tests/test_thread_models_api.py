from __future__ import annotations

# ruff: noqa: E402, I001
import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import thread_models
from openbase_coder_cli.openbase_coder_cli_app import threads as thread_views
from openbase_coder_cli.openbase_coder_cli_app.thread_models import (
    model_options_for_thread,
    validate_model_for_thread,
)
from openbase_coder_cli.thread_model_overrides import (
    get_thread_model_override,
    set_thread_model_override,
)
from openbase_coder_cli.thread_sync.models import ThreadInfo


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENBASE_CODER_BACKEND", raising=False)
    return tmp_path


class FakeManager:
    def __init__(self, threads: list[ThreadInfo]) -> None:
        self._threads = {thread.session_id: thread for thread in threads}
        self.turns: list[tuple[str, str, str | None]] = []

    async def get_thread_state(self, thread_id: str) -> ThreadInfo | None:
        return self._threads.get(thread_id)

    async def start_turn(
        self, thread_id: str, prompt: str, model: str | None = None
    ) -> str:
        self.turns.append((thread_id, prompt, model))
        return "turn-1"

    async def queue_turn(
        self, thread_id: str, prompt: str, model: str | None = None
    ) -> dict:
        self.turns.append((thread_id, prompt, model))
        return {"queued": True}


def _threads() -> list[ThreadInfo]:
    return [
        ThreadInfo(
            session_id="claude-1",
            directory="/tmp/p",
            backend="claude_code",
            model="claude-fable-5-1",
        ),
        ThreadInfo(session_id="codex-1", directory="/tmp/p", backend="codex"),
    ]


def _request(method: str, url: str, data: dict | None = None):
    factory = APIRequestFactory()
    request = getattr(factory, method)(url, data, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


# --- validation -------------------------------------------------------------


def test_options_are_limited_to_the_threads_engine() -> None:
    claude_ids = {o["id"] for o in model_options_for_thread("claude_code")}
    codex_ids = {o["id"] for o in model_options_for_thread("codex")}
    assert "claude-fable-5-1" in claude_ids and "gpt-5.6-terra" not in claude_ids
    assert "gpt-5.6-terra" in codex_ids and "claude-fable-5-1" not in codex_ids
    assert model_options_for_thread("not-a-backend") == ()


def test_validate_normalizes_same_engine_models() -> None:
    assert (
        validate_model_for_thread("claude_code", "  CLAUDE-OPUS-5-5 ")
        == "claude-opus-5-5"
    )
    assert validate_model_for_thread("codex", "GPT-5.6-TERRA") == "gpt-5.6-terra"


def test_validate_rejects_cross_backend_and_unknown_models() -> None:
    with pytest.raises(ValueError, match="same backend"):
        validate_model_for_thread("claude_code", "gpt-5.6-terra")
    with pytest.raises(ValueError, match="Unknown model"):
        validate_model_for_thread("codex", "gpt-2")
    with pytest.raises(ValueError, match="model is required"):
        validate_model_for_thread("codex", "   ")


# --- GET/PUT /threads/<id>/models/ -------------------------------------------


def test_get_lists_engine_options_and_current_selection(monkeypatch) -> None:
    manager = FakeManager(_threads())
    monkeypatch.setattr(thread_models, "get_session_manager", lambda: manager)

    response = thread_models.thread_model_settings(
        _request("get", "/api/threads/claude-1/models/"), "claude-1"
    )

    assert response.status_code == 200
    assert response.data["engine"] == "claude"
    assert response.data["model"] == "claude-fable-5-1"
    assert response.data["model_override"] is None
    assert {o["engine"] for o in response.data["options"]} == {"claude"}


def test_put_stores_override_and_get_reflects_it(monkeypatch) -> None:
    manager = FakeManager(_threads())
    monkeypatch.setattr(thread_models, "get_session_manager", lambda: manager)

    response = thread_models.thread_model_settings(
        _request("put", "/api/threads/claude-1/models/", {"model": "CLAUDE-OPUS-5-5"}),
        "claude-1",
    )
    assert response.status_code == 200
    assert response.data["model"] == "claude-opus-5-5"
    assert response.data["model_override"] == "claude-opus-5-5"
    assert get_thread_model_override("claude-1") == "claude-opus-5-5"

    cleared = thread_models.thread_model_settings(
        _request("put", "/api/threads/claude-1/models/", {"model": None}),
        "claude-1",
    )
    assert cleared.status_code == 200
    assert cleared.data["model"] == "claude-fable-5-1"
    assert cleared.data["model_override"] is None
    assert get_thread_model_override("claude-1") is None


def test_put_rejects_cross_backend_model(monkeypatch) -> None:
    manager = FakeManager(_threads())
    monkeypatch.setattr(thread_models, "get_session_manager", lambda: manager)

    response = thread_models.thread_model_settings(
        _request("put", "/api/threads/codex-1/models/", {"model": "claude-opus-5-5"}),
        "codex-1",
    )
    assert response.status_code == 400
    assert "same backend" in response.data["error"]
    assert get_thread_model_override("codex-1") is None


def test_unknown_thread_is_404(monkeypatch) -> None:
    manager = FakeManager(_threads())
    monkeypatch.setattr(thread_models, "get_session_manager", lambda: manager)

    response = thread_models.thread_model_settings(
        _request("get", "/api/threads/nope/models/"), "nope"
    )
    assert response.status_code == 404


# --- model in turn payloads --------------------------------------------------


def test_start_turn_with_model_switches_and_sticks(monkeypatch) -> None:
    manager = FakeManager(_threads())
    monkeypatch.setattr(thread_views, "get_session_manager", lambda: manager)

    response = thread_views.thread_start_turn(
        _request(
            "post",
            "/api/threads/claude-1/turns/",
            {"prompt": "hi", "model": "CLAUDE-SONNET-5"},
        ),
        "claude-1",
    )
    assert response.status_code == 201, response.data
    assert manager.turns == [("claude-1", "hi", "claude-sonnet-5")]
    assert get_thread_model_override("claude-1") == "claude-sonnet-5"

    # A later turn without a model keeps the stored override (resolved by the
    # session manager, so the view passes None through).
    response = thread_views.thread_queue_turn(
        _request("post", "/api/threads/claude-1/turns/queue/", {"prompt": "more"}),
        "claude-1",
    )
    assert response.status_code == 202, response.data
    assert manager.turns[-1] == ("claude-1", "more", None)
    assert get_thread_model_override("claude-1") == "claude-sonnet-5"


def test_start_turn_with_cross_backend_model_is_400(monkeypatch) -> None:
    manager = FakeManager(_threads())
    monkeypatch.setattr(thread_views, "get_session_manager", lambda: manager)
    set_thread_model_override("codex-1", "gpt-5.6-terra")

    response = thread_views.thread_start_turn(
        _request(
            "post",
            "/api/threads/codex-1/turns/",
            {"prompt": "hi", "model": "claude-fable-5-1"},
        ),
        "codex-1",
    )
    assert response.status_code == 400
    assert "same backend" in response.data["error"]
    assert manager.turns == []
    assert get_thread_model_override("codex-1") == "gpt-5.6-terra"


@pytest.mark.parametrize(
    "model", ["claude-sonnet-5", "claude-opus-5-5", "claude-fable-5-1"]
)
def test_trial_catalog_disables_paid_models_and_rejects_switch_and_create(
    monkeypatch, model
):
    from openbase_coder_cli import cloud_models

    reason = "Requires a paid plan."
    monkeypatch.setattr(
        cloud_models,
        "cloud_model_availability",
        lambda: {
            "claude-haiku-4-5-20251001": None,
            "claude-sonnet-5": reason,
            "claude-opus-5-5": reason,
            "claude-fable-5-1": reason,
        },
    )
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "openbase_cloud")
    manager = FakeManager(
        [
            ThreadInfo(
                session_id="cloud-1",
                directory="/tmp/p",
                backend="openbase_cloud",
                model="claude-haiku-4-5-20251001",
            )
        ]
    )
    monkeypatch.setattr(thread_models, "get_session_manager", lambda: manager)
    response = thread_models.thread_model_settings(
        _request("get", "/api/threads/cloud-1/models/"), "cloud-1"
    )
    options = {option["id"]: option for option in response.data["options"]}
    assert options[model]["available"] is False
    assert options[model]["unavailable_reason"] == reason
    assert options["claude-haiku-4-5-20251001"]["available"] is True
    rejected = thread_models.thread_model_settings(
        _request("put", "/api/threads/cloud-1/models/", {"model": model}), "cloud-1"
    )
    assert rejected.status_code == 400
    assert rejected.data["error"] == reason
    assert get_thread_model_override("cloud-1") is None
    with pytest.raises(ValueError, match="Requires a paid plan"):
        thread_views._resolve_new_thread_model(manager, model, "openbase_cloud")
    assert (
        validate_model_for_thread("openbase_cloud", "claude-haiku-4-5-20251001")
        == "claude-haiku-4-5-20251001"
    )


@pytest.mark.parametrize(
    "model", ["gpt-5.5", "gpt-5", "sol", "astra", "claude-opus-4-8", "claude-fable-5"]
)
def test_legacy_thread_keeps_its_model_without_offering_it(monkeypatch, model):
    from super_agents.backend_config import resolve_model

    _, backend = resolve_model(model)
    manager = FakeManager(
        [
            ThreadInfo(
                session_id="legacy", directory="/tmp/p", backend=backend, model=model
            )
        ]
    )
    monkeypatch.setattr(thread_models, "get_session_manager", lambda: manager)
    monkeypatch.setattr(thread_views, "get_session_manager", lambda: manager)
    set_thread_model_override("legacy", model)
    response = thread_models.thread_model_settings(
        _request("get", "/api/threads/legacy/models/"), "legacy"
    )
    assert response.data["model"] == model
    assert model not in {option["id"] for option in response.data["options"]}
    response = thread_views.thread_start_turn(
        _request("post", "/api/threads/legacy/turns/", {"prompt": "continue"}), "legacy"
    )
    assert response.status_code == 201
    assert get_thread_model_override("legacy") == model

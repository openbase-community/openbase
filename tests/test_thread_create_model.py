from __future__ import annotations

# ruff: noqa: E402, I001

import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

from openbase_coder_cli import dispatcher_config
from openbase_coder_cli.openbase_coder_cli_app import threads as thread_views
from openbase_coder_cli.thread_sync.models import ThreadInfo


OPTIONS = (
    {"id": "fable", "label": "Claude Fable 5", "engine": "claude", "available": True},
    {"id": "gpt-5.5", "label": "GPT-5.5", "engine": "codex", "available": True},
    {"id": "gpt-5", "label": "GPT-5", "engine": "codex", "available": False},
)


class FakeManager:
    def __init__(self, execution_backend: str = "claude_code") -> None:
        self._execution_backend = execution_backend
        self.created: list[dict] = []

    async def create_thread(self, directory: str, backend: str | None = None):
        self.created.append({"directory": directory, "backend": backend})
        return ThreadInfo(
            session_id="new-1",
            directory=directory,
            backend=backend or self._execution_backend,
        )


class FakeMixedManager(FakeManager):
    def __init__(self, backends: set[str]) -> None:
        super().__init__("claude_code")
        self._backends = backends

    def manager_for_backend(self, backend: str):
        return self if backend in self._backends else None


def _post(monkeypatch, manager, body: dict):
    overrides: dict[str, str] = {}
    monkeypatch.setattr(thread_views, "get_session_manager", lambda: manager)
    monkeypatch.setattr(thread_views, "set_thread_origin", lambda *_: None)
    monkeypatch.setattr(thread_views, "invalidate_thread_list_cache", lambda: None)
    monkeypatch.setattr(
        thread_views,
        "set_thread_model_override",
        lambda thread_id, model: overrides.__setitem__(thread_id, model),
    )
    monkeypatch.setattr(dispatcher_config, "backend_location", lambda *_: "local")
    monkeypatch.setattr(dispatcher_config, "combined_model_options", lambda _location: OPTIONS)
    monkeypatch.setattr(
        dispatcher_config,
        "model_engine",
        lambda model: "codex" if model.startswith("gpt") else "claude",
    )
    request = APIRequestFactory().post("/api/threads/", body, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return thread_views.thread_list(request), overrides


def test_create_without_model_is_unchanged(monkeypatch) -> None:
    manager = FakeManager()
    response, overrides = _post(monkeypatch, manager, {"directory": "/w/app"})
    assert response.status_code == 201
    assert response.data["model"] is None
    assert manager.created == [{"directory": "/w/app", "backend": None}]
    assert overrides == {}


def test_create_with_same_engine_model_stores_override(monkeypatch) -> None:
    manager = FakeManager("claude_code")
    response, overrides = _post(monkeypatch, manager, {"directory": "/w/app", "model": "fable"})
    assert response.status_code == 201
    assert response.data["model"] == "fable"
    assert manager.created == [{"directory": "/w/app", "backend": None}]
    assert overrides == {"new-1": "fable"}


def test_mixed_install_routes_model_to_its_backend(monkeypatch) -> None:
    manager = FakeMixedManager({"claude_code", "codex"})
    response, overrides = _post(monkeypatch, manager, {"directory": "/w/app", "model": "gpt-5.5"})
    assert response.status_code == 201
    assert manager.created == [{"directory": "/w/app", "backend": "codex"}]
    assert overrides == {"new-1": "gpt-5.5"}


def test_model_for_an_engine_that_cannot_run_here_is_rejected(monkeypatch) -> None:
    manager = FakeManager("claude_code")
    response, overrides = _post(monkeypatch, manager, {"directory": "/w/app", "model": "gpt-5.5"})
    assert response.status_code == 400
    assert "not available on this computer" in response.data["error"]
    assert manager.created == []
    assert overrides == {}


def test_unknown_or_unavailable_model_is_rejected_with_choices(monkeypatch) -> None:
    for model in ("made-up", "gpt-5"):
        manager = FakeManager("claude_code")
        response, _ = _post(monkeypatch, manager, {"directory": "/w/app", "model": model})
        assert response.status_code == 400
        assert "fable" in response.data["error"]
        assert manager.created == []


def test_blank_model_is_rejected(monkeypatch) -> None:
    manager = FakeManager()
    response, _ = _post(monkeypatch, manager, {"directory": "/w/app", "model": "  "})
    assert response.status_code == 400
    assert manager.created == []

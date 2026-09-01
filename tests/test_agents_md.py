from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace


def _setup_django():
    os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings"
    )

    import django

    django.setup()


def test_agents_md_lists_all_instruction_targets(tmp_path: Path, monkeypatch) -> None:
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import views

    instructions_dir = tmp_path / "openbase" / "instructions"
    codex_home = tmp_path / "codex"
    openbase_agents = instructions_dir / "AGENTS.md"
    normal_agents = codex_home / "AGENTS.md"
    direct_instructions = instructions_dir / "VOICE_INSTRUCTIONS.md"
    dispatcher_instructions = instructions_dir / "DISPATCHER_INSTRUCTIONS.md"
    super_agent_instructions = instructions_dir / "SUPER_AGENT_INSTRUCTIONS.md"
    instructions_dir.mkdir(parents=True)
    openbase_agents.write_text("openbase instructions", encoding="utf-8")
    direct_instructions.write_text("direct instructions", encoding="utf-8")
    super_agent_instructions.write_text("super instructions", encoding="utf-8")

    monkeypatch.setattr(views, "CODEX_HOME_DIR", codex_home)
    monkeypatch.setattr(views, "CODEX_AGENTS_MD_PATH", normal_agents)
    monkeypatch.setattr(views, "OPENBASE_AGENTS_MD_PATH", openbase_agents)
    monkeypatch.setattr(views, "OPENBASE_INSTRUCTIONS_DIR", instructions_dir)
    monkeypatch.setattr(
        views, "CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH", direct_instructions
    )
    monkeypatch.setattr(
        views, "CODEX_DISPATCHER_INSTRUCTIONS_PATH", dispatcher_instructions
    )
    monkeypatch.setattr(
        views, "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", super_agent_instructions
    )
    monkeypatch.delenv(
        "LIVEKIT_DIRECT_CODEX_DEVELOPER_INSTRUCTIONS_PATH", raising=False
    )
    monkeypatch.delenv("CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", raising=False)

    factory = APIRequestFactory()
    request = factory.get("/api/agents-md/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = views.agents_md(request)

    assert response.status_code == 200
    assert response.data["content"] == "openbase instructions"
    documents = {document["id"]: document for document in response.data["documents"]}
    assert list(documents) == [
        "openbase",
        "normal",
        "direct_livekit",
        "super_agent",
        "dispatcher",
    ]
    assert documents["openbase"]["content"] == "openbase instructions"
    assert documents["openbase"]["exists"] is True
    assert documents["normal"]["content"] == ""
    assert documents["normal"]["exists"] is False
    assert documents["normal"]["path"] == str(normal_agents)
    assert documents["direct_livekit"]["content"] == "direct instructions"
    assert documents["direct_livekit"]["exists"] is True
    assert documents["super_agent"]["label"] == "Super Agent instructions"
    assert documents["super_agent"]["content"] == "super instructions"
    assert documents["super_agent"]["exists"] is True
    assert documents["dispatcher"]["content"] == ""
    assert documents["dispatcher"]["exists"] is False
    assert documents["dispatcher"]["path"] == str(dispatcher_instructions)


def test_agents_md_lists_super_agent_target_when_file_is_absent(
    tmp_path: Path, monkeypatch
) -> None:
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import views

    instructions_dir = tmp_path / "openbase" / "instructions"
    super_agent_instructions = instructions_dir / "SUPER_AGENT_INSTRUCTIONS.md"

    monkeypatch.setattr(views, "CODEX_HOME_DIR", tmp_path / "codex")
    monkeypatch.setattr(views, "CODEX_AGENTS_MD_PATH", tmp_path / "codex" / "AGENTS.md")
    monkeypatch.setattr(views, "OPENBASE_AGENTS_MD_PATH", instructions_dir / "AGENTS.md")
    monkeypatch.setattr(views, "OPENBASE_INSTRUCTIONS_DIR", instructions_dir)
    monkeypatch.setattr(
        views,
        "CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH",
        instructions_dir / "VOICE_INSTRUCTIONS.md",
    )
    monkeypatch.setattr(
        views, "CODEX_DISPATCHER_INSTRUCTIONS_PATH", instructions_dir / "dispatcher.md"
    )
    monkeypatch.setattr(
        views, "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", super_agent_instructions
    )
    monkeypatch.delenv(
        "LIVEKIT_DIRECT_CODEX_DEVELOPER_INSTRUCTIONS_PATH", raising=False
    )
    monkeypatch.delenv("CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", raising=False)

    factory = APIRequestFactory()
    request = factory.get("/api/agents-md/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = views.agents_md(request)

    assert response.status_code == 200
    documents = {document["id"]: document for document in response.data["documents"]}
    assert documents["super_agent"]["label"] == "Super Agent instructions"
    assert documents["super_agent"]["path"] == str(super_agent_instructions)
    assert documents["super_agent"]["content"] == ""
    assert documents["super_agent"]["exists"] is False


def test_agents_md_put_creates_normal_codex_home_file(
    tmp_path: Path, monkeypatch
) -> None:
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import views

    instructions_dir = tmp_path / "openbase" / "instructions"
    codex_home = tmp_path / "codex"
    openbase_agents = instructions_dir / "AGENTS.md"
    normal_agents = codex_home / "AGENTS.md"
    direct_instructions = instructions_dir / "VOICE_INSTRUCTIONS.md"
    dispatcher_instructions = instructions_dir / "DISPATCHER_INSTRUCTIONS.md"
    super_agent_instructions = instructions_dir / "SUPER_AGENT_INSTRUCTIONS.md"

    monkeypatch.setattr(views, "CODEX_HOME_DIR", codex_home)
    monkeypatch.setattr(views, "CODEX_AGENTS_MD_PATH", normal_agents)
    monkeypatch.setattr(views, "OPENBASE_AGENTS_MD_PATH", openbase_agents)
    monkeypatch.setattr(views, "OPENBASE_INSTRUCTIONS_DIR", instructions_dir)
    monkeypatch.setattr(
        views, "CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH", direct_instructions
    )
    monkeypatch.setattr(
        views, "CODEX_DISPATCHER_INSTRUCTIONS_PATH", dispatcher_instructions
    )
    monkeypatch.setattr(
        views, "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", super_agent_instructions
    )
    monkeypatch.delenv(
        "LIVEKIT_DIRECT_CODEX_DEVELOPER_INSTRUCTIONS_PATH", raising=False
    )
    monkeypatch.delenv("CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", raising=False)

    factory = APIRequestFactory()
    request = factory.put(
        "/api/agents-md/",
        {"target": "normal", "content": "normal instructions"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = views.agents_md(request)

    assert response.status_code == 200
    assert response.data["id"] == "normal"
    assert response.data["exists"] is True
    assert response.data["content"] == "normal instructions"
    assert normal_agents.read_text(encoding="utf-8") == "normal instructions"
    assert not openbase_agents.exists()


def test_agents_md_put_creates_dispatcher_instruction_file(
    tmp_path: Path, monkeypatch
) -> None:
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import views

    dispatcher_instructions = tmp_path / "dispatcher.md"
    monkeypatch.setattr(
        views, "CODEX_DISPATCHER_INSTRUCTIONS_PATH", dispatcher_instructions
    )
    monkeypatch.setenv(
        "LIVEKIT_DISPATCHER_INSTRUCTIONS_PATH",
        str(tmp_path / "ignored-dispatcher.md"),
    )

    factory = APIRequestFactory()
    request = factory.put(
        "/api/agents-md/",
        {"target": "dispatcher", "content": "dispatcher instructions"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = views.agents_md(request)

    assert response.status_code == 200
    assert response.data["id"] == "dispatcher"
    assert response.data["path"] == str(dispatcher_instructions)
    assert dispatcher_instructions.read_text(encoding="utf-8") == (
        "dispatcher instructions"
    )
    assert not (tmp_path / "ignored-dispatcher.md").exists()


def test_agents_md_put_creates_super_agent_instruction_file(
    tmp_path: Path, monkeypatch
) -> None:
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import views

    super_agent_instructions = tmp_path / "super-agent.md"
    monkeypatch.setenv(
        "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", str(super_agent_instructions)
    )

    factory = APIRequestFactory()
    request = factory.put(
        "/api/agents-md/",
        {"target": "super_agent", "content": "super agent instructions"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = views.agents_md(request)

    assert response.status_code == 200
    assert response.data["id"] == "super_agent"
    assert response.data["label"] == "Super Agent instructions"
    assert response.data["path"] == str(super_agent_instructions)
    assert super_agent_instructions.read_text(encoding="utf-8") == (
        "super agent instructions"
    )

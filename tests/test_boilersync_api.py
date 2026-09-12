from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from openbase_coder_cli.services import boilersync


def _setup_django() -> None:
    os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings"
    )

    import django

    django.setup()


def _authenticated_request(method: str, payload: dict | None = None):
    from rest_framework.test import APIRequestFactory, force_authenticate

    factory = APIRequestFactory()
    request = getattr(factory, method.lower())(
        "/api/boilersync/templates/", payload or {}, format="json"
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def test_templates_payload_offers_featured_source_when_cache_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    from openbase_coder_cli.services import console_settings

    monkeypatch.setattr(
        console_settings,
        "CONSOLE_SETTINGS_JSON_PATH",
        tmp_path / "console-settings.json",
    )
    monkeypatch.setattr(boilersync, "resolve_boilersync_binary", lambda: "/bin/tool")
    monkeypatch.setattr(
        boilersync,
        "run_boilersync_json",
        lambda _binary, *args: {
            "payload": (
                {"template_root_dir": str(tmp_path), "sources": []}
                if args[1] == "sources"
                else {"template_root_dir": str(tmp_path), "templates": []}
            ),
            "error": None,
        },
    )

    payload = boilersync.boilersync_templates_payload()

    assert payload["featured_source"] == {
        "org": "openbase-community",
        "repo": "templates",
        "repo_url": "https://github.com/openbase-community/templates.git",
        "installed": False,
        "prompt_dismissed": False,
        "prompt_visible": True,
    }


def test_templates_payload_hides_featured_source_when_already_installed(
    tmp_path: Path, monkeypatch
) -> None:
    from openbase_coder_cli.services import console_settings

    monkeypatch.setattr(
        console_settings,
        "CONSOLE_SETTINGS_JSON_PATH",
        tmp_path / "console-settings.json",
    )
    monkeypatch.setattr(boilersync, "resolve_boilersync_binary", lambda: "/bin/tool")
    monkeypatch.setattr(
        boilersync,
        "run_boilersync_json",
        lambda _binary, *args: {
            "payload": (
                {
                    "template_root_dir": str(tmp_path),
                    "sources": [{"org": "openbase-community", "repo": "templates"}],
                }
                if args[1] == "sources"
                else {"template_root_dir": str(tmp_path), "templates": []}
            ),
            "error": None,
        },
    )

    payload = boilersync.boilersync_templates_payload()

    assert payload["featured_source"]["installed"] is True
    assert payload["featured_source"]["prompt_visible"] is False


def test_add_source_invokes_non_interactive_boilersync_init(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(boilersync, "resolve_boilersync_binary", lambda: "/bin/tool")
    monkeypatch.setattr(
        boilersync.subprocess,
        "run",
        lambda args, **kwargs: (
            calls.append((args, kwargs))
            or subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")
        ),
    )

    boilersync.add_boilersync_source("https://github.com/example/templates.git")

    assert calls[0][0] == [
        "/bin/tool",
        "templates",
        "init",
        "https://github.com/example/templates.git",
        "--no-input",
    ]
    assert calls[0][1]["timeout"] == boilersync.BOILERSYNC_CLONE_TIMEOUT_SECONDS


def test_remove_source_deletes_only_the_listed_cache_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    source_path = tmp_path / "example" / "templates"
    (source_path / ".git").mkdir(parents=True)
    (source_path / "template.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(boilersync, "resolve_boilersync_binary", lambda: "/bin/tool")
    monkeypatch.setattr(
        boilersync,
        "run_boilersync_json",
        lambda *_args: {
            "payload": {
                "template_root_dir": str(tmp_path),
                "sources": [
                    {
                        "org": "example",
                        "repo": "templates",
                        "path": str(source_path),
                    }
                ],
            },
            "error": None,
        },
    )

    boilersync.remove_boilersync_source("example", "templates")

    assert not source_path.exists()
    assert not source_path.parent.exists()


def test_remove_source_accepts_an_equivalent_symlinked_cache_root(
    tmp_path: Path, monkeypatch
) -> None:
    real_root = tmp_path / "private" / "cache"
    source_path = real_root / "example" / "templates"
    (source_path / ".git").mkdir(parents=True)
    linked_root = tmp_path / "cache-link"
    linked_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setattr(boilersync, "resolve_boilersync_binary", lambda: "/bin/tool")
    monkeypatch.setattr(
        boilersync,
        "run_boilersync_json",
        lambda *_args: {
            "payload": {
                "template_root_dir": str(linked_root),
                "sources": [
                    {
                        "org": "example",
                        "repo": "templates",
                        "path": str(linked_root / "example" / "templates"),
                    }
                ],
            },
            "error": None,
        },
    )

    boilersync.remove_boilersync_source("example", "templates")

    assert not source_path.exists()


def test_featured_prompt_can_be_restored_from_settings(
    tmp_path: Path, monkeypatch
) -> None:
    _setup_django()

    from openbase_coder_cli.openbase_coder_cli_app import plugins_tools
    from openbase_coder_cli.services import console_settings

    monkeypatch.setattr(
        console_settings,
        "CONSOLE_SETTINGS_JSON_PATH",
        tmp_path / "console-settings.json",
    )
    monkeypatch.setattr(
        plugins_tools,
        "boilersync_templates_payload",
        lambda: {"featured_source": {"prompt_dismissed": False}},
    )
    console_settings.set_featured_template_prompt_dismissed(True)

    response = plugins_tools.boilersync_templates(
        _authenticated_request("PATCH", {"dismissed": False})
    )

    assert response.status_code == 200
    assert console_settings.get_featured_template_prompt_dismissed() is False

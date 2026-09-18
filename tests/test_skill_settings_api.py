from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import skill_settings  # noqa: E402


def request(data):
    req = APIRequestFactory().patch("/api/skills/settings/", data, format="json")
    force_authenticate(req, user=SimpleNamespace(is_authenticated=True))
    return req


def test_rejects_non_boolean_settings_before_any_mutation(monkeypatch):
    def must_not_write(*args):
        raise AssertionError("Invalid request must not change preferences")

    monkeypatch.setattr(skill_settings.skills_sync, "set_enabled", must_not_write)
    monkeypatch.setattr(
        skill_settings.dispatcher_config,
        "set_auto_link_personal_skills",
        must_not_write,
    )
    for data in [
        {"sync_skills_across_devices": "false"},
        {"auto_link_personal_skills": 0},
        {"unknown": True},
        {},
    ]:
        assert skill_settings.skill_sharing_settings(request(data)).status_code == 400


def test_disabling_device_sync_does_not_change_backend_linking(monkeypatch):
    changes = []
    monkeypatch.setattr(
        skill_settings.skills_sync, "set_enabled", lambda value: changes.append(value)
    )
    monkeypatch.setattr(
        skill_settings.skills_sync,
        "settings_payload",
        lambda: {"sync_skills_across_devices": False},
    )
    monkeypatch.setattr(
        skill_settings.dispatcher_config, "auto_link_personal_skills", lambda: True
    )
    response = skill_settings.skill_sharing_settings(
        request({"sync_skills_across_devices": False})
    )
    assert response.status_code == 200
    assert response.data["auto_link_personal_skills"] is True
    assert changes == [False]


def test_device_sync_failure_returns_actionable_error(monkeypatch):
    from openbase_coder_cli.code_sync import CodeSyncError

    def fail(value):
        raise CodeSyncError("Add a second machine to enable sync.")

    monkeypatch.setattr(skill_settings.skills_sync, "set_enabled", fail)
    response = skill_settings.skill_sharing_settings(
        request({"sync_skills_across_devices": True})
    )
    assert response.status_code == 400
    assert "second machine" in str(response.data)

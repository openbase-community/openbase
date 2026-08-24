from __future__ import annotations

import json
import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import diagnostics, views  # noqa: E402


def _upload_ios_logs(payload: dict):
    request = APIRequestFactory().post(
        "/api/diagnostics/ios-logs/",
        payload,
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return views.ios_logs_upload(request)


def _ios_log_entry(index: int) -> dict:
    return {
        "timestamp": "2026-05-23T16:00:00Z",
        "component": "AuthDiagnostics",
        "message": f"request complete {index}",
        "metadata": {"status": "200"},
        "line": f"[AuthDiagnostics][AuthDiagnostics] request complete {index} status=200",
    }


def test_ios_logs_upload_appends_jsonl_to_openbase_logs(monkeypatch, tmp_path) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(views, "DEFAULT_LOG_DIR", log_dir)

    response = _upload_ios_logs(
        {
            "device": {
                "model": "iPhone",
                "name": "Gabe's iPhone",
                "system_version": "18.5",
            },
            "entries": [
                {
                    **_ios_log_entry(0),
                    "message": "request complete for gabe@example.com",
                    "metadata": {
                        "authorization": "Bearer abcdefghijklmnop",
                        "status": "200",
                    },
                    "line": "[AuthDiagnostics][AuthDiagnostics] request complete authorization=Bearer abcdefghijklmnop email=gabe@example.com",
                }
            ],
        }
    )

    assert response.status_code == 201
    assert response.data["uploaded_count"] == 1
    assert response.data["log_path"] == str(log_dir / "ios-app.log")

    lines = (log_dir / "ios-app.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["source"] == "ios"
    assert payload["device"]["model"] == "iPhone"
    assert payload["device"]["name"] == diagnostics.REDACTED_VALUE
    assert payload["entry"]["component"] == "AuthDiagnostics"
    assert payload["entry"]["metadata"] == {
        "authorization": diagnostics.REDACTED_VALUE,
        "status": "200",
    }
    assert "gabe@example.com" not in payload["entry"]["message"]
    assert "abcdefghijklmnop" not in payload["entry"]["line"]
    assert diagnostics.REDACTED_EMAIL_VALUE in payload["entry"]["message"]


def test_ios_logs_upload_rejects_empty_entries(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(views, "DEFAULT_LOG_DIR", tmp_path / "logs")

    response = _upload_ios_logs({"entries": []})

    assert response.status_code == 400
    assert not (tmp_path / "logs" / "ios-app.log").exists()


def test_ios_logs_upload_accepts_full_retained_buffer(monkeypatch, tmp_path) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(views, "DEFAULT_LOG_DIR", log_dir)

    response = _upload_ios_logs(
        {
            "entries": [
                _ios_log_entry(index)
                for index in range(diagnostics.IOS_LOG_UPLOAD_MAX_ENTRIES)
            ]
        }
    )

    assert response.status_code == 201
    assert response.data["uploaded_count"] == diagnostics.IOS_LOG_UPLOAD_MAX_ENTRIES
    assert (
        len((log_dir / "ios-app.log").read_text(encoding="utf-8").splitlines())
        == diagnostics.IOS_LOG_UPLOAD_MAX_ENTRIES
    )


def test_ios_logs_upload_rejects_entries_over_retained_buffer(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(views, "DEFAULT_LOG_DIR", tmp_path / "logs")

    response = _upload_ios_logs(
        {
            "entries": [
                _ios_log_entry(index)
                for index in range(diagnostics.IOS_LOG_UPLOAD_MAX_ENTRIES + 1)
            ]
        }
    )

    assert response.status_code == 400
    assert not (tmp_path / "logs" / "ios-app.log").exists()


def test_ios_logs_upload_accepts_long_diagnostic_fields(monkeypatch, tmp_path) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(views, "DEFAULT_LOG_DIR", log_dir)
    long_value = ("diagnostic detail " * 500).rstrip()

    response = _upload_ios_logs(
        {
            "entries": [
                {
                    **_ios_log_entry(0),
                    "message": long_value,
                    "metadata": {"error": long_value},
                    "line": f"[Diagnostics] {long_value}",
                }
            ]
        }
    )

    assert response.status_code == 201
    payload = json.loads(
        (log_dir / "ios-app.log").read_text(encoding="utf-8").splitlines()[0]
    )
    assert payload["entry"]["message"] == long_value
    assert payload["entry"]["metadata"]["error"] == long_value


def test_ios_logs_upload_rejects_oversized_batch(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(views, "DEFAULT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(diagnostics, "IOS_LOG_UPLOAD_MAX_BYTES", 100)

    response = _upload_ios_logs({"entries": [_ios_log_entry(0)]})

    assert response.status_code == 400
    assert "2 MiB batch limit" in str(response.data["entries"][0])
    assert not (tmp_path / "logs" / "ios-app.log").exists()

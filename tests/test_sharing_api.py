from __future__ import annotations

import base64
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli import sharing_service  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    sharing as sharing_views,
)
from openbase_coder_cli.services.cloud_registration import (  # noqa: E402
    CloudReportResult,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nfakepixels"


def _request(method: str, path: str, data: dict | None = None, query: str = ""):
    factory = APIRequestFactory()
    request = getattr(factory, method)(f"{path}{query}", data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def _ok(response=None, status_code=200) -> CloudReportResult:
    return CloudReportResult(
        ok=True, supported=True, status_code=status_code, response=response
    )


def _project_with_report(tmp_path: Path, content: str) -> tuple[str, str]:
    reports_dir = tmp_path / ".reports"
    (reports_dir / "images").mkdir(parents=True)
    (reports_dir / "2026-09-12-demo.md").write_text(content, encoding="utf-8")
    (reports_dir / "images" / "chart.png").write_bytes(PNG_BYTES)
    return str(tmp_path), "2026-09-12-demo.md"


def _reset_cache():
    with sharing_service._cache_lock:
        sharing_service._shared_origin_keys.clear()


class TestOriginKey:
    def test_matches_across_input_spellings(self, tmp_path):
        base = sharing_service.report_origin_key(str(tmp_path), "a.md")
        assert base == sharing_service.report_origin_key(
            f"{tmp_path}{os.sep}", "./a.md"
        )
        assert base != sharing_service.report_origin_key(str(tmp_path), "b.md")


class TestAssetCollection:
    def test_collects_referenced_images_only(self, tmp_path):
        content = (
            "# Demo\n"
            "![chart](images/chart.png)\n"
            "![remote](https://example.com/x.png)\n"
            "![missing](images/nope.png)\n"
            "![escape](../../etc/passwd)\n"
        )
        project, name = _project_with_report(tmp_path, content)
        assets = sharing_service.collect_report_assets(project, name, content)
        assert [asset["path"] for asset in assets] == ["images/chart.png"]
        assert assets[0]["content_type"] == "image/png"
        assert base64.b64decode(assets[0]["body_base64"]) == PNG_BYTES

    def test_dedupes_repeated_references(self, tmp_path):
        content = "![a](images/chart.png)\n![b](images/chart.png)\n"
        project, name = _project_with_report(tmp_path, content)
        assets = sharing_service.collect_report_assets(project, name, content)
        assert len(assets) == 1


class TestPublishPayload:
    def test_builds_payload_with_title_and_assets(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sharing_service, "local_device_id", lambda: "device-test")
        content = "# Demo Report\n\n![chart](images/chart.png)\n"
        project, name = _project_with_report(tmp_path, content)
        payload = sharing_service.build_publish_payload(project, name)
        assert payload["kind"] == "report"
        assert payload["title"] == "Demo Report"
        assert payload["origin_item_path"] == name
        assert payload["origin_device_id"] == "device-test"
        assert len(payload["assets"]) == 1


class TestShareViews:
    def test_share_state_not_shared(self, tmp_path, monkeypatch):
        _reset_cache()
        project, name = _project_with_report(tmp_path, "# Demo\n")
        monkeypatch.setattr(
            sharing_service.cloud_sharing, "list_items", lambda **_: _ok([])
        )
        response = sharing_views.report_share(
            _request(
                "get",
                "/api/projects/reports/share/",
                query=f"?path={project}&file={name}",
            )
        )
        assert response.status_code == 200
        assert response.data == {"shared": False}

    def test_share_publish_and_state(self, tmp_path, monkeypatch):
        _reset_cache()
        project, name = _project_with_report(tmp_path, "# Demo\n")
        published = {}

        def fake_publish(payload):
            published.update(payload)
            return _ok({"id": "it_1", "revision_seq": 1}, status_code=201)

        item = {
            "id": "it_1",
            "kind": "report",
            "title": "Demo",
            "origin_project_path": project,
            "origin_item_path": name,
            "latest_revision_seq": 1,
        }
        monkeypatch.setattr(sharing_service.cloud_sharing, "publish_item", fake_publish)
        monkeypatch.setattr(
            sharing_service.cloud_sharing, "list_items", lambda **_: _ok([item])
        )
        monkeypatch.setattr(
            sharing_service.cloud_sharing,
            "list_grants",
            lambda item_id: _ok({"grants": []}),
        )
        monkeypatch.setattr(sharing_service, "local_device_id", lambda: "device-test")
        response = sharing_views.report_share(
            _request(
                "post",
                "/api/projects/reports/share/",
                {"path": project, "file": name},
            )
        )
        assert response.status_code == 200
        assert response.data["shared"] is True
        assert response.data["item"]["id"] == "it_1"
        assert published["origin_item_path"] == name

    def test_share_publish_missing_file(self, tmp_path):
        response = sharing_views.report_share(
            _request(
                "post",
                "/api/projects/reports/share/",
                {"path": str(tmp_path), "file": "nope.md"},
            )
        )
        assert response.status_code == 404

    def test_grant_requires_share(self, tmp_path, monkeypatch):
        _reset_cache()
        project, name = _project_with_report(tmp_path, "# Demo\n")
        monkeypatch.setattr(
            sharing_service.cloud_sharing, "list_items", lambda **_: _ok([])
        )
        response = sharing_views.report_share_grants(
            _request(
                "post",
                "/api/projects/reports/share/grants/",
                {"path": project, "file": name, "email": "friend@example.com"},
            )
        )
        assert response.status_code == 409

    def test_unshare(self, tmp_path, monkeypatch):
        _reset_cache()
        project, name = _project_with_report(tmp_path, "# Demo\n")
        item = {
            "id": "it_1",
            "origin_project_path": project,
            "origin_item_path": name,
        }
        monkeypatch.setattr(
            sharing_service.cloud_sharing, "list_items", lambda **_: _ok([item])
        )
        monkeypatch.setattr(
            sharing_service.cloud_sharing,
            "list_grants",
            lambda item_id: _ok({"grants": []}),
        )
        deleted = {}

        def fake_delete(item_id):
            deleted["id"] = item_id
            return _ok({})

        monkeypatch.setattr(sharing_service.cloud_sharing, "delete_item", fake_delete)
        response = sharing_views.report_share(
            _request(
                "delete",
                "/api/projects/reports/share/",
                query=f"?path={project}&file={name}",
            )
        )
        assert response.status_code == 200
        assert deleted["id"] == "it_1"


class TestMaybeRepublish:
    def test_republishes_only_known_shares(self, tmp_path, monkeypatch):
        _reset_cache()
        project, name = _project_with_report(tmp_path, "# Demo\n")
        calls = []
        monkeypatch.setattr(
            sharing_service,
            "publish_report",
            lambda *args: calls.append(args),
        )
        sharing_service.maybe_republish_report(project, name)
        assert calls == []

        key = sharing_service.report_origin_key(project, name)
        with sharing_service._cache_lock:
            sharing_service._shared_origin_keys.add(key)
        sharing_service.maybe_republish_report(project, name)
        assert len(calls) == 1


class TestSecretScan:
    def test_detects_common_secret_shapes(self):
        content = (
            "# Report\n"
            "aws AKIAABCDEFGHIJKLMNOP here\n"
            "clean line\n"
            "api_key = 'abcdefghijklmnop1234'\n"
        )
        findings = sharing_service.scan_for_secrets(content)
        assert [f["line"] for f in findings] == [2, 4]
        assert findings[0]["rule"] == "aws-access-key-id"

    def test_clean_content_has_no_findings(self):
        assert sharing_service.scan_for_secrets("# Weekly\nAll fine.\n") == []

    def test_publish_blocks_secrets_without_confirm(self, tmp_path, monkeypatch):
        _reset_cache()
        content = "# R\ntoken = 'abcdefghijklmnop1234'\n"
        project, name = _project_with_report(tmp_path, content)
        monkeypatch.setattr(
            sharing_service.cloud_sharing,
            "publish_item",
            lambda payload: (_ for _ in ()).throw(AssertionError("must not publish")),
        )
        result = sharing_service.publish_report(project, name)
        assert result["ok"] is False
        assert result["reason"] == "possible_secrets"

    def test_share_view_returns_409_with_findings(self, tmp_path):
        _reset_cache()
        content = "# R\ntoken = 'abcdefghijklmnop1234'\n"
        project, name = _project_with_report(tmp_path, content)
        response = sharing_views.report_share(
            _request(
                "post",
                "/api/projects/reports/share/",
                {"path": project, "file": name},
            )
        )
        assert response.status_code == 409
        assert response.data["reason"] == "possible_secrets"
        assert response.data["findings"]

    def test_confirm_secrets_publishes(self, tmp_path, monkeypatch):
        _reset_cache()
        content = "# R\ntoken = 'abcdefghijklmnop1234'\n"
        project, name = _project_with_report(tmp_path, content)
        monkeypatch.setattr(
            sharing_service, "local_device_id", lambda: "device-test"
        )
        monkeypatch.setattr(
            sharing_service.cloud_sharing,
            "publish_item",
            lambda payload: _ok({"id": "it_1", "revision_seq": 1}, status_code=201),
        )
        result = sharing_service.publish_report(project, name, allow_secrets=True)
        assert result["ok"] is True


class TestSweepEfficiency:
    def test_sweep_skips_unchanged_content(self, tmp_path, monkeypatch):
        _reset_cache()
        content = "# Demo\n"
        project, name = _project_with_report(tmp_path, content)
        item = {
            "id": "it_1",
            "origin_project_path": project,
            "origin_item_path": name,
        }
        publishes = []
        monkeypatch.setattr(
            sharing_service, "local_device_id", lambda: "device-test"
        )
        monkeypatch.setattr(
            sharing_service.cloud_sharing, "list_items", lambda **_: _ok([item])
        )
        monkeypatch.setattr(
            sharing_service.cloud_sharing,
            "publish_item",
            lambda payload: publishes.append(payload)
            or _ok({"id": "it_1", "revision_seq": 1}),
        )
        sharing_service.sync_shared_reports(force=True)
        assert len(publishes) == 1
        sharing_service.sync_shared_reports(force=True)
        assert len(publishes) == 1

        report_file = Path(project) / ".reports" / name
        report_file.write_text("# Demo changed\n", encoding="utf-8")
        sharing_service.sync_shared_reports(force=True)
        assert len(publishes) == 2

    def test_asset_collection_respects_total_budget(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sharing_service, "MAX_SHARE_TOTAL_ASSET_BYTES", len(PNG_BYTES) + 5)
        content = "![a](images/chart.png)\n![b](images/chart2.png)\n"
        project, name = _project_with_report(tmp_path, content)
        chart2 = Path(project) / ".reports" / "images" / "chart2.png"
        chart2.write_bytes(PNG_BYTES)
        assets = sharing_service.collect_report_assets(project, name, content)
        assert len(assets) == 1

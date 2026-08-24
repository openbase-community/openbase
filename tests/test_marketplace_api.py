from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
import pytest  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    marketplace,
    marketplace_install,
)

COMMIT = "a" * 40


def _request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    request = getattr(factory, method)(path, data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def _source(**overrides):
    payload = {
        "repository_url": "https://github.com/openbase/example-skill",
        "commit": COMMIT,
        "path": "skills/example",
        "integrity": None,
    }
    payload.update(overrides)
    return payload


def _entry(**overrides):
    payload = {
        "id": 1,
        "slug": "example-skill",
        "name": "Example Skill",
        "tagline": "A useful example",
        "description": "Longer catalog description.",
        "category": "Developer tools",
        "kind": "skill",
        "docs_url": "https://example.com/docs",
        "install_notes": "Review before installing.",
        "featured": True,
        "featured_rank": 1,
        "install_count": 0,
        "created_at": "2026-08-19T00:00:00Z",
        "updated_at": "2026-08-19T00:00:00Z",
        "source": _source(),
    }
    payload.update(overrides)
    return payload


def _routine_entry(**overrides):
    payload = {
        "id": 9,
        "slug": "daily-review",
        "name": "Daily Review",
        "tagline": "Review active work",
        "description": "A read-only routine template.",
        "category": "Productivity",
        "kind": "agent",
        "prompt": "Review my active work.",
        "command": "",
        "command_timeout_seconds": 300,
        "schedule_type": "daily",
        "time": "09:00:00",
        "interval_seconds": None,
        "use_client_timezone": True,
        "suggested_timezone": "America/New_York",
        "required_skills": ["example-skill"],
        "install_count": 0,
        "created_at": "2026-08-19T00:00:00Z",
        "updated_at": "2026-08-19T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def _patch_skill_roots(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    roots = {
        scope: tmp_path / scope / "skills"
        for scope in marketplace.GLOBAL_SKILL_SCOPES
    }
    monkeypatch.setattr(
        marketplace_install,
        "_skills_dir",
        lambda _project, scope: roots[scope],
    )
    return roots


def _archive(*members: tuple[str, bytes, str | None, int]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        for name, content, link_type, mode in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if link_type == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/tmp/elsewhere"
                info.size = 0
                bundle.addfile(info)
            else:
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
    return output.getvalue()


def test_catalog_proxies_only_validated_pinned_entries(
    monkeypatch, tmp_path: Path
):
    roots = _patch_skill_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(
        marketplace,
        "_fetch_catalog",
        lambda **_kwargs: [_entry()],
    )
    destination = roots["home"] / "example-skill"
    destination.mkdir(parents=True)
    (destination / marketplace_install.INSTALL_METADATA_FILENAME).write_text(
        json.dumps({"source": _source()}), encoding="utf-8"
    )

    response = marketplace.marketplace_skills(
        _request("get", "/api/marketplace/skills/")
    )

    assert response.status_code == 200
    assert response.data["categories"] == [
        {"name": "Developer tools", "count": 1}
    ]
    item = response.data["entries"][0]
    assert item["source"]["commit"] == COMMIT
    assert item["install_count"] == 0
    assert item["installable"] is True
    assert item["installed_targets"]["home"] == "installed"


def test_routine_catalog_is_explicitly_read_only(monkeypatch):
    monkeypatch.setattr(
        marketplace,
        "_fetch_routine_catalog",
        lambda **_kwargs: [_routine_entry()],
    )

    response = marketplace.marketplace_routines(
        _request("get", "/api/marketplace/routines/")
    )

    assert response.status_code == 200
    assert response.data["read_only"] is True
    assert response.data["categories"] == [
        {"name": "Productivity", "count": 1}
    ]
    assert response.data["entries"][0]["required_skills"] == ["example-skill"]


@pytest.mark.parametrize(
    ("entry_overrides", "source"),
    [
        ({}, _source(repository_url="http://github.com/openbase/example-skill")),
        (
            {},
            _source(
                repository_url="https://token@github.com/openbase/example-skill"
            ),
        ),
        (
            {},
            _source(
                repository_url="https://github.com/openbase/example-skill?ref=main"
            ),
        ),
        ({}, _source(commit="main")),
        ({}, _source(path="../example")),
        ({}, _source(path="skills\\example")),
        ({}, _source(integrity="sha256:not-a-digest")),
        ({"docs_url": "javascript:alert(1)"}, _source()),
        ({"docs_url": "https://user@example.com/docs"}, _source()),
    ],
)
def test_catalog_rejects_mutable_or_unsafe_sources_and_links(
    monkeypatch, entry_overrides, source
):
    monkeypatch.setattr(
        marketplace,
        "_fetch_catalog",
        lambda **_kwargs: [_entry(source=source, **entry_overrides)],
    )

    response = marketplace.marketplace_skills(
        _request("get", "/api/marketplace/skills/")
    )

    assert response.status_code == 502


def test_install_rejects_unexpected_client_source_fields():
    response = marketplace.marketplace_skill_install(
        _request(
            "post",
            "/api/marketplace/skills/install/",
            {
                "slug": "example-skill",
                "commit": COMMIT,
                "targets": ["home"],
                "confirmed": True,
                "repository_url": "https://attacker.invalid/repository",
            },
        )
    )

    assert response.status_code == 400
    assert response.data["fields"] == ["repository_url"]


@pytest.mark.parametrize(
    ("entry", "expected_error"),
    [
        (_entry(source=None), "no immutable source"),
        (_entry(kind="mcp", source=None), "Only catalog skills"),
        (_entry(source=_source(commit="b" * 40)), "changed after confirmation"),
    ],
)
def test_install_rechecks_authoritative_catalog_entry(
    monkeypatch, tmp_path: Path, entry, expected_error: str
):
    _patch_skill_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(marketplace, "_fetch_catalog_skill", lambda _slug: entry)

    response = marketplace.marketplace_skill_install(
        _request(
            "post",
            "/api/marketplace/skills/install/",
            {
                "slug": "example-skill",
                "commit": COMMIT,
                "targets": ["home"],
                "confirmed": True,
            },
        )
    )

    assert response.status_code == 422
    assert expected_error in response.data["error"]


def test_install_writes_verified_files_and_provenance(
    monkeypatch, tmp_path: Path
):
    roots = _patch_skill_roots(monkeypatch, tmp_path)
    skill_md = b"# Example\n"
    entry = _entry(
        source=_source(
            integrity=f"sha256:{hashlib.sha256(skill_md).hexdigest()}"
        )
    )
    monkeypatch.setattr(marketplace, "_fetch_catalog_skill", lambda _slug: entry)
    monkeypatch.setattr(
        marketplace,
        "_download_skill_files",
        lambda _source: {
            "SKILL.md": marketplace_install.SkillFile(skill_md, False),
            "scripts/check.sh": marketplace_install.SkillFile(
                b"#!/bin/sh\nexit 0\n", True
            ),
        },
    )

    request_data = {
        "slug": "example-skill",
        "commit": COMMIT,
        "targets": ["home", "codex"],
        "confirmed": True,
    }
    response = marketplace.marketplace_skill_install(
        _request("post", "/api/marketplace/skills/install/", request_data)
    )

    assert response.status_code == 201
    for scope in ("home", "codex"):
        destination = roots[scope] / "example-skill"
        assert (destination / "SKILL.md").read_bytes() == skill_md
        assert (destination / "scripts/check.sh").stat().st_mode & 0o111
        metadata = json.loads(
            (destination / marketplace_install.INSTALL_METADATA_FILENAME).read_text()
        )
        assert metadata["source"]["commit"] == COMMIT

    repeated = marketplace.marketplace_skill_install(
        _request("post", "/api/marketplace/skills/install/", request_data)
    )
    assert repeated.status_code == 200
    assert {item["status"] for item in repeated.data["results"]} == {
        "already_installed"
    }


def test_install_rejects_existing_symlink_without_following_it(
    monkeypatch, tmp_path: Path
):
    roots = _patch_skill_roots(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / marketplace_install.INSTALL_METADATA_FILENAME).write_text(
        json.dumps({"source": _source()}), encoding="utf-8"
    )
    destination = roots["home"] / "example-skill"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        marketplace, "_fetch_catalog_skill", lambda _slug: _entry()
    )

    response = marketplace.marketplace_skill_install(
        _request(
            "post",
            "/api/marketplace/skills/install/",
            {
                "slug": "example-skill",
                "commit": COMMIT,
                "targets": ["home"],
                "confirmed": True,
            },
        )
    )

    assert response.status_code == 409
    assert destination.is_symlink()


@pytest.mark.parametrize(
    "archive",
    [
        _archive(
            ("root/skills/example/SKILL.md", b"# Fine\n", None, 0o644),
            ("root/skills/example/../escape", b"bad", None, 0o644),
        ),
        _archive(
            ("root/skills/example/SKILL.md", b"# Fine\n", None, 0o644),
            ("root/skills/example/link", b"", "symlink", 0o777),
        ),
        _archive(
            ("root/skills/example/SKILL.md", b"# Fine\n", None, 0o644),
            ("root/skills/example/.env", b"TOKEN=secret", None, 0o600),
        ),
        _archive(
            (
                "root/skills/example/SKILL.md",
                b"-----BEGIN OPENSSH " + b"PRIVATE KEY-----",
                None,
                0o644,
            ),
        ),
    ],
)
def test_archive_or_content_validation_rejects_unsafe_skills(archive: bytes):
    source = marketplace_install._catalog_source(_source())
    assert source is not None

    with pytest.raises(marketplace_install.MarketplaceInstallError):
        files = marketplace_install._skill_files_from_archive(archive, source)
        marketplace_install._verify_skill_files(files, source)


def test_install_rolls_back_earlier_targets_when_later_target_fails(
    monkeypatch, tmp_path: Path
):
    roots = _patch_skill_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(
        marketplace, "_fetch_catalog_skill", lambda _slug: _entry()
    )
    monkeypatch.setattr(
        marketplace,
        "_download_skill_files",
        lambda _source: {
            "SKILL.md": marketplace_install.SkillFile(b"# Fine\n", False)
        },
    )
    real_replace = marketplace_install.os.replace
    replace_calls = 0

    def fail_second_replace(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("simulated disk failure")
        real_replace(source, destination)

    monkeypatch.setattr(marketplace_install.os, "replace", fail_second_replace)

    response = marketplace.marketplace_skill_install(
        _request(
            "post",
            "/api/marketplace/skills/install/",
            {
                "slug": "example-skill",
                "commit": COMMIT,
                "targets": ["home", "codex"],
                "confirmed": True,
            },
        )
    )

    assert response.status_code == 500
    assert not (roots["home"] / "example-skill").exists()
    assert not (roots["codex"] / "example-skill").exists()

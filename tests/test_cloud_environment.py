from __future__ import annotations

from pathlib import Path

from openbase_coder_cli import cloud_environment
from openbase_coder_cli.runtime import RuntimePackage, _package_from_root


def test_default_web_backend_url_tracks_standalone_channel(monkeypatch) -> None:
    monkeypatch.setattr(
        cloud_environment,
        "current_runtime_package",
        lambda: RuntimePackage(root=Path("/package"), channel="staging"),
    )

    assert (
        cloud_environment.default_web_backend_url()
        == "https://app-staging.openbase.cloud"
    )


def test_configured_web_backend_url_preserves_explicit_override(monkeypatch) -> None:
    monkeypatch.setenv("OPENBASE_CODER_CLI_WEB_BACKEND_URL", "https://custom.example/")

    assert cloud_environment.configured_web_backend_url() == "https://custom.example"


def test_runtime_package_reads_release_channel(tmp_path) -> None:
    (tmp_path / "openbase-coder-package.json").write_text(
        '{"version":"1.2.3","target":"arm64","channel":"staging"}\n',
        encoding="utf-8",
    )

    assert _package_from_root(tmp_path) == RuntimePackage(
        root=tmp_path,
        version="1.2.3",
        target="arm64",
        channel="staging",
    )

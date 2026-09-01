from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from openbase_coder_cli.services import tunneld
from openbase_coder_cli.services.installation import InstallationConfig


def test_install_tunneld_binary_builds_dev_source_atomically(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "cli" / "tunneld"
    source.mkdir(parents=True)
    (source / "go.mod").write_text("module example.test/tunneld\n", encoding="utf-8")
    destination_dir = tmp_path / "openbase-bin"
    monkeypatch.setattr(tunneld, "OPENBASE_BIN_DIR", destination_dir)
    monkeypatch.setattr(tunneld, "_go_binary", lambda: "/usr/local/bin/go")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output = Path(command[command.index("-o") + 1])
        output.write_bytes(b"new tunneld")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tunneld.subprocess, "run", fake_run)

    installed = tunneld.install_tunneld_binary(
        InstallationConfig(workspace_path=str(workspace))
    )

    assert installed == destination_dir / "openbase-tunneld"
    assert installed.read_bytes() == b"new tunneld"
    assert os.access(installed, os.X_OK)
    assert calls[0][0][:2] == ["/usr/local/bin/go", "build"]
    assert calls[0][1]["cwd"] == source
    assert not list(destination_dir.glob(".openbase-tunneld-*"))


def test_install_tunneld_binary_preserves_existing_binary_on_build_failure(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "cli" / "tunneld"
    source.mkdir(parents=True)
    (source / "go.mod").write_text("module example.test/tunneld\n", encoding="utf-8")
    destination_dir = tmp_path / "openbase-bin"
    destination_dir.mkdir()
    existing = destination_dir / "openbase-tunneld"
    existing.write_bytes(b"known good")
    existing.chmod(0o755)
    monkeypatch.setattr(tunneld, "OPENBASE_BIN_DIR", destination_dir)
    monkeypatch.setattr(tunneld, "_go_binary", lambda: "/usr/local/bin/go")
    monkeypatch.setattr(
        tunneld.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="compile error"
        ),
    )

    with pytest.raises(RuntimeError, match="build failed: compile error"):
        tunneld.install_tunneld_binary(
            InstallationConfig(workspace_path=str(workspace))
        )

    assert existing.read_bytes() == b"known good"
    assert not list(destination_dir.glob(".openbase-tunneld-*"))


def test_install_tunneld_binary_requires_go_for_dev_source(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "cli" / "tunneld"
    source.mkdir(parents=True)
    (source / "go.mod").write_text("module example.test/tunneld\n", encoding="utf-8")
    monkeypatch.setattr(tunneld, "_go_binary", lambda: None)

    with pytest.raises(RuntimeError, match="Go toolchain is required"):
        tunneld.install_tunneld_binary(
            InstallationConfig(workspace_path=str(workspace))
        )

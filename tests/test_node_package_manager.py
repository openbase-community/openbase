from __future__ import annotations

from pathlib import Path

import pytest

from openbase_coder_cli.cli import node


def test_run_workspace_package_command_prefers_pnpm_workspace(
    monkeypatch, tmp_path: Path
):
    workspace_dir = tmp_path / "workspace"
    package_dir = workspace_dir / "console"
    package_dir.mkdir(parents=True)
    (workspace_dir / "pnpm-workspace.yaml").write_text("packages:\n  - console\n")
    (workspace_dir / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (package_dir / "package.json").write_text("{}")
    calls = []

    monkeypatch.setattr(
        node.shutil,
        "which",
        lambda name: f"/bin/{name}" if name in {"npm", "pnpm"} else None,
    )
    monkeypatch.setattr(
        node.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert (
        node.run_workspace_package_command(workspace_dir, package_dir, "install")
        is True
    )

    assert calls[0][0][0] == [
        "/bin/pnpm",
        "install",
        "--no-lockfile",
        "--shamefully-hoist",
    ]
    assert calls[0][1]["cwd"] == str(package_dir)
    assert calls[0][1]["check"] is True


def test_run_workspace_package_command_uses_npm_for_standalone_package(
    monkeypatch,
    tmp_path: Path,
):
    workspace_dir = tmp_path / "workspace"
    package_dir = workspace_dir / "console"
    package_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text("{}")
    calls = []

    monkeypatch.setattr(
        node.shutil,
        "which",
        lambda name: f"/bin/{name}" if name in {"npm", "pnpm"} else None,
    )
    monkeypatch.setattr(
        node.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert (
        node.run_workspace_package_command(workspace_dir, package_dir, "run", "build")
        is True
    )

    assert calls[0][0][0] == ["/bin/npm", "run", "build"]


def test_run_workspace_package_command_finds_pnpm_home_when_not_on_path(
    monkeypatch,
    tmp_path: Path,
):
    workspace_dir = tmp_path / "workspace"
    package_dir = workspace_dir / "console"
    pnpm_home = tmp_path / "pnpm-home"
    pnpm_bin = pnpm_home / "pnpm"
    package_dir.mkdir(parents=True)
    pnpm_home.mkdir()
    pnpm_bin.write_text("#!/bin/sh\n")
    pnpm_bin.chmod(0o755)
    (workspace_dir / "pnpm-workspace.yaml").write_text("packages:\n  - console\n")
    (workspace_dir / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (package_dir / "package.json").write_text("{}")
    calls = []

    monkeypatch.setenv("PNPM_HOME", str(pnpm_home))
    monkeypatch.setattr(node.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        node.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert (
        node.run_workspace_package_command(workspace_dir, package_dir, "install")
        is True
    )

    assert calls[0][0][0] == [
        str(pnpm_bin),
        "install",
        "--no-lockfile",
        "--shamefully-hoist",
    ]


def test_run_workspace_package_command_requires_pnpm_for_workspace(
    monkeypatch,
    tmp_path: Path,
):
    workspace_dir = tmp_path / "workspace"
    package_dir = workspace_dir / "console"
    package_dir.mkdir(parents=True)
    (workspace_dir / "pnpm-workspace.yaml").write_text("packages:\n  - console\n")
    (package_dir / "package.json").write_text("{}")

    monkeypatch.setattr(
        node,
        "_which_node_binary",
        lambda name: "/bin/npm" if name == "npm" else None,
    )

    with pytest.raises(node.click.ClickException, match="pnpm is required"):
        node.run_workspace_package_command(workspace_dir, package_dir, "install")


def test_which_node_binary_finds_newest_nvm_install(monkeypatch, tmp_path: Path):
    nvm_root = tmp_path / ".nvm" / "versions" / "node"
    old_pnpm = nvm_root / "v9.9.0" / "bin" / "pnpm"
    new_pnpm = nvm_root / "v24.15.0" / "bin" / "pnpm"
    for executable in (old_pnpm, new_pnpm):
        executable.parent.mkdir(parents=True)
        executable.write_text("stub\n")
        executable.chmod(0o755)

    monkeypatch.delenv("PNPM_HOME", raising=False)
    monkeypatch.setattr(node.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(node.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        node.os,
        "access",
        lambda path, _mode: str(path).startswith(str(tmp_path)),
    )

    assert node._which_node_binary("pnpm") == str(new_pnpm)


def test_run_workspace_package_command_allows_pnpm_install_without_lockfile(
    monkeypatch,
    tmp_path: Path,
):
    workspace_dir = tmp_path / "workspace"
    package_dir = workspace_dir / "console"
    package_dir.mkdir(parents=True)
    (workspace_dir / "pnpm-workspace.yaml").write_text("packages:\n  - console\n")
    (package_dir / "package.json").write_text("{}")
    calls = []

    monkeypatch.setattr(
        node.shutil,
        "which",
        lambda name: f"/bin/{name}" if name in {"npm", "pnpm"} else None,
    )
    monkeypatch.setattr(
        node.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert (
        node.run_workspace_package_command(workspace_dir, package_dir, "install")
        is True
    )

    assert calls[0][0][0] == ["/bin/pnpm", "install"]

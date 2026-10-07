from pathlib import Path
from types import SimpleNamespace

import pytest

from openbase_coder_cli.cli.setup import system_summary as summary


@pytest.fixture
def system_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        summary,
        "helper_launchd_health",
        lambda: SimpleNamespace(registered=False, detail="not registered"),
    )
    base = tmp_path / ".openbase"
    for name, path in {
        "OPENBASE_BIN_DIR": base / "bin",
        "INSTALLATION_JSON_PATH": base / "installation.json",
        "OPENBASE_DISPATCHER_CONFIG_PATH": base / "dispatcher-config.json",
        "PLIST_DIR": tmp_path / "Library/LaunchAgents",
        "SYSTEMD_UNIT_DIR": tmp_path / ".config/systemd/user",
        "TASK_SCHEDULER_DIR": base / "tasks",
    }.items():
        monkeypatch.setattr(summary, name, path)
    git_ignore = tmp_path / "global.gitignore"
    monkeypatch.setattr(summary, "_git_ignore_path", lambda: git_ignore)
    return base / ".env", git_ignore


def test_system_summary_reports_only_observed_changes(system_paths, capsys):
    env_file, git_ignore = system_paths
    git_ignore.write_text("existing-pattern\nremove-me\n")
    before = summary.SystemSetupSnapshot.capture(env_file)

    env_file.parent.mkdir(parents=True)
    env_file.write_text("PRIVATE_VALUE=must-never-appear-in-summary\n")
    summary.PLIST_DIR.mkdir(parents=True)
    (summary.PLIST_DIR / "com.openbase.coder.django-cli.plist").write_text("service")
    (summary.PLIST_DIR / "org.example.unrelated.plist").write_text("unrelated")
    git_ignore.write_text("existing-pattern\nnew-pattern\n")
    after = summary.SystemSetupSnapshot.capture(env_file)

    summary.print_system_setup_summary(
        before,
        after,
        service_manager="launchd",
        skip_services=False,
        serve_healthy=True,
    )
    output = capsys.readouterr().out
    assert "Added launchd service definitions: com.openbase.coder.django-cli" in output
    assert "Added local configuration/launcher files: ~/.openbase/.env" in output
    assert (
        "Added Global Git ignore entries in ~/global.gitignore: new-pattern" in output
    )
    assert (
        "Removed Global Git ignore entries in ~/global.gitignore: remove-me" in output
    )
    assert "existing-pattern" not in output
    assert "unrelated" not in output
    assert "must-never-appear-in-summary" not in output
    assert len(output.strip().splitlines()) == 1


def test_system_summary_rerun_does_not_claim_new_services_or_ignores(
    system_paths, capsys
):
    env_file, git_ignore = system_paths
    git_ignore.write_text("already-present\n")
    snapshot = summary.SystemSetupSnapshot.capture(env_file)
    summary.print_system_setup_summary(
        snapshot,
        snapshot,
        service_manager="launchd",
        skip_services=True,
        serve_healthy=False,
    )
    output = capsys.readouterr().out
    assert "Background service installation was skipped" in output
    assert "Global Git ignore entries are unchanged" in output
    assert "Private-network health was not confirmed" in output
    assert "Added" not in output
    assert "refreshed" not in output


def test_unreadable_configuration_is_not_reported_as_removed(capsys):
    before = summary.SystemSetupSnapshot(files={"~/config": "fingerprint"})
    after = summary.SystemSetupSnapshot(unreadable=["~/config"])
    summary.print_system_setup_summary(
        before, after, service_manager="launchd", skip_services=True, serve_healthy=True
    )
    output = capsys.readouterr().out
    assert "Could not compare: ~/config" in output
    assert "Removed local" not in output


def test_snapshot_detects_config_changes_through_symlinks(system_paths, tmp_path):
    env_file, _ = system_paths
    env_file.parent.mkdir(parents=True)
    target = tmp_path / "env-target"
    target.write_text("BEFORE=1\n")
    env_file.symlink_to(target)
    before = summary.SystemSetupSnapshot.capture(env_file)
    target.write_text("AFTER=2\n")
    after = summary.SystemSetupSnapshot.capture(env_file)
    assert before.files["~/.openbase/.env"] != after.files["~/.openbase/.env"]


def test_global_git_ignore_path_uses_configured_file(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(
        summary.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, str(tmp_path / "ignores") + "\n"
        ),
    )
    assert summary._git_ignore_path() == tmp_path / "ignores"


def test_global_git_ignore_path_uses_xdg_default(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        summary.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, ""),
    )
    assert summary._git_ignore_path() == tmp_path / "git/ignore"

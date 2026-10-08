"""Recover real process death between the runtime flip and service activation."""

import signal
import subprocess
import sys

import pytest
from test_self_update import _make_fake_package, _patch_standalone_layout

from openbase_coder_cli import self_update
from openbase_coder_cli.runtime import RuntimePackage
from openbase_coder_cli.self_update_activation import Activation


@pytest.fixture
def interrupted(tmp_path, monkeypatch):
    layout = _patch_standalone_layout(monkeypatch, tmp_path)
    old = _make_fake_package(layout["releases"] / "old", version="1.0")
    new = _make_fake_package(layout["releases"] / "new", version="2.0")
    layout["current"].symlink_to(old)
    site = tmp_path / "plugins" / "site"
    site.mkdir(parents=True)
    (site / "native.so").write_bytes(b"old-ABI")
    monkeypatch.setattr(self_update, "PLUGIN_SITE_DIR", site)
    monkeypatch.setattr(self_update.InstallationConfig, "exists", lambda: False)
    monkeypatch.setattr(self_update, "_voice_session_active", lambda: False)
    monkeypatch.setattr(self_update, "_refresh_backend_binaries", lambda _: None)
    monkeypatch.setattr(self_update, "_validate_release_dir", lambda _: None)
    monkeypatch.setattr(
        self_update,
        "current_runtime_package",
        lambda: RuntimePackage(
            root=layout["current"].resolve(),
            version="2.0" if layout["current"].resolve() == new else "1.0",
            target="target",
        ),
    )
    monkeypatch.setattr(
        self_update,
        "_fetch_manifest",
        lambda _: pytest.fail("Recovery must not need the network"),
    )
    return layout, old, new, site


def kill_during_activation(layout, old, new, site, phase):
    code = """
import os, signal, sys
from pathlib import Path
from openbase_coder_cli.self_update_activation import Activation
from openbase_coder_cli.self_update import _point_symlink
base, old, new, site = map(Path, sys.argv[1:5])
transaction = Activation.begin(base, old=old, new=new, current="1.0", latest="2.0",
                               channel="stable", plugin_site=site, migrate_plugins=True)
_point_symlink(base / "current", new)
(site / "native.so").write_bytes(b"partial-new-ABI")
if sys.argv[5] == "rollback":
    transaction.rollback()
    _point_symlink(base / "current", old)
# SIGKILL does not execute finally blocks or Python shutdown cleanup.
os.kill(os.getpid(), signal.SIGKILL)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(layout["current"].parent),
            str(old),
            str(new),
            str(site),
            phase,
        ],
        timeout=10,
    )
    assert result.returncode == -signal.SIGKILL


def test_killed_activation_is_reported_and_resumed_offline(interrupted, monkeypatch):
    layout, old, new, site = interrupted
    kill_during_activation(layout, old, new, site, "activating")
    assert self_update.check_for_update().update_available
    assert self_update.version_info()["update_available"]
    calls = []

    def post_flip(launcher, **kwargs):
        calls.append(launcher)
        assert (kwargs["plugin_backup"] / "site/native.so").read_bytes() == b"old-ABI"
        (site / "native.so").write_bytes(b"complete-new-ABI")
        return True

    monkeypatch.setattr(self_update, "_post_flip", post_flip)
    result = self_update.run_self_update(report=lambda _: None)
    assert result.status == "updated"
    assert calls == [layout["current"] / "bin/openbase-coder"]
    assert layout["current"].resolve() == new
    assert layout["previous"].resolve() == old
    assert not self_update.activation_pending()
    assert (site / "native.so").read_bytes() == b"complete-new-ABI"


def test_killed_rollback_restores_plugins_and_retries_failed_services(
    interrupted, monkeypatch
):
    layout, old, new, site = interrupted
    kill_during_activation(layout, old, new, site, "rollback")
    monkeypatch.setattr(self_update, "_run_launcher", lambda *a, **kw: False)
    with pytest.raises(
        self_update.SelfUpdateError, match="restoring its services failed"
    ):
        self_update.run_self_update(report=lambda _: None)
    assert self_update.activation_pending()
    assert (site / "native.so").read_bytes() == b"old-ABI"
    # Death during a previous restore must not have consumed the durable backup.
    (site / "native.so").write_bytes(b"partial-restore")
    monkeypatch.setattr(self_update, "_run_launcher", lambda *a, **kw: True)
    assert self_update.run_self_update(report=lambda _: None).status == "rolled-back"
    assert layout["current"].resolve() == old
    assert (site / "native.so").read_bytes() == b"old-ABI"
    assert not self_update.activation_pending()


def test_recovery_waits_for_voice_and_refuses_developer_install(
    interrupted, monkeypatch
):
    layout, old, new, site = interrupted
    kill_during_activation(layout, old, new, site, "activating")
    monkeypatch.setattr(self_update, "_voice_session_active", lambda: True)
    assert self_update.run_self_update(report=lambda _: None).status == "deferred"
    monkeypatch.setattr(self_update, "current_runtime_package", lambda: None)
    with pytest.raises(self_update.SelfUpdateError, match="development workspace"):
        self_update.run_self_update(force=True)
    assert self_update.activation_pending()
    assert layout["current"].resolve() == new


def test_unknown_journal_is_preserved(interrupted):
    layout, _, _, _ = interrupted
    journal = layout["current"].parent / ".activation.json"
    journal.write_text('{"schema_version": 999}')
    with pytest.raises(self_update.SelfUpdateError, match="Unsupported"):
        Activation.load(journal.parent)
    assert journal.read_text() == '{"schema_version": 999}'


def test_unusable_interrupted_target_rolls_back_offline(interrupted, monkeypatch):
    layout, old, new, site = interrupted
    kill_during_activation(layout, old, new, site, "activating")

    def invalid(_):
        raise self_update.SelfUpdateError("missing target file")

    monkeypatch.setattr(self_update, "_validate_release_dir", invalid)
    monkeypatch.setattr(self_update, "_run_launcher", lambda *a, **kw: True)
    result = self_update.run_self_update(report=lambda _: None)
    assert result.status == "rolled-back"
    assert layout["current"].resolve() == old
    assert (site / "native.so").read_bytes() == b"old-ABI"
    assert not self_update.activation_pending()

"""Developer installs must not use leftovers from an earlier packaged install."""

import json

import pytest

from openbase_coder_cli import runtime, self_update
from openbase_coder_cli.services import installation


@pytest.fixture
def package_tree(monkeypatch, tmp_path):
    root = tmp_path / "release"
    python = root / "python/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    (root / runtime.PACKAGE_METADATA_FILENAME).write_text(
        json.dumps({"version": "1.0.0", "target": "aarch64-apple-darwin"})
    )
    monkeypatch.setattr(
        installation, "INSTALLATION_JSON_PATH", tmp_path / "installation.json"
    )
    monkeypatch.setattr(self_update, "STANDALONE_PACKAGES_DIR", tmp_path / "state")
    monkeypatch.setattr(self_update, "STANDALONE_CURRENT_DIR", root)
    monkeypatch.setenv("OPENBASE_CODER_PACKAGE_DIR", str(root))
    return root


@pytest.mark.parametrize("packaged_python", [False, True])
def test_package_hint_cannot_make_workspace_code_standalone(
    monkeypatch, package_tree, packaged_python
):
    if packaged_python:
        # Editable/PYTHONPATH workspace code, even using bundled Python.
        monkeypatch.setattr(
            runtime.sys, "executable", str(package_tree / "python/bin/python")
        )
    assert runtime.current_runtime_package() is None
    monkeypatch.setattr(
        self_update,
        "_fetch_manifest",
        lambda _: pytest.fail("developer fetched a release"),
    )
    assert self_update.check_for_update().update_available is False
    for updater in (
        self_update.run_self_update,
        self_update.run_automatic_self_update,
        self_update.spawn_detached_self_update,
    ):
        with pytest.raises(self_update.SelfUpdateError, match="development workspace"):
            updater(force=True)
    assert not self_update.STANDALONE_PACKAGES_DIR.exists()


def use_packaged_code(monkeypatch, root):
    monkeypatch.setattr(
        runtime, "__file__", str(root / "python/lib/openbase_coder_cli/runtime.py")
    )
    monkeypatch.setattr(runtime.sys, "executable", str(root / "python/bin/python"))


def test_packaged_code_ignores_stale_hint_and_resolves_current_symlink(
    monkeypatch, package_tree, tmp_path
):
    use_packaged_code(monkeypatch, package_tree)
    other = tmp_path / "other-release"
    other.mkdir()
    (other / runtime.PACKAGE_METADATA_FILENAME).write_text(
        json.dumps({"version": "0.9.0"})
    )
    monkeypatch.setenv("OPENBASE_CODER_PACKAGE_DIR", str(other))
    assert runtime.current_runtime_package().root == package_tree
    alias = tmp_path / "current"
    alias.symlink_to(package_tree)
    monkeypatch.setenv("OPENBASE_CODER_PACKAGE_DIR", str(alias))
    assert runtime.current_runtime_package().root == alias


@pytest.mark.parametrize("contents", ["invalid json", "[]", "null"])
def test_invalid_metadata_is_not_a_packaged_runtime(
    monkeypatch, package_tree, contents
):
    use_packaged_code(monkeypatch, package_tree)
    (package_tree / runtime.PACKAGE_METADATA_FILENAME).write_text(contents)
    assert runtime.current_runtime_package() is None


@pytest.mark.parametrize("standalone", [False, "false", None])
def test_packaged_worker_cannot_replace_active_developer_install(
    monkeypatch, package_tree, standalone
):
    use_packaged_code(monkeypatch, package_tree)
    installation.InstallationConfig(
        workspace_path="/workspace", standalone=standalone
    ).save()
    monkeypatch.setattr(
        self_update,
        "_fetch_manifest",
        lambda _: pytest.fail("developer fetched a release"),
    )
    assert runtime.is_standalone_runtime()
    assert self_update.check_for_update().update_available is False
    for updater in (
        self_update.run_self_update,
        self_update.run_automatic_self_update,
        self_update.spawn_detached_self_update,
    ):
        with pytest.raises(self_update.SelfUpdateError, match="development workspace"):
            updater(force=True)


def test_waiting_worker_exits_when_install_switches_to_developer(
    monkeypatch, package_tree
):
    use_packaged_code(monkeypatch, package_tree)
    monkeypatch.delenv(self_update.AUTO_UPDATE_ENV_KEY, raising=False)
    monkeypatch.setattr(self_update, "OPENBASE_BASE_DIR", package_tree.parent)
    monkeypatch.setattr(self_update, "_voice_session_active", lambda: True)
    monkeypatch.setattr(
        self_update.time,
        "sleep",
        lambda _: installation.InstallationConfig(standalone=False).save(),
    )
    with pytest.raises(self_update.SelfUpdateError, match="development workspace"):
        self_update.run_automatic_self_update(report=lambda _: None)


@pytest.mark.parametrize("force", [False, True])
def test_developer_setup_during_download_prevents_activation(
    monkeypatch, package_tree, force
):
    use_packaged_code(monkeypatch, package_tree)
    monkeypatch.setattr(self_update, "_voice_session_active", lambda: False)
    monkeypatch.setattr(
        self_update,
        "_fetch_manifest",
        lambda _: {
            "version": "2.0.0",
            "layout_version": 1,
            "targets": {
                "aarch64-apple-darwin": {"url": "fixture", "sha256": "fixture"}
            },
        },
    )

    def download(**kwargs):
        installation.InstallationConfig(standalone=False).save()
        return package_tree.parent / "new-release"

    monkeypatch.setattr(self_update, "_download_and_extract", download)
    monkeypatch.setattr(self_update, "_validate_release_dir", lambda _: None)
    monkeypatch.setattr(
        self_update,
        "_point_symlink",
        lambda *_: pytest.fail("developer runtime replaced"),
    )
    with pytest.raises(self_update.SelfUpdateError, match="development workspace"):
        self_update.run_self_update(force=force, report=lambda _: None)


def test_packaged_status_still_reads_update_cache(monkeypatch, package_tree, tmp_path):
    use_packaged_code(monkeypatch, package_tree)
    cache = tmp_path / "update-check.json"
    cache.write_text(
        json.dumps(
            {
                "update_available": True,
                "update_required": True,
                "latest_version": "2.0.0",
            }
        )
    )
    monkeypatch.setattr(self_update, "UPDATE_CHECK_CACHE_PATH", cache)
    assert self_update.version_info()["update_required"] is True
    installation.InstallationConfig(standalone=False).save()
    info = self_update.version_info()
    assert info["update_available"] is info["update_required"] is False
    assert "latest_version" not in info


@pytest.mark.parametrize("packaged_process", [False, True])
def test_update_api_denies_developer_install_even_with_force(
    monkeypatch, package_tree, packaged_process
):
    from types import SimpleNamespace

    from django.conf import settings

    if not settings.configured:
        settings.configure(DEFAULT_CHARSET="utf-8", REST_FRAMEWORK={})
    from openbase_coder_cli.openbase_coder_cli_app import update

    if packaged_process:
        use_packaged_code(monkeypatch, package_tree)
    installation.InstallationConfig(standalone=False).save()
    monkeypatch.setattr(
        update,
        "spawn_detached_self_update",
        lambda **_: pytest.fail("API spawned updater"),
    )
    request = SimpleNamespace(data={"force": True}, query_params={"refresh": "1"})
    response = update.update_apply.cls().post(request)
    assert response.status_code == 400
    response = update.update_status.cls().get(request)
    assert response.status_code == 200
    assert (
        response.data["update_available"] is response.data["update_required"] is False
    )


@pytest.mark.parametrize(
    "install_mode", ["workspace", "leftover-package", "standalone"]
)
def test_routines_startup_only_spawns_for_active_standalone(
    monkeypatch, package_tree, install_mode
):
    from importlib import import_module

    from click.testing import CliRunner

    from openbase_coder_cli import skills_autolink

    routines = import_module("openbase_coder_cli.cli.routines")
    if install_mode != "workspace":
        use_packaged_code(monkeypatch, package_tree)
    installation.InstallationConfig(standalone=install_mode == "standalone").save()
    monkeypatch.delenv(self_update.AUTO_UPDATE_ENV_KEY, raising=False)
    monkeypatch.setattr(routines, "_run_client", lambda _: {"count": 0})
    monkeypatch.setattr(
        skills_autolink, "sync_auto_linked_skills", lambda: {"enabled": False}
    )
    fetched = []

    def fetch(channel):
        fetched.append(channel)
        return {"version": "2.0.0"}

    monkeypatch.setattr(self_update, "_fetch_manifest", fetch)
    spawned = []
    monkeypatch.setattr(
        self_update,
        "spawn_detached_self_update",
        lambda **kwargs: spawned.append(kwargs),
    )

    def stop_after_iteration(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(routines.time, "sleep", stop_after_iteration)
    result = CliRunner().invoke(routines.routines, ["run-loop"])
    assert result.exit_code == 1  # Deliberately end the otherwise infinite loop.
    assert len(fetched) == len(spawned) == (1 if install_mode == "standalone" else 0)

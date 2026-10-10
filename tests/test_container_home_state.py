"""A container image upgrade keeps the Super Agents registry.

On Maritime only /data survives a redeploy; the image layer, $HOME included,
is replaced. The Super Agents registry lived in $HOME, so an in-place image
upgrade of a staging workspace (2026-10-09) lost every managed thread id, the
Dispatcher's thread and the Super Agent names. These tests drive the real
entrypoint helper with a real Super Agents store.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from super_agents.agent_store import Store as AgentStore

SCRIPT = Path(__file__).parents[1] / "docker" / "persist-home-state.sh"
PRE_UPGRADE = Path(__file__).parents[1] / "docker" / "pre-upgrade-copy-home-state.sh"


def _boot(home: Path, data_dir: Path) -> str:
    result = subprocess.run(
        ["bash", str(SCRIPT), str(home), str(data_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def _store(monkeypatch, home: Path) -> AgentStore:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SUPER_AGENTS_CLAUDE_CODE_HOME", raising=False)
    return AgentStore()


def test_image_upgrade_keeps_thread_ids_dispatcher_and_agent_names(tmp_path, volume, monkeypatch) -> None:
    # A workspace running an image from before the fix: the registry is a
    # real directory in the image layer's $HOME.
    old_layer = tmp_path / "layer-old" / "home"
    old_layer.mkdir(parents=True)
    store = _store(monkeypatch, old_layer)
    dispatcher = store.create_session("dispatcher", cwd=str(tmp_path))
    sunny = store.create_session("tic-tac-toe", cwd=str(tmp_path), agent_name="Sunny")
    joanie = store.create_session("checklist", cwd=str(tmp_path), agent_name="Joanie")
    (old_layer / ".super-agents").mkdir()
    (old_layer / ".super-agents" / "state.json").write_text('{"sessions": {"x": 1}}', encoding="utf-8")
    (old_layer / ".openbase").mkdir()
    (old_layer / ".openbase" / "coder-projects.json").write_text('[{"path": "/data/workspace/tic-tac-toe"}]', encoding="utf-8")

    # First boot on the fixed image (a restart of that layer) adopts it.
    output = _boot(old_layer, volume)
    assert "adopted" in output
    assert (old_layer / ".local" / "share" / "super-agents-claude-code").is_symlink()
    assert (old_layer / ".super-agents").is_symlink()

    # The image upgrade: the whole image layer, $HOME included, is replaced.
    shutil.rmtree(tmp_path / "layer-old")
    new_layer = tmp_path / "layer-new" / "home"
    new_layer.mkdir(parents=True)
    _boot(new_layer, volume)

    upgraded = _store(monkeypatch, new_layer)
    assert {session.id for session in upgraded.list_sessions()} == {dispatcher.id, sunny.id, joanie.id}
    assert upgraded.get_by_name("dispatcher").id == dispatcher.id
    assert upgraded.get_session(sunny.id).agent_name == "Sunny"
    assert upgraded.get_session(joanie.id).agent_name == "Joanie"
    assert json.loads((new_layer / ".super-agents" / "state.json").read_text(encoding="utf-8")) == {"sessions": {"x": 1}}
    assert json.loads((volume / "coder-projects.json").read_text(encoding="utf-8")) == [{"path": "/data/workspace/tic-tac-toe"}]

    # New threads after the upgrade land on the volume too.
    after = upgraded.create_session("after-upgrade", cwd=str(tmp_path), agent_name="Renee")
    assert (volume / "super-agents-claude-code" / "state.sqlite3").is_file()
    assert _store(monkeypatch, new_layer).get_session(after.id).agent_name == "Renee"


def test_pre_upgrade_refresh_preserves_each_project_snapshot(tmp_path, volume) -> None:
    home = tmp_path / "home"
    source = home / ".openbase" / "coder-projects.json"
    source.parent.mkdir(parents=True)
    source.write_text("[]")
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "date").write_text("#!/bin/sh\nprintf '20261009T000000Z\\n'\n")
    (shims / "date").chmod(0o755)
    env = {"PATH": f"{shims}:{os.environ['PATH']}"}
    for version in range(3):
        source.write_text(json.dumps([{"version": version}]))
        result = _copy("--refresh", str(home), str(volume), env=env)
        assert result.returncode == 0, result.stderr
    parked = sorted(volume.glob("coder-projects.json.replaced-*"))
    assert len(parked) == 2
    assert sorted(json.loads((path / "state").read_text())[0]["version"] for path in parked) == [0, 1]


def test_pre_upgrade_refresh_keeps_projects_already_on_volume(tmp_path, volume) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".openbase").symlink_to(volume, target_is_directory=True)
    projects_file = volume / "coder-projects.json"
    projects_file.write_text("[]")
    result = _copy("--refresh", str(home), str(volume))
    assert result.returncode == 0, result.stderr
    assert projects_file.read_text() == "[]"
    assert not list(volume.glob("coder-projects.json.replaced-*"))


def test_boot_is_idempotent_and_never_deletes_a_volume_copy(tmp_path, volume, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _boot(home, volume)
    store = _store(monkeypatch, home)
    kept = store.create_session("dispatcher", cwd=str(tmp_path))

    # Restarts change nothing and set nothing aside.
    assert _boot(home, volume) == ""
    assert _boot(home, volume) == ""
    assert not list(volume.glob("*.replaced-*"))
    assert _store(monkeypatch, home).get_by_name("dispatcher").id == kept.id

    # A newer live directory in $HOME wins over an older copy on the volume,
    # which is set aside rather than deleted.
    os.utime(volume / "super-agents", (1, 1))
    other = tmp_path / "other-home"
    (other / ".super-agents").mkdir(parents=True)
    (other / ".super-agents" / "state.json").write_text("{}", encoding="utf-8")
    output = _boot(other, volume)
    assert "adopted" in output and "kept as" in output
    assert len(list(volume.glob("super-agents.replaced-*"))) == 1
    assert (volume / "super-agents" / "state.json").read_text(encoding="utf-8") == "{}"


def test_entrypoint_persists_home_state_before_setup_and_services() -> None:
    root = Path(__file__).parents[1]
    entrypoint = (root / "docker" / "entrypoint.sh").read_text()
    dockerfile = (root / "Dockerfile").read_text()
    call = entrypoint.index('/usr/local/bin/openbase-coder-persist-home-state "$HOME" "$DATA_DIR"')
    assert call < entrypoint.index("# --- First-run setup")
    assert call < entrypoint.index('start_supervised "$name" bash "$wrapper"')
    assert "COPY docker/persist-home-state.sh /usr/local/bin/openbase-coder-persist-home-state" in dockerfile


def test_pre_upgrade_copy_lets_a_pre_fix_workspace_keep_its_registry(tmp_path, volume, monkeypatch) -> None:
    # A workspace still on an image from before the fix never ran the
    # adoption, and the redeploy replaces $HOME before the new entrypoint
    # runs. The one-time copy made inside the running workspace bridges it.
    old_layer = tmp_path / "layer-old" / "home"
    old_layer.mkdir(parents=True)
    store = _store(monkeypatch, old_layer)
    dispatcher = store.create_session("dispatcher", cwd=str(tmp_path))
    sunny = store.create_session("tic-tac-toe", cwd=str(tmp_path), agent_name="Sunny")
    (old_layer / ".super-agents").mkdir()
    (old_layer / ".super-agents" / "state.json").write_text("{}", encoding="utf-8")

    copied = subprocess.run(
        ["bash", str(PRE_UPGRADE), str(old_layer), str(volume)], check=True, capture_output=True, text=True
    ).stdout
    assert "copied" in copied
    # It leaves the running layer untouched and never overwrites a volume copy.
    assert not (old_layer / ".super-agents").is_symlink()
    again = subprocess.run(
        ["bash", str(PRE_UPGRADE), str(old_layer), str(volume)], check=True, capture_output=True, text=True
    ).stdout
    assert "kept" in again and "copied" not in again

    shutil.rmtree(tmp_path / "layer-old")
    new_layer = tmp_path / "layer-new" / "home"
    new_layer.mkdir(parents=True)
    _boot(new_layer, volume)

    upgraded = _store(monkeypatch, new_layer)
    assert upgraded.get_by_name("dispatcher").id == dispatcher.id
    assert upgraded.get_session(sunny.id).agent_name == "Sunny"


def test_boot_adopts_state_behind_a_different_symlink(tmp_path, volume) -> None:
    home = tmp_path / "home"
    home.mkdir()
    previous = tmp_path / "previous-registry"
    previous.mkdir()
    (previous / "state.json").write_text('{"sessions": {"kept": 1}}')
    (home / ".super-agents").symlink_to(previous, target_is_directory=True)

    _boot(home, volume)

    assert (home / ".super-agents").resolve() == volume / "super-agents"
    assert (volume / "super-agents" / "state.json").read_text() == (previous / "state.json").read_text()


@pytest.mark.parametrize("script,suffix", [(SCRIPT, "adopting"), (PRE_UPGRADE, "copying")])
def test_retry_does_not_nest_state_in_an_interrupted_copy(tmp_path, volume, script, suffix) -> None:
    home = tmp_path / "home"
    source = home / ".super-agents"
    source.mkdir(parents=True)
    (source / "state.json").write_text('{"sessions": {"latest": 1}}')
    partial = volume / f"super-agents.{suffix}"
    partial.mkdir()
    (partial / "state.json").write_text("incomplete")

    subprocess.run(["bash", str(script), str(home), str(volume)], check=True, capture_output=True)

    assert (volume / "super-agents" / "state.json").read_text() == '{"sessions": {"latest": 1}}'


def test_boot_repairs_a_dangling_expected_symlink(tmp_path, volume) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = volume / "super-agents"
    (home / ".super-agents").symlink_to(destination, target_is_directory=True)

    _boot(home, volume)

    assert destination.is_dir()


def test_boot_keeps_projects_when_legacy_home_is_a_volume_symlink(tmp_path, volume) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".openbase").symlink_to(volume, target_is_directory=True)
    projects = volume / "coder-projects.json"
    projects.write_text('[{"path": "/data/workspace/project"}]')

    _boot(home, volume)

    assert projects.read_text() == '[{"path": "/data/workspace/project"}]'


def test_boot_retains_both_conflicting_project_caches(tmp_path, volume) -> None:
    legacy = tmp_path / "home" / ".openbase" / "coder-projects.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('[{"path": "/data/workspace/legacy"}]')
    durable = volume / "coder-projects.json"
    durable.write_text('[{"path": "/data/workspace/durable"}]')

    _boot(legacy.parent.parent, volume)

    assert legacy.read_text() == '[{"path": "/data/workspace/legacy"}]'
    assert durable.read_text() == '[{"path": "/data/workspace/durable"}]'


def test_pre_upgrade_backs_up_committed_wal_with_an_open_connection(tmp_path, volume) -> None:
    home = tmp_path / "home"
    source = home / ".local" / "share" / "super-agents-claude-code"
    source.mkdir(parents=True)
    database = source / "state.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sessions (name TEXT)")
        connection.execute("INSERT INTO sessions VALUES ('dispatcher')")
        connection.commit()
        assert database.with_name("state.sqlite3-wal").stat().st_size > 0

        subprocess.run(["bash", str(PRE_UPGRADE), str(home), str(volume)], check=True, capture_output=True)

        backup = sqlite3.connect(volume / "super-agents-claude-code" / "state.sqlite3")
        try:
            assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert backup.execute("SELECT name FROM sessions").fetchall() == [("dispatcher",)]
        finally:
            backup.close()
    finally:
        connection.close()


def newest_mtime_ns(root: Path) -> int:
    """The scripts' freshness measure: the newest mtime anywhere in the tree."""
    entries = [root, *root.rglob("*")] if root.is_dir() and not root.is_symlink() else [root]
    return max(entry.lstat().st_mtime_ns for entry in entries)


def _touch_newer_than(reference: Path, path: Path) -> None:
    """Give one source file an mtime an hour past everything under reference."""
    stamp = newest_mtime_ns(reference) + 3_600 * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def _copy(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:

    return subprocess.run(
        ["bash", str(PRE_UPGRADE), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def _pre_fix_layer(
    tmp_path: Path, monkeypatch, name: str = "layer-old"
) -> tuple[Path, AgentStore]:
    home = tmp_path / name / "home"
    home.mkdir(parents=True)
    store = _store(monkeypatch, home)
    (home / ".super-agents").mkdir()
    (home / ".super-agents" / "state.json").write_text("{}", encoding="utf-8")
    (home / ".openbase").mkdir()
    (home / ".openbase" / "coder-projects.json").write_text("[]", encoding="utf-8")
    return home, store


def test_pre_upgrade_copy_never_reads_the_callers_home(
    tmp_path, volume, monkeypatch
) -> None:
    # Maritime exec runs as root: $HOME there is /root, not the workspace
    # user's home, so the paths are explicit and an unset one is an error,
    # never a silent copy of the wrong (or no) registry.
    decoy, _ = _pre_fix_layer(tmp_path, monkeypatch, "decoy")
    result = _copy(
        str(tmp_path / "missing-home"), str(volume), env={"HOME": str(decoy)}
    )
    assert result.returncode == 2
    assert "not a directory" in result.stderr
    assert not (volume / "super-agents").exists()
    assert not (volume / "coder-projects.json").exists()


def test_pre_upgrade_copy_gives_everything_to_the_home_owner(
    tmp_path, volume, monkeypatch
) -> None:
    # The copy is written by root (the exec user); the workspace user must own
    # it, or the store is read-only after the redeploy and the API fails.
    home, store = _pre_fix_layer(tmp_path, monkeypatch)
    store.create_session("dispatcher", cwd=str(tmp_path))
    shims = tmp_path / "shims"
    shims.mkdir()
    calls = tmp_path / "chown-calls.txt"
    (shims / "chown").write_text(
        f'#!/bin/bash\necho "$*" >> {calls}\n', encoding="utf-8"
    )
    (shims / "chown").chmod(0o755)
    result = _copy(
        str(home), str(volume), env={"PATH": f"{shims}:{os.environ['PATH']}"}
    )
    assert result.returncode == 0, result.stderr
    owner = f"{home.stat().st_uid}:{home.stat().st_gid}"
    recorded = calls.read_text(encoding="utf-8").splitlines()
    assert f"{owner} {volume}" in recorded
    # Each destination is handed over before it is moved into place.
    assert any(
        line.startswith(f"-R {owner} {volume}/super-agents.copying-")
        for line in recorded
    )
    assert any(
        line.startswith(f"-R {owner} {volume}/super-agents-claude-code.copying-")
        for line in recorded
    )
    assert f"-R {owner} {volume}/coder-projects.json" in recorded

    _touch_newer_than(volume, home / ".super-agents" / "state.json")
    _touch_newer_than(volume, home / ".local" / "share" / "super-agents-claude-code" / "state.sqlite3")
    _touch_newer_than(volume, home / ".openbase" / "coder-projects.json")
    refreshed = _copy(
        "--refresh", str(home), str(volume), env={"PATH": f"{shims}:{os.environ['PATH']}"}
    )
    assert refreshed.returncode == 0, refreshed.stderr
    recorded = calls.read_text(encoding="utf-8").splitlines()
    parked = list(volume.glob("*.replaced-*"))
    assert len(parked) == 3
    assert all(f"{owner} {path}" in recorded for path in parked)


def test_pre_upgrade_copy_keeps_the_store_file_mode(
    tmp_path, volume, monkeypatch
) -> None:
    home, store = _pre_fix_layer(tmp_path, monkeypatch)
    store.create_session("dispatcher", cwd=str(tmp_path))
    source = home / ".local" / "share" / "super-agents-claude-code" / "state.sqlite3"
    source.chmod(0o600)
    assert _copy(str(home), str(volume)).returncode == 0
    copied = volume / "super-agents-claude-code" / "state.sqlite3"
    assert copied.stat().st_mode & 0o777 == 0o600


def test_pre_upgrade_copy_refresh_sets_the_previous_copy_aside(
    tmp_path, volume, monkeypatch
) -> None:
    # The live store keeps changing while a workspace runs, and the Cloud's
    # redeploy guard refuses a copy older than the store. --refresh remakes
    # the copy right before the redeploy and never deletes the earlier one.
    home, store = _pre_fix_layer(tmp_path, monkeypatch)
    dispatcher = store.create_session("dispatcher", cwd=str(tmp_path))
    assert "copied" in _copy(str(home), str(volume)).stdout
    sunny = store.create_session("tic-tac-toe", cwd=str(tmp_path), agent_name="Sunny")
    (home / ".openbase" / "coder-projects.json").write_text(
        '[{"path": "/data/workspace/x"}]', encoding="utf-8"
    )

    refreshed = _copy("--refresh", str(home), str(volume))
    assert refreshed.returncode == 0, refreshed.stderr
    # The Claude Code store and the projects file changed since the copy;
    # .super-agents did not, so its volume copy (as new as the source) stays.
    assert refreshed.stdout.count("set aside") == 2
    assert refreshed.stdout.count("copied") == 2
    assert f"kept {volume / 'super-agents'}" in refreshed.stdout
    assert not list(volume.glob("super-agents.replaced-*"))
    parked = sorted(volume.glob("super-agents-claude-code.replaced-*"))
    assert len(parked) == 1 and (parked[0] / "state" / "state.sqlite3").is_file()
    assert sorted(volume.glob("coder-projects.json.replaced-*"))
    assert json.loads((volume / "coder-projects.json").read_text(encoding="utf-8")) == [
        {"path": "/data/workspace/x"}
    ]

    shutil.rmtree(tmp_path / "layer-old")
    new_layer = tmp_path / "layer-new" / "home"
    new_layer.mkdir(parents=True)
    _boot(new_layer, volume)
    upgraded = _store(monkeypatch, new_layer)
    assert {session.id for session in upgraded.list_sessions()} == {
        dispatcher.id,
        sunny.id,
    }
    assert upgraded.get_session(sunny.id).agent_name == "Sunny"


@pytest.mark.parametrize("pre_upgrade", [False, True])
def test_github_login_survives_image_replacement(tmp_path, volume, pre_upgrade):
    old_home = tmp_path / "old-home"
    config = old_home / ".config" / "gh"
    config.mkdir(parents=True)
    hosts = config / "hosts.yml"
    # Deliberately contains no credential; preservation is byte-for-byte.
    hosts.write_text("github.com:\n    user: example-user\n    git_protocol: https\n")
    hosts.chmod(0o600)
    expected = hosts.read_bytes()
    if pre_upgrade:
        subprocess.run(["bash", str(PRE_UPGRADE), str(old_home), str(volume)], check=True, capture_output=True)
        assert not config.is_symlink()
    else:
        _boot(old_home, volume)
        assert config.resolve() == volume / "github-cli"
        assert _boot(old_home, volume) == ""
    shutil.rmtree(old_home)
    new_home = tmp_path / "new-home"
    new_home.mkdir()
    _boot(new_home, volume)
    restored = new_home / ".config" / "gh" / "hosts.yml"
    assert restored.read_bytes() == expected
    assert restored.stat().st_mode & 0o777 == 0o600
    assert restored.parent.stat().st_mode & 0o777 == 0o700

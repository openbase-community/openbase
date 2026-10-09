"""A container image upgrade keeps the Super Agents registry.

On Maritime only /data survives a redeploy; the image layer, $HOME included,
is replaced. The Super Agents registry lived in $HOME, so an in-place image
upgrade of a staging workspace (2026-10-09) lost every managed thread id, the
Dispatcher's thread and the Super Agent names. These tests drive the real
entrypoint helper with a real Super Agents store.
"""

from __future__ import annotations

import json
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


@pytest.fixture
def volume(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPER_AGENTS_CLAUDE_CODE_HOME", raising=False)
    data_dir = tmp_path / "data" / "openbase"
    data_dir.mkdir(parents=True)
    return data_dir


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

    # A live directory in $HOME wins over an older copy on the volume, which
    # is set aside rather than deleted.
    other = tmp_path / "other-home"
    (other / ".super-agents").mkdir(parents=True)
    (other / ".super-agents" / "state.json").write_text("{}", encoding="utf-8")
    output = _boot(other, volume)
    assert "kept the previous" in output
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
    assert "already exists" in again

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

"""A /data store is never replaced by an older copy.

Maritime workspaces can wake on an older image than the one last deployed
(staging, 2026-10-10). That image layer's $HOME then holds real Super Agents
stores again, older than the ones on /data, and both the boot-time adoption
(docker/persist-home-state.sh) and the pre-upgrade copy
(docker/pre-upgrade-copy-home-state.sh --refresh) copied the stale layer over
the newer volume store on workspace 374, parking a thread and its turns.
Both scripts now compare the newest modification time anywhere in each tree
and keep the volume copy unless the $HOME store is strictly newer (or the
operator forces the copy). These tests drive the real scripts with touched
mtimes; no container is needed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_container_home_state import SCRIPT, _boot, _copy, _store, newest_mtime_ns

OLD = 1_700_000_000
NEWER = OLD + 3_600


def _set_tree_mtime(root: Path, seconds: int) -> None:
    """Pin every entry of a tree (directories included) to one mtime."""
    for entry in [*root.rglob("*"), root]:
        os.utime(entry, (seconds, seconds), follow_symlinks=False)


def _home_with_store(tmp_path: Path, name: str, payload: str, *, mtime: int) -> Path:
    home = tmp_path / name
    (home / ".super-agents").mkdir(parents=True)
    (home / ".super-agents" / "state.json").write_text(payload, encoding="utf-8")
    _set_tree_mtime(home / ".super-agents", mtime)
    return home


def _volume_store(volume: Path, payload: str, *, mtime: int) -> Path:
    store = volume / "super-agents"
    store.mkdir()
    (store / "state.json").write_text(payload, encoding="utf-8")
    _set_tree_mtime(store, mtime)
    return store


def _registry(volume: Path) -> str:
    return (volume / "super-agents" / "state.json").read_text(encoding="utf-8")


def _parked(volume: Path) -> list[Path]:
    return sorted(volume.glob("super-agents.replaced-*"))


@pytest.mark.parametrize("operation", ["boot", "refresh"])
@pytest.mark.parametrize("failed_side", ["home", "volume"])
@pytest.mark.parametrize("failed_command", ["find", "stat"])
def test_incomplete_freshness_scan_refuses_to_change_either_store(
    tmp_path, volume, operation, failed_side, failed_command
) -> None:
    home = _home_with_store(tmp_path, "home", '{"layer": true}', mtime=OLD + 60)
    store = _volume_store(volume, '{"volume": true}', mtime=NEWER)
    failed_root = home / ".super-agents" if failed_side == "home" else store
    shims = tmp_path / "shims"
    shims.mkdir()
    executable = shutil.which(failed_command)
    assert executable is not None
    shim = shims / failed_command
    shim.write_text(
        '#!/bin/bash\n'
        'for argument in "$@"; do\n'
        '    if [ "$argument" = "$FAILED_ROOT" ]; then\n'
        '        printf "1700000000.000000000\\n"\n'
        '        echo "simulated incomplete scan" >&2\n'
        '        exit 1\n'
        '    fi\n'
        'done\n'
        'exec "$REAL_COMMAND" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{shims}:{os.environ['PATH']}",
        "FAILED_ROOT": str(failed_root),
        "REAL_COMMAND": executable,
    }

    if operation == "boot":
        result = subprocess.run(
            ["bash", str(SCRIPT), str(home), str(volume)],
            capture_output=True,
            text=True,
            env=env,
        )
    else:
        result = _copy("--refresh", str(home), str(volume), env=env)

    assert result.returncode != 0
    assert "simulated incomplete scan" in result.stderr
    assert not (home / ".super-agents").is_symlink()
    assert (home / ".super-agents" / "state.json").read_text() == '{"layer": true}'
    assert _registry(volume) == '{"volume": true}'
    assert _parked(volume) == []
    assert not list(home.glob(".super-agents.migrated-*"))


@pytest.mark.parametrize("operation", ["boot", "refresh"])
@pytest.mark.parametrize("newer_side", ["home", "volume"])
def test_freshness_distinguishes_writes_in_the_same_second(
    tmp_path, volume, operation, newer_side
) -> None:
    home = _home_with_store(tmp_path, "home", '{"layer": true}', mtime=OLD)
    store = _volume_store(volume, '{"volume": true}', mtime=OLD)
    newer_root = home / ".super-agents" if newer_side == "home" else store
    stamp = OLD * 1_000_000_000 + 1
    os.utime(newer_root / "state.json", ns=(stamp, stamp))

    if operation == "boot":
        _boot(home, volume)
    else:
        result = _copy("--refresh", str(home), str(volume))
        assert result.returncode == 0, result.stderr

    expected = '{"layer": true}' if newer_side == "home" else '{"volume": true}'
    assert _registry(volume) == expected


# --- pre-upgrade-copy-home-state.sh -----------------------------------------


@pytest.mark.parametrize("flags", [(), ("--refresh",)])
def test_pre_upgrade_keeps_a_newer_volume_copy(tmp_path, volume, flags) -> None:
    home = _home_with_store(tmp_path, "home", '{"stale": true}', mtime=OLD)
    _volume_store(volume, '{"live": true}', mtime=NEWER)

    result = _copy(*flags, str(home), str(volume))

    assert result.returncode == 0, result.stderr
    assert (
        f"kept {volume / 'super-agents'} (volume copy is newer than {home / '.super-agents'})"
        in result.stdout
    )
    assert "set aside" not in result.stdout and "copied" not in result.stdout
    assert _registry(volume) == '{"live": true}'
    assert _parked(volume) == []


@pytest.mark.parametrize("flags", [(), ("--refresh",)])
def test_pre_upgrade_keeps_a_volume_copy_as_new_as_the_source(
    tmp_path, volume, flags
) -> None:
    home = _home_with_store(tmp_path, "home", '{"layer": true}', mtime=OLD)
    _volume_store(volume, '{"volume": true}', mtime=OLD)

    result = _copy(*flags, str(home), str(volume))

    assert result.returncode == 0, result.stderr
    assert f"kept {volume / 'super-agents'}" in result.stdout
    assert _registry(volume) == '{"volume": true}'
    assert _parked(volume) == []


def test_pre_upgrade_refresh_replaces_an_older_volume_copy_and_parks_it(
    tmp_path, volume
) -> None:
    home = _home_with_store(tmp_path, "home", '{"live": true}', mtime=NEWER)
    _volume_store(volume, '{"stale": true}', mtime=OLD)

    result = _copy("--refresh", str(home), str(volume))

    assert result.returncode == 0, result.stderr
    assert "set aside" in result.stdout and "copied" in result.stdout
    assert _registry(volume) == '{"live": true}'
    [parked] = _parked(volume)
    assert (parked / "state" / "state.json").read_text(
        encoding="utf-8"
    ) == '{"stale": true}'


def test_pre_upgrade_without_refresh_leaves_an_older_volume_copy_alone(
    tmp_path, volume
) -> None:
    home = _home_with_store(tmp_path, "home", '{"live": true}', mtime=NEWER)
    _volume_store(volume, '{"stale": true}', mtime=OLD)

    result = _copy(str(home), str(volume))

    assert result.returncode == 0, result.stderr
    assert "already exists" in result.stdout and "--refresh" in result.stdout
    assert _registry(volume) == '{"stale": true}'
    assert _parked(volume) == []


def test_pre_upgrade_force_replaces_a_newer_volume_copy_with_a_warning(
    tmp_path, volume
) -> None:
    home = _home_with_store(tmp_path, "home", '{"stale": true}', mtime=OLD)
    _volume_store(volume, '{"live": true}', mtime=NEWER)

    result = _copy("--force", str(home), str(volume))

    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr and "--force" in result.stderr
    assert "set aside" in result.stdout and "copied" in result.stdout
    assert _registry(volume) == '{"stale": true}'
    [parked] = _parked(volume)
    assert (parked / "state" / "state.json").read_text(
        encoding="utf-8"
    ) == '{"live": true}'


def test_pre_upgrade_rejects_an_unknown_flag(tmp_path, volume) -> None:
    result = _copy("--fresh", str(tmp_path), str(volume))
    assert result.returncode == 2
    assert "unknown option" in result.stderr


def test_pre_upgrade_freshness_covers_the_whole_tree(tmp_path, volume) -> None:
    # The top-level files of the volume copy are older, but a thread log deep
    # in its tree is newer than anything in the layer's store: the copy wins.
    home = _home_with_store(tmp_path, "home", '{"layer": true}', mtime=OLD + 60)
    store = _volume_store(volume, '{"volume": true}', mtime=OLD)
    nested = store / "threads" / "s_1"
    nested.mkdir(parents=True)
    (nested / "turn.log").write_text("latest turn", encoding="utf-8")
    _set_tree_mtime(store, OLD)
    os.utime(nested / "turn.log", (NEWER, NEWER))

    result = _copy("--refresh", str(home), str(volume))

    assert result.returncode == 0, result.stderr
    assert f"kept {volume / 'super-agents'}" in result.stdout
    assert _registry(volume) == '{"volume": true}'


def test_pre_upgrade_applies_the_rule_to_the_projects_file(tmp_path, volume) -> None:
    home = tmp_path / "home"
    legacy = home / ".openbase" / "coder-projects.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('[{"path": "/data/workspace/stale"}]', encoding="utf-8")
    durable = volume / "coder-projects.json"
    durable.write_text('[{"path": "/data/workspace/live"}]', encoding="utf-8")
    os.utime(legacy, (OLD, OLD))
    os.utime(durable, (NEWER, NEWER))

    result = _copy("--refresh", str(home), str(volume))
    assert result.returncode == 0, result.stderr
    assert f"kept {durable}" in result.stdout
    assert json.loads(durable.read_text(encoding="utf-8")) == [
        {"path": "/data/workspace/live"}
    ]
    assert not list(volume.glob("coder-projects.json.replaced-*"))

    os.utime(legacy, (NEWER + 1, NEWER + 1))
    result = _copy("--refresh", str(home), str(volume))
    assert result.returncode == 0, result.stderr
    assert json.loads(durable.read_text(encoding="utf-8")) == [
        {"path": "/data/workspace/stale"}
    ]
    assert len(list(volume.glob("coder-projects.json.replaced-*"))) == 1


def test_pre_upgrade_refresh_on_a_workspace_woken_on_an_older_image_keeps_its_threads(
    tmp_path, volume, monkeypatch
) -> None:
    # Staging workspace 374, 2026-10-10: the upgrade adopted the layer's store
    # onto the volume, threads were created there, then a wake on the older
    # image brought the layer's stale real store back, and the next
    # --refresh copied it over the volume.
    layer = tmp_path / "layer" / "home"
    layer.mkdir(parents=True)
    _store(monkeypatch, layer).create_session("dispatcher", cwd=str(tmp_path))
    stale_layer = tmp_path / "stale-layer"
    shutil.copytree(layer, stale_layer, symlinks=True)
    _boot(layer, volume)
    call = _store(monkeypatch, layer).create_session(
        "call", cwd=str(tmp_path), agent_name="Sunny"
    )
    assert newest_mtime_ns(volume / "super-agents-claude-code") > newest_mtime_ns(
        stale_layer / ".local" / "share" / "super-agents-claude-code"
    )

    result = _copy("--refresh", str(stale_layer), str(volume))

    assert result.returncode == 0, result.stderr
    assert "set aside" not in result.stdout
    assert not list(volume.glob("super-agents-claude-code.replaced-*"))
    assert _store(monkeypatch, layer).get_session(call.id).agent_name == "Sunny"


# --- persist-home-state.sh (boot) --------------------------------------------


def test_boot_keeps_a_newer_volume_copy_over_the_layers_store(tmp_path, volume) -> None:
    home = _home_with_store(tmp_path, "home", '{"stale": true}', mtime=OLD)
    _volume_store(volume, '{"live": true}', mtime=NEWER)

    output = _boot(home, volume)

    [line] = output.splitlines()
    assert line.startswith(f"[persist-home-state] kept {volume / 'super-agents'}")
    assert "at least as new" in line and "retired" in line
    assert (home / ".super-agents").is_symlink()
    assert (home / ".super-agents").resolve() == (volume / "super-agents").resolve()
    assert _registry(volume) == '{"live": true}'
    assert _parked(volume) == []
    [retired] = sorted(home.glob(".super-agents.migrated-*"))
    assert (retired / "state" / "state.json").read_text(
        encoding="utf-8"
    ) == '{"stale": true}'


def test_boot_keeps_a_volume_copy_as_new_as_the_layers_store(tmp_path, volume) -> None:
    home = _home_with_store(tmp_path, "home", '{"layer": true}', mtime=OLD)
    _volume_store(volume, '{"volume": true}', mtime=OLD)

    output = _boot(home, volume)

    assert "kept" in output and "adopted" not in output
    assert (home / ".super-agents").is_symlink()
    assert _registry(volume) == '{"volume": true}'
    assert _parked(volume) == []


def test_boot_adopts_a_newer_layer_store_and_parks_the_volume_copy(
    tmp_path, volume
) -> None:
    home = _home_with_store(tmp_path, "home", '{"live": true}', mtime=NEWER)
    _volume_store(volume, '{"stale": true}', mtime=OLD)

    output = _boot(home, volume)

    [line] = output.splitlines()
    assert line.startswith(f"[persist-home-state] adopted {home / '.super-agents'}")
    assert "newer than the volume copy" in line and "kept as" in line
    assert (home / ".super-agents").is_symlink()
    assert _registry(volume) == '{"live": true}'
    [parked] = _parked(volume)
    assert (parked / "state" / "state.json").read_text(
        encoding="utf-8"
    ) == '{"stale": true}'


def test_boot_adopts_the_layer_store_when_the_volume_has_none(tmp_path, volume) -> None:
    home = _home_with_store(tmp_path, "home", '{"first": true}', mtime=OLD)

    output = _boot(home, volume)

    [line] = output.splitlines()
    assert "adopted" in line and "held no copy" in line
    assert (home / ".super-agents").is_symlink()
    assert _registry(volume) == '{"first": true}'


def test_boot_freshness_covers_the_whole_tree(tmp_path, volume) -> None:
    home = _home_with_store(tmp_path, "home", '{"layer": true}', mtime=OLD + 60)
    store = _volume_store(volume, '{"volume": true}', mtime=OLD)
    nested = store / "threads" / "s_1"
    nested.mkdir(parents=True)
    (nested / "turn.log").write_text("latest turn", encoding="utf-8")
    _set_tree_mtime(store, OLD)
    os.utime(nested / "turn.log", (NEWER, NEWER))

    output = _boot(home, volume)

    assert "kept" in output
    assert _registry(volume) == '{"volume": true}'


def test_wake_on_an_older_image_keeps_the_threads_created_since(
    tmp_path, volume, monkeypatch
) -> None:
    # Boot on image A, create threads (they land on the volume), then wake on
    # the older image whose layer still holds A's pre-adoption store.
    layer = tmp_path / "layer-a" / "home"
    layer.mkdir(parents=True)
    dispatcher = _store(monkeypatch, layer).create_session(
        "dispatcher", cwd=str(tmp_path)
    )
    stale_layer = tmp_path / "layer-a-again" / "home"
    shutil.copytree(layer, stale_layer, symlinks=True)
    assert _boot(layer, volume).count("adopted") == 1
    sunny = _store(monkeypatch, layer).create_session(
        "tic-tac-toe", cwd=str(tmp_path), agent_name="Sunny"
    )
    shutil.rmtree(tmp_path / "layer-a")

    output = _boot(stale_layer, volume)

    assert "kept" in output and "adopted" not in output
    assert not list(volume.glob("*.replaced-*"))
    woken = _store(monkeypatch, stale_layer)
    assert {session.id for session in woken.list_sessions()} == {
        dispatcher.id,
        sunny.id,
    }
    assert woken.get_session(sunny.id).agent_name == "Sunny"
    # A later restart of that same layer is a no-op.
    assert _boot(stale_layer, volume) == ""

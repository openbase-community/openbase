import json
import os
import time
from pathlib import Path

from openbase_coder_cli.thread_sync.thread_sync_common import (
    KEEP_LATEST_SNAPSHOTS_PER_ENTITY,
    prune_exchange_snapshots,
    translate_home_path,
)


def test_translate_home_path_uses_explicit_source_home() -> None:
    assert (
        translate_home_path(
            "/Users/example/Projects/openbase/code/openbase-coder-workspace",
            source_home=Path("/Users/example"),
            target_home=Path("/home/ubuntu"),
        )
        == "/home/ubuntu/Projects/openbase/code/openbase-coder-workspace"
    )


def test_translate_home_path_recognizes_legacy_mac_and_linux_homes() -> None:
    assert (
        translate_home_path(
            "/Users/example/Developer/tool", target_home=Path("/home/ubuntu")
        )
        == "/home/ubuntu/Developer/tool"
    )
    assert (
        translate_home_path(
            "/home/ubuntu/Projects/app", target_home=Path("/Users/example")
        )
        == "/Users/example/Projects/app"
    )


def test_translate_home_path_preserves_paths_outside_user_home() -> None:
    assert (
        translate_home_path(
            "/tmp/nonexistent/project", target_home=Path("/home/ubuntu")
        )
        == "/tmp/nonexistent/project"
    )


def _write_exchange_snapshot(
    exchange_dir: Path,
    *,
    device_id: str,
    entity_id: str,
    fingerprint: str,
    exported_at: float | None,
) -> Path:
    snapshot_dir = (
        exchange_dir / "devices" / device_id / "snapshots" / entity_id / fingerprint
    )
    snapshot_dir.mkdir(parents=True)
    metadata: dict[str, float] = {}
    if exported_at is not None:
        metadata["exported_at"] = exported_at
    (snapshot_dir / "metadata.json").write_text(json.dumps(metadata))
    (snapshot_dir / "payload.jsonl").write_text("{}")
    return snapshot_dir


def test_prune_exchange_snapshots_removes_expired_and_superseded(
    tmp_path: Path,
) -> None:
    now = time.time()
    day = 24 * 60 * 60
    fresh = [
        _write_exchange_snapshot(
            tmp_path,
            device_id="device-a",
            entity_id="thread-1",
            fingerprint=f"fp-{index}",
            exported_at=now - index * 60,
        )
        for index in range(KEEP_LATEST_SNAPSHOTS_PER_ENTITY + 2)
    ]
    expired = _write_exchange_snapshot(
        tmp_path,
        device_id="device-b",
        entity_id="thread-2",
        fingerprint="fp-old",
        exported_at=now - 20 * day,
    )

    removed = prune_exchange_snapshots(tmp_path, max_age_days=15)

    assert removed == 3
    kept = fresh[:KEEP_LATEST_SNAPSHOTS_PER_ENTITY]
    assert all(path.exists() for path in kept)
    assert not any(path.exists() for path in fresh[KEEP_LATEST_SNAPSHOTS_PER_ENTITY:])
    assert not expired.exists()
    # The emptied entity directory is removed with its last snapshot.
    assert not expired.parent.exists()


def test_prune_exchange_snapshots_noop_without_age_limit(tmp_path: Path) -> None:
    snapshot = _write_exchange_snapshot(
        tmp_path,
        device_id="device-a",
        entity_id="thread-1",
        fingerprint="fp-old",
        exported_at=time.time() - 400 * 24 * 60 * 60,
    )

    assert prune_exchange_snapshots(tmp_path, max_age_days=None) == 0
    assert snapshot.exists()


def test_prune_exchange_snapshots_spares_snapshots_still_materializing(
    tmp_path: Path,
) -> None:
    partial = tmp_path / "devices" / "device-a" / "snapshots" / "thread-1" / "fp-new"
    partial.mkdir(parents=True)
    (partial / "payload.jsonl").write_text("{}")
    old_ns = int((time.time() - 30 * 24 * 60 * 60) * 1e9)
    os.utime(partial, ns=(old_ns, old_ns))

    assert prune_exchange_snapshots(tmp_path, max_age_days=15) == 0
    assert partial.exists()


def test_prune_exchange_snapshots_uses_mtime_when_metadata_is_unreadable(
    tmp_path: Path,
) -> None:
    snapshot = _write_exchange_snapshot(
        tmp_path,
        device_id="device-a",
        entity_id="thread-1",
        fingerprint="fp-broken",
        exported_at=None,
    )
    (snapshot / "metadata.json").write_text("not json")
    old_ns = int((time.time() - 30 * 24 * 60 * 60) * 1e9)
    os.utime(snapshot, ns=(old_ns, old_ns))

    assert prune_exchange_snapshots(tmp_path, max_age_days=15) == 1
    assert not snapshot.exists()

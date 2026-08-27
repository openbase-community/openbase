from __future__ import annotations

import json
from pathlib import Path

import pytest

from openbase_coder_cli import sync_config


def test_folder_id_is_deterministic_and_prefixed() -> None:
    first = sync_config.folder_id_for_relpath("Projects/myapp")
    second = sync_config.folder_id_for_relpath("Projects/myapp")
    other = sync_config.folder_id_for_relpath("Projects/otherapp")

    assert first == second
    assert first != other
    assert first.startswith("cs-")
    assert len(first) == len("cs-") + 16
    assert all(char in "0123456789abcdef" for char in first[len("cs-") :])


@pytest.mark.parametrize(
    "relpath",
    [
        "",
        "   ",
        "/Users/zoe/Projects",
        "~/Projects",
        "a/../..",
        "../etc",
        ".openbase/skills",
    ],
)
def test_validate_relpath_rejects_invalid_paths(relpath: str) -> None:
    with pytest.raises(ValueError):
        sync_config.validate_relpath(relpath)


def test_validate_relpath_normalizes() -> None:
    assert sync_config.validate_relpath("Projects/myapp/") == "Projects/myapp"
    assert sync_config.validate_relpath("/Projects/myapp"[1:]) == "Projects/myapp"


def test_relpath_for_path_requires_home(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        sync_config.relpath_for_path(tmp_path / "outside-home")

    under_home = Path.home() / "Projects" / "demo"
    assert sync_config.relpath_for_path(under_home) == "Projects/demo"


def test_read_sync_config_refuses_newer_schema(tmp_path: Path) -> None:
    config_path = tmp_path / "sync-config.json"
    config_path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")

    with pytest.raises(ValueError, match="newer Openbase Coder"):
        sync_config.read_sync_config(config_path)


def test_enabled_and_lease_roundtrip(tmp_path: Path) -> None:
    config_path = tmp_path / "sync-config.json"

    assert sync_config.code_sync_enabled(config_path) is False
    sync_config.set_code_sync_enabled(True, config_path)
    assert sync_config.code_sync_enabled(config_path) is True

    assert sync_config.lease_mode(config_path) == "auto"
    sync_config.set_lease_mode("manual", config_path)
    assert sync_config.lease_mode(config_path) == "manual"
    with pytest.raises(ValueError):
        sync_config.set_lease_mode("bogus", config_path)

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == sync_config.SYNC_CONFIG_SCHEMA_VERSION


def test_folder_list_replace_add_remove(tmp_path: Path) -> None:
    config_path = tmp_path / "sync-config.json"

    folders = sync_config.set_sync_folders(
        [
            {"relpath": "Projects/one", "extra_ignores": ["*.log"]},
            {"relpath": "Projects/two/"},
            {"relpath": "Projects/one"},  # duplicate collapses
        ],
        config_path,
    )
    assert [folder.relpath for folder in folders] == [
        "Projects/one",
        "Projects/two",
    ]
    assert folders[0].extra_ignores == ("*.log",)

    sync_config.add_sync_folder("Projects/three", config_path)
    assert [f.relpath for f in sync_config.sync_folders(config_path)] == [
        "Projects/one",
        "Projects/two",
        "Projects/three",
    ]

    assert sync_config.remove_sync_folder("Projects/two", config_path) is True
    assert sync_config.remove_sync_folder("Projects/two", config_path) is False
    remaining = sync_config.sync_folders(config_path)
    assert [f.relpath for f in remaining] == ["Projects/one", "Projects/three"]

    found = sync_config.folder_for_id(remaining[0].folder_id, config_path)
    assert found is not None and found.relpath == "Projects/one"
    assert sync_config.folder_for_id("cs-unknown", config_path) is None


def test_add_and_remove_folder_ignore(tmp_path: Path) -> None:
    config_path = tmp_path / "sync-config.json"
    sync_config.set_sync_folders(
        [{"relpath": "Projects/one"}, {"relpath": "Projects/two"}],
        config_path,
    )

    updated = sync_config.add_folder_ignore("Projects/one", "*.log", config_path)
    assert updated.extra_ignores == ("*.log",)
    # Duplicate is a no-op, whitespace is trimmed, other folders untouched.
    sync_config.add_folder_ignore("Projects/one", "  *.log  ", config_path)
    sync_config.add_folder_ignore("Projects/one", "/build", config_path)
    folders = {f.relpath: f for f in sync_config.sync_folders(config_path)}
    assert folders["Projects/one"].extra_ignores == ("*.log", "/build")
    assert folders["Projects/two"].extra_ignores == ()

    assert sync_config.remove_folder_ignore("Projects/one", "*.log", config_path)
    assert not sync_config.remove_folder_ignore("Projects/one", "*.log", config_path)
    assert sync_config.folder_for_relpath(
        "Projects/one", config_path
    ).extra_ignores == ("/build",)


def test_add_folder_ignore_validates(tmp_path: Path) -> None:
    config_path = tmp_path / "sync-config.json"
    sync_config.set_sync_folders([{"relpath": "Projects/one"}], config_path)
    with pytest.raises(ValueError):
        sync_config.add_folder_ignore("Projects/one", "   ", config_path)
    with pytest.raises(ValueError):
        sync_config.add_folder_ignore("Projects/missing", "*.log", config_path)


def test_set_sync_folders_rejects_invalid_entries(tmp_path: Path) -> None:
    config_path = tmp_path / "sync-config.json"
    with pytest.raises(ValueError):
        sync_config.set_sync_folders([{"relpath": "../evil"}], config_path)
    assert sync_config.sync_folders(config_path) == ()


def test_product_state_relpaths_allowed_other_openbase_rejected():
    import pytest

    from openbase_coder_cli import sync_config

    for relpath in sync_config.PRODUCT_STATE_RELPATHS:
        assert sync_config.validate_relpath(relpath) == relpath

    for bad in (
        ".openbase",
        ".openbase/auth-things",
        ".openbase/legacy-managed",
        ".openbase/logs",
        ".openbase/thread-sync/../db",
    ):
        with pytest.raises(ValueError):
            sync_config.validate_relpath(bad)


def test_remove_legacy_openbase_folder_is_not_blocked_by_add_guard(
    tmp_path: Path,
) -> None:
    """A folder registered before the ~/.openbase add-guard (or grandfathered
    in) must still be removable — removal is never gated by add-time policy."""
    config_path = tmp_path / "sync-config.json"
    # Write a legacy registry entry directly: `set_sync_folders` would now
    # reject this relpath, mirroring a config from an older CLI.
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "folders": [
                    {"relpath": "Projects", "extra_ignores": []},
                    {"relpath": ".openbase/legacy-managed/skills", "extra_ignores": []},
                ],
            }
        ),
        encoding="utf-8",
    )

    # The guard still blocks *adding* it back.
    with pytest.raises(ValueError):
        sync_config.validate_relpath(".openbase/legacy-managed/skills")

    assert (
        sync_config.remove_sync_folder(".openbase/legacy-managed/skills", config_path)
        is True
    )
    assert [f.relpath for f in sync_config.sync_folders(config_path)] == ["Projects"]
    # Idempotent: removing an absent folder returns False, no raise.
    assert (
        sync_config.remove_sync_folder(".openbase/legacy-managed/skills", config_path)
        is False
    )


def test_relpath_for_path_guard_toggle() -> None:
    home = Path.home()
    legacy = home / ".openbase" / "legacy-managed" / "skills"
    with pytest.raises(ValueError):
        sync_config.relpath_for_path(legacy)
    assert (
        sync_config.relpath_for_path(legacy, guard=False)
        == ".openbase/legacy-managed/skills"
    )

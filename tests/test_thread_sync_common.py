from pathlib import Path

from openbase_coder_cli.thread_sync.thread_sync_common import translate_home_path


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

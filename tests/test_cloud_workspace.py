from pathlib import Path

from openbase_coder_cli.services.cloud_workspace import cloud_workspace_id


def test_cloud_workspace_id_prefers_explicit_marker(tmp_path: Path) -> None:
    public_id_path = tmp_path / "devspace-public-id"
    hostname_path = tmp_path / "devspace-tailscale-hostname"
    public_id_path.write_text("abc123def456\n", encoding="utf-8")
    hostname_path.write_text("devspace-legacy123456\n", encoding="utf-8")

    assert (
        cloud_workspace_id(
            public_id_path=public_id_path,
            tailscale_hostname_path=hostname_path,
        )
        == "abc123def456"
    )


def test_cloud_workspace_id_recovers_legacy_hostname_marker(tmp_path: Path) -> None:
    public_id_path = tmp_path / "missing-public-id"
    hostname_path = tmp_path / "devspace-tailscale-hostname"
    hostname_path.write_text("devspace-abc123def456\n", encoding="utf-8")

    assert (
        cloud_workspace_id(
            public_id_path=public_id_path,
            tailscale_hostname_path=hostname_path,
        )
        == "abc123def456"
    )


def test_cloud_workspace_id_ignores_ordinary_machine(tmp_path: Path) -> None:
    public_id_path = tmp_path / "missing-public-id"
    hostname_path = tmp_path / "devspace-tailscale-hostname"
    hostname_path.write_text("example-macbook\n", encoding="utf-8")

    assert (
        cloud_workspace_id(
            public_id_path=public_id_path,
            tailscale_hostname_path=hostname_path,
        )
        is None
    )


def test_cloud_workspace_id_ignores_unreadable_marker_content(tmp_path: Path) -> None:
    public_id_path = tmp_path / "devspace-public-id"
    hostname_path = tmp_path / "devspace-tailscale-hostname"
    public_id_path.write_bytes(b"\xff\xfe")
    hostname_path.write_text("devspace-abc123def456\n", encoding="utf-8")

    assert (
        cloud_workspace_id(
            public_id_path=public_id_path,
            tailscale_hostname_path=hostname_path,
        )
        == "abc123def456"
    )

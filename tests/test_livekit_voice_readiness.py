from __future__ import annotations

from openbase_coder_cli.openbase_coder_cli_app import livekit


def test_recent_worker_webrtc_timeout_is_actionable(monkeypatch, tmp_path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "livekit-agent.log").write_text(
        "registered worker\nwait_pc_connection timed out\n", encoding="utf-8"
    )
    monkeypatch.setattr(livekit, "DEFAULT_LOG_DIR", log_dir)

    error = livekit._recent_worker_failure()

    assert error is not None
    assert error.code == "worker_webrtc_unavailable"
    assert "Restart the LiveKit" in error.detail


def test_worker_log_without_failure_does_not_block_call(monkeypatch, tmp_path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "livekit-agent.log").write_text(
        "registered worker\nagent_session_start_complete\n", encoding="utf-8"
    )
    monkeypatch.setattr(livekit, "DEFAULT_LOG_DIR", log_dir)

    assert livekit._recent_worker_failure() is None

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openbase_coder_cli.livekit_agent.super_agents_client import (
    SuperAgentsLiveKitClient,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("matching", [True, False])
async def test_speech_can_deliver_finished_response_while_background_login_waits(
    tmp_path, monkeypatch, matching
):
    monkeypatch.setattr(
        "openbase_coder_cli.livekit_agent.super_agents_client.TURN_POLL_INTERVAL_SECONDS",
        0.001,
    )
    progress = {
        "status": "running",
        "threadId": "phone",
        "turnId": "login",
        "turn": {
            "turnId": "login" if matching else "earlier",
            "responseFinishedAt": "2026-10-10T04:16:10Z",
            "lastUsefulMessage": "Enter device code EXAMPLE on your phone.",
        },
    }
    completed = {
        "status": "completed",
        "turnId": "login",
        "turn": {"turnId": "login", "lastUsefulMessage": "Done."},
    }
    backend = SimpleNamespace(
        backend="claude_code",
        progress_by_label=AsyncMock(side_effect=[progress, completed]),
    )
    client = SuperAgentsLiveKitClient(
        cwd=str(tmp_path),
        state_path=str(tmp_path / "voice.json"),
        backend_client=backend,
    )
    result = await asyncio.wait_for(
        client._poll_turn_until_ready("phone", "login"), timeout=1
    )
    assert result == (progress if matching else completed)
    assert backend.progress_by_label.await_count == (1 if matching else 2)

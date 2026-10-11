from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openbase_coder_cli.livekit_agent import livekit


@pytest.mark.parametrize("threshold", [None, "inf"])
async def test_single_user_worker_reports_dispatchable_load_to_livekit(
    monkeypatch, threshold
):
    monkeypatch.delenv(livekit.LIVEKIT_AGENT_LOAD_THRESHOLD_ENV, raising=False)
    if threshold is not None:
        monkeypatch.setenv(livekit.LIVEKIT_AGENT_LOAD_THRESHOLD_ENV, threshold)
    server = livekit.LiveKitAgentServer(**livekit._livekit_agent_server_options())
    server._proc_pool = SimpleNamespace(processes=[])
    server._devmode = False
    server._simulation = False
    server._reserved_slots = 0
    server._previous_status = 0
    server._draining = False
    server._worker_load = 0.95
    queued = AsyncMock()
    monkeypatch.setattr(server, "_queue_msg", queued)
    await server._refresh_worker_load()
    await server._update_worker_status()
    update = queued.call_args.args[0].update_worker
    assert update.load == 0
    # LiveKit's server uses max(0, target_load - load) even for AVAILABLE
    # workers. Both checks must admit a call, including on a 95%-busy host.
    assert update.status == 0  # WS_AVAILABLE
    assert max(0, 0.7 - update.load) > 0


def test_shared_deployment_retains_cpu_load_calculation(monkeypatch):
    monkeypatch.setenv(livekit.LIVEKIT_AGENT_LOAD_THRESHOLD_ENV, "0.6")
    options = livekit._livekit_agent_server_options()
    assert options["load_threshold"] == 0.6
    assert "load_fnc" not in options

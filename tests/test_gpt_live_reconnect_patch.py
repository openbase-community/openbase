"""Queued input audio must not replay into a reconnected GPT-Live session."""

from livekit.agents.utils.aio import Chan
from livekit.plugins.openai.realtime import gpt_live_model
from livekit.plugins.openai.realtime import gpt_live_types as types

from openbase_coder_cli.livekit_agent.gpt_live_reconnect_patch import (
    drain_stale_input_audio,
    install_gpt_live_reconnect_patch,
)


def test_drain_drops_audio_and_keeps_context_appends_in_order():
    channel = Chan()
    channel.send_nowait(types.InputAudioAppendEvent(audio="AAAA"))
    channel.send_nowait(types.ThinkingAppendEvent.model_construct(delegation_id=None, content=[]))
    channel.send_nowait({"type": "session.input_audio.append", "audio": "BBBB"})
    channel.send_nowait(types.InstructionsAppendEvent.model_construct(delegation_id=None, content=[]))
    channel.send_nowait(types.InputAudioAppendEvent(audio="CCCC"))
    assert drain_stale_input_audio(channel) == 3
    remaining = []
    while True:
        try:
            remaining.append(channel.recv_nowait())
        except Exception:
            break
    assert [getattr(e, "type", None) for e in remaining] == [
        "session.thinking.append",
        "session.instructions.append",
    ]
    assert drain_stale_input_audio(channel) == 0


def test_patch_installs_once_and_drains_before_the_plugin_reset(monkeypatch):
    calls = []
    monkeypatch.setattr(
        gpt_live_model.GPTLiveSession,
        "_reset_for_reconnect",
        lambda self: calls.append("reset"),
    )
    monkeypatch.setattr(
        gpt_live_model.GPTLiveSession,
        "_openbase_reconnect_patched",
        False,
        raising=False,
    )
    assert install_gpt_live_reconnect_patch()
    assert install_gpt_live_reconnect_patch()  # idempotent
    channel = Chan()
    channel.send_nowait(types.InputAudioAppendEvent(audio="AAAA"))
    channel.send_nowait(types.CommentaryAppendEvent.model_construct(delegation_id=None, content=[]))
    stand_in = type("StandIn", (), {"_msg_ch": channel})()
    gpt_live_model.GPTLiveSession._reset_for_reconnect(stand_in)
    assert calls == ["reset"]
    assert channel.recv_nowait().type == "session.commentary.append"

"""Playback ownership, including provider output after its no-op interrupt."""

import asyncio
import base64
from array import array

from livekit.agents import AgentSession, llm
from livekit.agents.voice import io
from livekit.plugins.openai.realtime import GPTLiveModel
from test_live_voice import FakeGPTLiveServer

from openbase_coder_cli.livekit_agent.live_characters import CharacterAssistant
from openbase_coder_cli.livekit_agent.live_preconnect import wait_live_session_started
from openbase_coder_cli.livekit_agent.live_speech_gate import LiveSpeechGate


async def test_bridge_opens_speech_only_for_current_commentary_and_revokes_on_handoff():
    from test_live_delegation import _make_bridge

    bridge, live, *_ = _make_bridge()
    try:
        assert not bridge.speech_gate.authorized
        bridge._append_thinking("Working", None)
        assert not bridge.speech_gate.authorized
        bridge._append_commentary("Blake.", None)
        assert bridge.speech_gate.authorized
        bridge.on_user_state_changed("listening", "speaking")
        bridge.on_user_state_changed("speaking", "listening")
        assert not bridge.speech_gate.authorized
        bridge._append_commentary("The new answer.", None)
        assert bridge.speech_gate.authorized
        bridge.suspend_session()
        assert not bridge.speech_gate.authorized
    finally:
        await bridge.aclose()


async def test_caller_interrupt_discards_stale_tail_even_after_new_authorization():
    gate = LiveSpeechGate()
    gate.authorize()

    async def frames():
        yield "current answer"
        gate.user_state_changed("speaking")
        yield "over caller"
        gate.user_state_changed("listening")
        gate.authorize()
        yield "stale tail"

    assert [frame async for frame in gate.filter_audio(frames())] == ["current answer"]

    async def reply():
        yield "new authoritative answer"

    assert [frame async for frame in gate.filter_audio(reply())] == [
        "new authoritative answer"
    ]


async def test_early_name_is_not_unmuted_mid_burst_by_backend_result():
    gate = LiveSpeechGate()

    async def frames():
        yield "Gemma"
        gate.authorize()
        yield "Gemma again"

    assert [frame async for frame in gate.filter_audio(frames())] == []


class RecordingOutput(io.AudioOutput):
    def __init__(self):
        super().__init__(
            label="test", capabilities=io.AudioOutputCapabilities(pause=False)
        )
        self.frames = []
        self.clears = 0

    async def capture_frame(self, frame):
        await super().capture_frame(frame)
        self.frames.append(frame)

    def flush(self):
        super().flush()
        self.on_playback_finished(playback_position=0, interrupted=False)

    def clear_buffer(self):
        self.clears += 1
        self.on_playback_finished(playback_position=0, interrupted=True)


async def test_real_sdk_output_node_blocks_early_audio_and_passes_authorized_reply():
    # Real plugin, adapter and AgentSession; PCM stays in memory, no speakers,
    # room, external provider, paid synthesis or mocked SDK internals.
    gate = LiveSpeechGate()
    sink = RecordingOutput()
    async with FakeGPTLiveServer() as server:
        model = GPTLiveModel(
            voice="cedar",
            delegation="client",
            api_key="cloud-token",
            base_url=server.base_url,
        )
        assistant = CharacterAssistant(
            model=model,
            instructions="Blake",
            history=llm.ChatContext(),
            speech_gate=gate,
        )
        session = AgentSession()
        session.output.audio = sink

        async def send_audio(value, samples=4800):
            await server.send(
                {
                    "type": "session.output_audio.delta",
                    "delta": base64.b64encode(array("h", [value] * samples)).decode(),
                }
            )

        async def drain_burst():
            await send_audio(0, 24000)
            await asyncio.sleep(0.15)

        try:
            await session.start(agent=assistant)
            await wait_live_session_started(assistant.duplex_session, timeout=5)
            gate.user_state_changed("speaking")
            await server.send(
                {
                    "type": "session.output_transcript.delta",
                    "delta": "Gemma.",
                    "start_ms": 0,
                    "end_ms": 200,
                }
            )
            await send_audio(5000)
            await asyncio.sleep(0.05)
            assert sink.frames == []
            gate.user_state_changed("listening")
            gate.authorize()
            await send_audio(6000)  # same early burst: still forbidden
            await drain_burst()
            assert sink.frames == []
            assert not [
                item
                for item in session.history.items
                if isinstance(item, llm.ChatMessage) and item.role == "assistant"
            ]
            await send_audio(7000)  # fresh backend-authorized burst
            async with asyncio.timeout(5):
                while not sink.frames:
                    await asyncio.sleep(0.01)
            assert any(7000 in frame.data for frame in sink.frames)
            gate.user_state_changed("speaking")
            await session.interrupt(force=True)
            count = len(sink.frames)
            await send_audio(8000)  # provider continues after interrupt
            await drain_burst()
            assert len(sink.frames) == count
            assert sink.clears > 0
        finally:
            await session.aclose()
            await model.aclose()

"""Playback ownership, including provider output after its no-op interrupt."""

import asyncio
import base64
from array import array

from livekit import rtc
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
    gate = LiveSpeechGate(barge_in_min_seconds=0)
    gate.authorize()

    async def frames():
        yield "current answer"
        gate.user_state_changed("speaking")
        gate.caller_heard(" hold on")  # over the agent, words prove the caller
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
    gate = LiveSpeechGate(barge_in_min_seconds=0)

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


async def test_real_sdk_output_node_blocks_early_audio_and_passes_authorized_reply(
    caplog,
):
    # Real plugin, adapter and AgentSession; PCM stays in memory, no speakers,
    # room, external provider, paid synthesis or mocked SDK internals.
    gate = LiveSpeechGate(barge_in_min_seconds=0)
    caplog.set_level("INFO", logger="openbase_coder_cli.livekit_agent.live_speech_gate")
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
            pcm_events = [
                r.message
                for r in caplog.records
                if "stage=live_pcm_forwarded " in r.message
            ]
            assert len(pcm_events) == 1
            assert "peak=7000" in pcm_events[0]
            assert any(
                "frames=0" in r.message and "live_pcm_segment_end" in r.message
                for r in caplog.records
            )
            gate.user_state_changed("speaking")
            gate.caller_heard(" hold on")
            await session.interrupt(force=True)
            count = len(sink.frames)
            await send_audio(8000)  # provider continues after interrupt
            await drain_burst()
            assert len(sink.frames) == count
            assert sink.clears > 0
        finally:
            await session.aclose()
            await model.aclose()


async def test_short_caller_blip_does_not_cut_the_answer_but_sustained_speech_does():
    """Speakerphone echo trips the VAD for a moment; a real interruption lasts."""
    clock = {"now": 10.0}
    gate = LiveSpeechGate(barge_in_min_seconds=0.5, clock=lambda: clock["now"])
    gate.authorize()

    async def answer():
        yield "part one"
        gate.user_state_changed("speaking")  # "...uper" echoing back
        clock["now"] += 0.2
        yield "part two"
        gate.user_state_changed("listening")
        yield "part three"
        gate.user_state_changed("speaking")  # the caller really talks over it
        clock["now"] += 0.6
        gate.caller_heard(" wait, hold on")
        yield "cut"
        yield "cut too"

    heard = [frame async for frame in gate.filter_audio(answer())]
    assert heard == ["part one", "part two", "part three"]
    assert gate.barge_ins_ignored == 1
    assert not gate.authorized


async def test_sustained_speech_with_no_burst_in_flight_still_revokes_on_stop():
    clock = {"now": 10.0}
    gate = LiveSpeechGate(barge_in_min_seconds=0.5, clock=lambda: clock["now"])
    gate.authorize()
    gate.user_state_changed("speaking")
    clock["now"] += 0.7
    gate.user_state_changed("listening")
    assert not gate.authorized

    gate.authorize()
    gate.user_state_changed("speaking")
    clock["now"] += 0.1
    gate.user_state_changed("listening")
    assert gate.authorized


async def test_answer_authorized_during_a_discarded_burst_is_reported_starved():
    """2026-10-10: the model folded the owed answer into speech the gate was
    discarding, so the caller saw the caption and heard nothing."""
    gate = LiveSpeechGate(barge_in_min_seconds=0)
    starved = []
    gate.on_starved = lambda: starved.append(True)

    async def unsolicited():
        yield "the model answers on its own"
        gate.authorize()  # the backend answer arrives mid-burst
        yield "and reads the backend answer into the same breath"

    assert [frame async for frame in gate.filter_audio(unsolicited())] == []
    assert starved == [True]

    async def permitted():
        yield "the re-asked answer"

    assert [frame async for frame in gate.filter_audio(permitted())] == [
        "the re-asked answer"
    ]
    assert starved == [True]


async def test_a_permitted_burst_is_never_reported_starved():
    gate = LiveSpeechGate(barge_in_min_seconds=0)
    gate.on_starved = lambda: (_ for _ in ()).throw(AssertionError("starved"))
    gate.authorize()

    async def reply():
        yield "answer"
        gate.authorize()
        yield "more"

    assert [frame async for frame in gate.filter_audio(reply())] == ["answer", "more"]


async def test_a_bounded_permit_covers_one_burst_and_cuts_the_models_continuation():
    gate = LiveSpeechGate(barge_in_min_seconds=0)
    gate.authorize(limit_ms=40)

    def frame():
        return rtc.AudioFrame(
            data=b"\x00\x00" * 480,
            sample_rate=24000,
            num_channels=1,
            samples_per_channel=480,
        )

    async def ack_then_self_answer():
        yield frame()  # 20 ms "One"
        yield frame()  # 20 ms "moment"
        yield frame()  # the model keeps going on its own
        yield frame()

    heard = [f async for f in gate.filter_audio(ack_then_self_answer())]
    assert len(heard) == 2
    assert not gate.authorized

    # A spent bounded permit does not carry over to the next burst.
    gate.authorize(limit_ms=40)

    async def short_ack():
        yield frame()

    assert len([f async for f in gate.filter_audio(short_ack())]) == 1
    assert not gate.authorized

    async def self_answer():
        yield frame()

    assert [f async for f in gate.filter_audio(self_answer())] == []


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _open_permitted_burst(gate):
    from openbase_coder_cli.livekit_agent.live_speech_gate import _SpeechBurst

    gate.authorize()
    return _SpeechBurst(gate)


def test_echo_of_the_agent_does_not_barge_in_over_its_own_answer():
    # Maritime 376, 2026-10-10: speakerphone echo kept VAD "speaking" for
    # 0.5 s and every answer was cut after about a second.
    clock = _Clock()
    gate = LiveSpeechGate(barge_in_min_seconds=0.5, clock=clock)
    interrupts = []
    gate.on_barge_in = lambda: interrupts.append(clock.now)
    burst = _open_permitted_burst(gate)
    gate.agent_said("I'm Marian. Nine plus five is fourteen.")
    gate.user_state_changed("speaking")
    clock.now += 2.0
    assert not gate.speaking
    gate.caller_heard(" nine plus five is")
    assert not gate.speaking
    gate.user_state_changed("listening")
    assert gate.authorized and burst.permitted
    assert interrupts == []


def test_caller_words_over_the_agent_barge_in_and_interrupt_once():
    clock = _Clock()
    gate = LiveSpeechGate(barge_in_min_seconds=0.5, clock=clock)
    interrupts = []
    gate.on_barge_in = lambda: interrupts.append(clock.now)
    _open_permitted_burst(gate)
    gate.agent_said("The answer is fourteen.")
    gate.user_state_changed("speaking")
    clock.now += 0.6
    assert not gate.speaking
    gate.caller_heard(" wait, stop")
    assert gate.speaking
    assert not gate.authorized
    gate.caller_heard(" please")
    gate.user_state_changed("listening")
    assert len(interrupts) == 1


def test_echo_tail_still_counts_as_the_agent_after_its_burst_ends():
    from openbase_coder_cli.livekit_agent.live_speech_gate import ECHO_TAIL_SECONDS

    clock = _Clock()
    gate = LiveSpeechGate(barge_in_min_seconds=0.5, clock=clock)
    burst = _open_permitted_burst(gate)
    gate._burst_closed(burst)
    clock.now += ECHO_TAIL_SECONDS / 2
    assert gate.agent_audible
    clock.now += ECHO_TAIL_SECONDS
    assert not gate.agent_audible


def test_caller_speech_with_the_agent_silent_keeps_the_vad_rule():
    clock = _Clock()
    gate = LiveSpeechGate(barge_in_min_seconds=0.5, clock=clock)
    interrupts = []
    gate.on_barge_in = lambda: interrupts.append(clock.now)
    gate.authorize()
    gate.user_state_changed("speaking")
    clock.now += 0.3
    assert not gate.speaking
    clock.now += 0.3
    assert gate.speaking
    assert not gate.authorized
    assert len(interrupts) == 1


def test_is_echo_compares_words_with_what_the_agent_just_said():
    clock = _Clock()
    gate = LiveSpeechGate(clock=clock)
    gate.agent_said("Nine plus five is fourteen.")
    assert gate.is_echo("five is fourteen")
    assert not gate.is_echo("hold on a second")
    clock.now += 60
    gate.agent_said("")
    assert not gate.is_echo("five is fourteen")


async def test_announcer_playout_counts_as_agent_audio_for_barge_in():
    """VM2 2026-10-11: the route announcer's echo counted as the caller, and
    the greeting burst right after it opened muted and was discarded."""
    clock = {"now": 10.0}
    gate = LiveSpeechGate(barge_in_min_seconds=0.5, clock=lambda: clock["now"])
    gate.external_playout_started("Voice route transferred.")
    assert gate.agent_audible
    gate.user_state_changed("speaking")  # the announcer's echo
    clock["now"] += 1.0
    assert not gate.speaking
    gate.external_playout_ended()
    gate.user_state_changed("listening")
    gate.authorize()

    async def greeting():
        yield "Hi, I'm Cooper."

    assert [f async for f in gate.filter_audio(greeting())] == ["Hi, I'm Cooper."]

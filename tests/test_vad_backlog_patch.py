from __future__ import annotations

import asyncio

from livekit import rtc
from livekit.agents.vad import VADStream
from livekit.plugins import silero

from openbase_coder_cli.livekit_agent.vad_backlog_patch import (
    BoundedVadInputChannel,
    install_vad_backlog_patch,
    set_vad_backlog_listener,
)


def _frame(seconds: float = 0.5, sample_rate: int = 16_000) -> rtc.AudioFrame:
    samples = int(seconds * sample_rate)
    return rtc.AudioFrame(
        data=b"\x00\x00" * samples,
        sample_rate=sample_rate,
        num_channels=1,
        samples_per_channel=samples,
    )


def test_bounded_channel_drops_oldest_audio_beyond_backlog():
    dropped: list[float] = []
    set_vad_backlog_listener(dropped.append)
    try:

        async def run() -> list[object]:
            channel = BoundedVadInputChannel(
                max_backlog_seconds=1.0,
                keep_seconds=0.6,
            )
            frames = [_frame() for _ in range(4)]
            for frame in frames:
                channel.send_nowait(frame)
            channel.close()
            received = [item async for item in channel]
            assert received == frames[2:]
            return received

        asyncio.run(run())
    finally:
        set_vad_backlog_listener(None)

    assert sum(dropped) == 1.0


def test_bounded_channel_preserves_flush_sentinels_when_dropping():
    async def run() -> None:
        channel = BoundedVadInputChannel(
            max_backlog_seconds=1.0,
            keep_seconds=0.6,
        )
        sentinel = VADStream._FlushSentinel()
        first, second, third = _frame(), _frame(), _frame()
        channel.send_nowait(first)
        channel.send_nowait(sentinel)
        channel.send_nowait(second)
        channel.send_nowait(third)
        channel.close()
        received = [item async for item in channel]
        assert received == [sentinel, third]

    asyncio.run(run())


def test_bounded_channel_keeps_small_backlogs_intact():
    async def run() -> None:
        channel = BoundedVadInputChannel(
            max_backlog_seconds=1.0,
            keep_seconds=0.6,
        )
        first, second = _frame(0.3), _frame(0.3)
        channel.send_nowait(first)
        channel.send_nowait(second)
        channel.close()
        received = [item async for item in channel]
        assert received == [first, second]

    asyncio.run(run())


def test_patch_installs_bounded_channel_on_real_silero_stream():
    install_vad_backlog_patch()

    async def run() -> None:
        vad = silero.VAD.load()
        stream = vad.stream()
        try:
            assert isinstance(stream._input_ch, BoundedVadInputChannel)
        finally:
            await stream.aclose()

    asyncio.run(run())

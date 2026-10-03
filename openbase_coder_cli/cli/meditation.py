"""Guided-meditation commands for long Super Agent tasks.

``meditation run`` is the detached worker that ``openbase-coder user intro``
starts for every newly dispatched task; ``meditation render`` turns a saved
script into audio without any model call, for checking voices and pauses.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import click

from openbase_coder_cli import task_meditation


@click.group()
def meditation() -> None:
    """Guided meditations that play while a long task runs."""


def _configure_worker_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@meditation.command("run")
@click.option("--thread-id", default="", help="Super Agent thread the task runs in.")
@click.option(
    "--thread-name", default="", help="Thread name; doubles as the task summary."
)
@click.option("--agent-name", default="", help="Speaking name of the Super Agent.")
@click.option(
    "--task",
    "task_text",
    default="",
    help="Task text to estimate instead of the thread name.",
)
@click.option(
    "--force", is_flag=True, help="Skip the estimate and always produce the meditation."
)
@click.option(
    "--no-play",
    is_flag=True,
    help="Render the audio file but do not play it in the call.",
)
@click.option(
    "--room", "room_name", default="", help="Explicit LiveKit room name for playback."
)
def run(
    thread_id: str,
    thread_name: str,
    agent_name: str,
    task_text: str,
    force: bool,
    no_play: bool,
    room_name: str,
) -> None:
    """Estimate the task and, when it is long, write, render, and play a meditation."""
    _configure_worker_logging()
    settings = task_meditation.load_task_meditation_settings()
    if not settings.enabled and not force:
        click.echo(json.dumps({"status": "skipped", "reason": "disabled"}))
        return
    lock_path = task_meditation.acquire_run_lock(settings.output_dir)
    if lock_path is None:
        click.echo(
            json.dumps({"status": "skipped", "reason": "another meditation is running"})
        )
        return

    synthesize = task_meditation.build_synthesizer(settings)

    estimate = (
        task_meditation.JevEstimator(
            api_key=settings.jev_api_key,
            model=settings.jev_model,
            threshold_seconds=settings.threshold_seconds,
        )
        if settings.jev_api_key
        else None
    )

    def publish(path: Path) -> None:
        task_meditation.publish_meditation_audio(
            path, room_name=room_name.strip() or None
        )

    async def read_conversation() -> str:
        return await task_meditation.recent_conversation_text(
            exclude_thread_id=thread_id.strip() or None
        )

    try:
        outcome = asyncio.run(
            task_meditation.run_task_meditation(
                thread_id=thread_id.strip(),
                thread_name=thread_name.strip(),
                agent_name=agent_name.strip() or None,
                settings=settings,
                complete=task_meditation.CodexOneShotCompleter(),
                estimate=estimate,
                synthesize=synthesize,
                publish=publish,
                read_conversation=read_conversation,
                task_text=task_text.strip() or None,
                force=force,
                play=not no_play,
            )
        )
    finally:
        task_meditation.release_run_lock(lock_path)
        task_meditation.close_synthesizer(synthesize)
    click.echo(json.dumps(outcome.payload(), sort_keys=True))
    if outcome.status == task_meditation.STATUS_FAILED:
        raise SystemExit(1)


@meditation.command("render")
@click.argument(
    "script_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
)
@click.option(
    "--play", is_flag=True, help="Also play the rendered file in the active call."
)
def render(script_path: Path, output_path: Path | None, play: bool) -> None:
    """Synthesize a saved meditation script (with <pause N seconds> markers) to WAV."""
    settings = task_meditation.load_task_meditation_settings()
    synthesize = task_meditation.build_synthesizer(settings)
    if synthesize is None:
        raise click.ClickException(
            "No voice engine is available: set "
            f"{task_meditation.ELEVENLABS_API_KEY_ENV} or switch "
            f"{task_meditation.TTS_ENGINE_ENV} to product."
        )
    segments = task_meditation.parse_meditation_script(
        script_path.read_text(encoding="utf-8"),
        max_pause_seconds=settings.max_pause_seconds,
    )
    if not any(isinstance(segment, task_meditation.Speech) for segment in segments):
        raise click.ClickException("The script has no spoken text.")
    rendered: list[bytes | task_meditation.Pause] = []
    try:
        for segment in segments:
            if isinstance(segment, task_meditation.Pause):
                rendered.append(segment)
            else:
                rendered.append(synthesize(segment.text))
    finally:
        task_meditation.close_synthesizer(synthesize)
    pcm = task_meditation.stitch_pcm(rendered)
    target = output_path or script_path.with_suffix(".wav")
    task_meditation.write_wav(target, pcm)
    click.echo(
        json.dumps(
            {
                "audio_path": str(target),
                "audio_seconds": round(task_meditation.pcm_duration_seconds(pcm), 1),
                "segments": len(segments),
                "voice": getattr(synthesize, "describe", lambda: None)(),
            },
            sort_keys=True,
        )
    )
    if play:
        task_meditation.publish_meditation_audio(target)
        click.echo("Playing in the active voice session.")

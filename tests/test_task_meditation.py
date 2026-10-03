from __future__ import annotations

import asyncio
import json
import os
import wave
from pathlib import Path

import pytest

from openbase_coder_cli import task_meditation as tm

# --- script parsing --------------------------------------------------------


def test_parse_meditation_script_splits_speech_and_pauses():
    script = (
        "Settle into your seat.\n<pause 5 seconds>\nNotice the breath.\n"
        "<pause 3s>\nLet the work be carried for a while."
    )
    segments = tm.parse_meditation_script(script)
    assert segments == [
        tm.Speech("Settle into your seat."),
        tm.Pause(5.0),
        tm.Speech("Notice the breath."),
        tm.Pause(3.0),
        tm.Speech("Let the work be carried for a while."),
    ]


@pytest.mark.parametrize(
    "marker",
    [
        "<pause 4 seconds>",
        "<pause 4s>",
        "<pause: 4>",
        "[pause 4 seconds]",
        "<PAUSE 4 sec>",
        "(pause 4)",
    ],
)
def test_parse_meditation_script_accepts_marker_variants(marker):
    segments = tm.parse_meditation_script(f"One.{marker}Two.")
    assert segments == [tm.Speech("One."), tm.Pause(4.0), tm.Speech("Two.")]


def test_parse_meditation_script_bare_pause_uses_default():
    segments = tm.parse_meditation_script(
        "One. <pause> Two.", default_pause_seconds=2.5
    )
    assert segments[1] == tm.Pause(2.5)


def test_parse_meditation_script_merges_clamps_and_trims_pauses():
    script = "<pause 5 seconds>Breathe in.<pause 15 seconds><pause 15 seconds>Breathe out.<pause 9 seconds>"
    segments = tm.parse_meditation_script(script, max_pause_seconds=20)
    assert segments == [
        tm.Speech("Breathe in."),
        tm.Pause(20.0),
        tm.Speech("Breathe out."),
    ]


def test_parse_meditation_script_strips_code_fences_and_whitespace():
    script = "```text\n  Welcome   back.  \n<pause 2 seconds>\n Rest. \n```"
    assert tm.parse_meditation_script(script) == [
        tm.Speech("Welcome back."),
        tm.Pause(2.0),
        tm.Speech("Rest."),
    ]


def test_script_helpers_report_speech_and_pause_totals():
    segments = [
        tm.Speech("a"),
        tm.Pause(2.0),
        tm.Speech("b"),
        tm.Pause(3.0),
        tm.Speech("c"),
    ]
    assert tm.script_speech_text(segments) == "a b c"
    assert tm.script_pause_seconds(segments) == 5.0


# --- estimate parsing -------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"estimated_seconds": 240, "rationale": "tests"}', 240.0),
        ('```json\n{"estimated_seconds": "45"}\n```', 45.0),
        ("Roughly 2 minutes of work.", 120.0),
        ("About 1.5 hours.", 5400.0),
        ("90s", 90.0),
        ("Estimate: 300", 300.0),
        ("no idea", None),
        ("", None),
    ],
)
def test_parse_estimate_seconds(reply, expected):
    assert tm.parse_estimate_seconds(reply) == expected


def test_should_meditate_requires_estimate_over_threshold():
    assert tm.should_meditate(91, 90) is True
    assert tm.should_meditate(90, 90) is False
    assert tm.should_meditate(None, 90) is False


def test_meditation_target_seconds_is_bounded():
    assert tm.meditation_target_seconds(None) == 150
    assert tm.meditation_target_seconds(60) == 75
    assert tm.meditation_target_seconds(200) == 140
    assert tm.meditation_target_seconds(3600) == 300


# --- audio ------------------------------------------------------------------


def test_stitch_pcm_inserts_pauses_and_gaps(tmp_path):
    rate = 100
    speech = b"\x01\x00" * 50  # 0.5 s
    pcm = tm.stitch_pcm(
        [speech, tm.Pause(2.0), speech, speech],
        sample_rate=rate,
        segment_gap_seconds=0.5,
    )
    # 0.5 + 2.0 + 0.5 + 0.5 (gap) + 0.5 = 4.0 s
    assert tm.pcm_duration_seconds(pcm, sample_rate=rate) == 4.0
    path = tm.write_wav(tmp_path / "out.wav", pcm, sample_rate=rate)
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == rate
        assert handle.getnframes() == 400


def test_silence_pcm_is_zeroed_and_sized():
    pcm = tm.silence_pcm(0.25, sample_rate=1000)
    assert pcm == b"\x00" * 500


# --- prompts ----------------------------------------------------------------


def test_meditation_prompt_covers_required_themes_and_context():
    prompt = tm.build_meditation_prompt(
        task="fix the login bug",
        agent_name="Dottie",
        conversation="<voice>User: please fix login</voice>\nAgent: on it",
        estimate_seconds=240,
    )
    lowered = prompt.lower()
    assert "attachment to the work" in lowered
    assert "gratitude that the work is being done" in lowered
    assert "connect with and influence" in lowered
    assert "Dottie" in prompt
    assert "fix the login bug" in prompt
    assert "<voice>" not in prompt and "please fix login" in prompt
    assert "about 4 minutes" in prompt
    assert f"about {tm.meditation_target_seconds(240)} seconds" in prompt


def test_estimate_prompt_requests_json_and_includes_conversation():
    prompt = tm.build_estimate_prompt(
        task="add tests", agent_name=None, conversation="User: add tests"
    )
    assert '"estimated_seconds"' in prompt
    assert "User: add tests" in prompt
    assert "Agent working on it" not in prompt


# --- settings ---------------------------------------------------------------


def test_settings_defaults(tmp_path):
    settings = tm.load_task_meditation_settings(
        env={}, config_path=tmp_path / "missing.json"
    )
    assert settings.enabled is True
    assert settings.threshold_seconds == 90.0
    assert settings.estimator_model == "gpt-5.5"
    assert settings.estimator_reasoning_effort == "low"
    assert settings.meditation_model == "gpt-6-sol"
    assert settings.meditation_reasoning_effort == "medium"
    assert settings.elevenlabs_api_key is None
    assert settings.payload()["elevenlabs_api_key"] == "missing"


def test_settings_config_then_env_precedence(tmp_path):
    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(
        json.dumps(
            {
                "task_meditation": {
                    "enabled": False,
                    "threshold_seconds": 120,
                    "meditation_model": "gpt-5",
                    "voice_id": "config-voice",
                    "output_dir": str(tmp_path / "meds"),
                }
            }
        )
    )
    settings = tm.load_task_meditation_settings(env={}, config_path=config_path)
    assert settings.enabled is False
    assert settings.threshold_seconds == 120.0
    assert settings.meditation_model == "gpt-5"
    assert settings.elevenlabs_voice_id == "config-voice"
    assert settings.output_dir == tmp_path / "meds"

    env = {
        tm.ENABLED_ENV: "true",
        tm.THRESHOLD_ENV: "45",
        tm.MEDITATION_MODEL_ENV: "sol",
        tm.MEDITATION_REASONING_EFFORT_ENV: "high",
        tm.ELEVENLABS_API_KEY_ENV: "secret",
        tm.ELEVENLABS_VOICE_ID_ENV: "env-voice",
    }
    settings = tm.load_task_meditation_settings(env=env, config_path=config_path)
    assert settings.enabled is True
    assert settings.threshold_seconds == 45.0
    assert settings.meditation_model == "sol"
    assert settings.meditation_reasoning_effort == "high"
    assert settings.elevenlabs_api_key == "secret"
    assert settings.elevenlabs_voice_id == "env-voice"
    assert settings.payload()["elevenlabs_api_key"] == "set"


def test_settings_ignore_invalid_values(tmp_path):
    env = {tm.THRESHOLD_ENV: "soon", tm.ENABLED_ENV: "maybe"}
    settings = tm.load_task_meditation_settings(
        env=env, config_path=tmp_path / "missing.json"
    )
    assert settings.threshold_seconds == 90.0
    assert settings.enabled is True


# --- conversation flattening ------------------------------------------------


def test_conversation_lines_from_claude_turn_views():
    payload = {
        "turns": [
            {
                "createdAt": "2026-10-03T10:01:00Z",
                "prompt": "<voice>second ask</voice>",
                "lastUsefulMessage": "done two",
            },
            {
                "createdAt": "2026-10-03T10:00:00Z",
                "prompt": "first ask",
                "finalMessage": "done one",
            },
        ]
    }
    assert tm.conversation_lines_from_thread(payload) == [
        "User: first ask",
        "Agent: done one",
        "User: second ask",
        "Agent: done two",
    ]


def test_conversation_lines_from_codex_items():
    payload = {
        "thread": {
            "turns": [
                {
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "ship it"}],
                        },
                        {"type": "agentMessage", "text": "first draft"},
                        {"type": "agentMessage", "text": "shipping now"},
                    ]
                }
            ]
        }
    }
    assert tm.conversation_lines_from_thread(payload) == [
        "User: ship it",
        "Agent: shipping now",
    ]


def test_conversation_lines_handles_missing_turns():
    assert tm.conversation_lines_from_thread({}) == []
    assert tm.conversation_lines_from_thread({"turns": "nope"}) == []


def test_trim_conversation_keeps_newest_lines():
    lines = [f"User: message {index} " + "x" * 50 for index in range(20)]
    trimmed = tm.trim_conversation(lines, max_chars=200)
    assert trimmed.splitlines()[-1] == lines[-1]
    assert len(trimmed) <= 200
    assert lines[0] not in trimmed


def test_recent_conversation_text_prefers_active_target(monkeypatch):
    from types import SimpleNamespace

    from openbase_coder_cli import livekit_voice_route

    monkeypatch.setattr(
        livekit_voice_route,
        "get_livekit_voice_route_state",
        lambda: SimpleNamespace(
            active_target_thread_id="agent-1", dispatcher_thread_id="dispatch-1"
        ),
    )
    reads: list[str] = []

    class FakeMulti:
        async def read_by_label(self, query, include_turns=False):
            reads.append(query.thread_id)
            if query.thread_id == "agent-1":
                return {"turns": []}
            return {"turns": [{"prompt": "hello dispatcher", "finalMessage": "hi"}]}

        async def aclose(self):
            reads.append("closed")

    import super_agents.multi_backend as multi_backend

    monkeypatch.setattr(multi_backend, "MultiBackendClient", lambda: FakeMulti())

    text = asyncio.run(tm.recent_conversation_text(exclude_thread_id="new-task"))
    assert text == "User: hello dispatcher\nAgent: hi"
    assert reads == ["agent-1", "dispatch-1", "closed"]


# --- orchestration ----------------------------------------------------------


def _settings(tmp_path: Path, **overrides) -> tm.TaskMeditationSettings:
    values = {
        "enabled": True,
        "threshold_seconds": 90.0,
        "output_dir": tmp_path / "meditations",
        "elevenlabs_api_key": "secret",
    }
    values.update(overrides)
    return tm.TaskMeditationSettings(**values)


class FakeCompleter:
    def __init__(self, estimate_reply: str, script_reply: str) -> None:
        self.estimate_reply = estimate_reply
        self.script_reply = script_reply
        self.calls: list[dict] = []

    async def __call__(
        self, prompt, *, model, reasoning_effort, developer_instructions
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "developer_instructions": developer_instructions,
            }
        )
        if developer_instructions == tm.ESTIMATOR_INSTRUCTIONS:
            return self.estimate_reply
        return self.script_reply


def _run(
    settings,
    completer,
    *,
    synthesize="default",
    publish=None,
    force=False,
    play=True,
    conversation="User: hi",
):
    synthesized: list[str] = []
    published: list[Path] = []

    def fake_synthesize(text: str) -> bytes:
        synthesized.append(text)
        return b"\x01\x00" * tm.SAMPLE_RATE  # one second per segment

    def fake_publish(path: Path) -> None:
        published.append(path)

    async def read_conversation() -> str:
        return conversation

    outcome = asyncio.run(
        tm.run_task_meditation(
            thread_id="thread-1",
            thread_name="Fix the login bug",
            agent_name="Dottie",
            settings=settings,
            complete=completer,
            synthesize=fake_synthesize if synthesize == "default" else synthesize,
            publish=publish or fake_publish,
            read_conversation=read_conversation,
            force=force,
            play=play,
        )
    )
    return outcome, synthesized, published


def test_run_skips_short_tasks_without_writing_a_script(tmp_path):
    completer = FakeCompleter('{"estimated_seconds": 30}', "unused")
    outcome, synthesized, published = _run(_settings(tmp_path), completer)
    assert outcome.status == "skipped"
    assert outcome.reason == "under threshold"
    assert outcome.estimate_seconds == 30.0
    assert len(completer.calls) == 1
    assert completer.calls[0]["model"] == "gpt-5.5"
    assert completer.calls[0]["reasoning_effort"] == "low"
    assert "User: hi" in completer.calls[0]["prompt"]
    assert synthesized == [] and published == []
    assert not (tmp_path / "meditations").exists()


def test_run_plays_a_meditation_for_long_tasks(tmp_path):
    script = "Settle in.\n<pause 3 seconds>\nLet go of the outcome.\n<pause 2 seconds>\nThank you."
    completer = FakeCompleter('{"estimated_seconds": 300}', script)
    outcome, synthesized, published = _run(_settings(tmp_path), completer)
    assert outcome.status == "played"
    assert outcome.estimate_seconds == 300.0
    assert synthesized == ["Settle in.", "Let go of the outcome.", "Thank you."]
    assert completer.calls[1]["model"] == "gpt-6-sol"
    assert completer.calls[1]["reasoning_effort"] == "medium"
    assert completer.calls[1]["developer_instructions"] == tm.MEDITATION_INSTRUCTIONS
    assert "attachment to the work" in completer.calls[1]["prompt"].lower()
    assert [str(path) for path in published] == [outcome.audio_path]
    assert outcome.audio_seconds == 8.0  # 3 s speech + 5 s pauses
    assert Path(outcome.script_path).read_text().startswith("Settle in.")
    record = json.loads(Path(outcome.record_path).read_text())
    assert record["status"] == "played"
    with wave.open(outcome.audio_path, "rb") as handle:
        assert handle.getframerate() == tm.SAMPLE_RATE


def test_run_force_skips_estimate_and_no_play_only_renders(tmp_path):
    completer = FakeCompleter("unused", "Breathe.<pause 2 seconds>Release.")
    outcome, synthesized, published = _run(
        _settings(tmp_path), completer, force=True, play=False
    )
    assert outcome.status == "rendered"
    assert outcome.reason == "playback disabled"
    assert outcome.estimate_seconds is None
    assert len(completer.calls) == 1
    assert completer.calls[0]["model"] == "gpt-6-sol"
    assert synthesized == ["Breathe.", "Release."]
    assert published == []
    assert Path(outcome.audio_path).is_file()


def test_run_disabled_without_force(tmp_path):
    completer = FakeCompleter('{"estimated_seconds": 300}', "Breathe.")
    outcome, _, _ = _run(_settings(tmp_path, enabled=False), completer)
    assert outcome.status == "skipped" and outcome.reason == "disabled"
    assert completer.calls == []


def test_run_without_api_key_saves_script_only(tmp_path):
    completer = FakeCompleter(
        '{"estimated_seconds": 300}', "Breathe.<pause 2 seconds>Rest."
    )
    outcome, _, published = _run(
        _settings(tmp_path, elevenlabs_api_key=None), completer, synthesize=None
    )
    assert outcome.status == "skipped"
    assert "ElevenLabs" in outcome.reason
    assert Path(outcome.script_path).is_file()
    assert outcome.audio_path is None
    assert published == []


def test_run_reports_unparsable_estimate(tmp_path):
    completer = FakeCompleter("I cannot say.", "unused")
    outcome, _, _ = _run(_settings(tmp_path), completer)
    assert outcome.status == "skipped" and outcome.reason == "estimate unparsable"


def test_run_reports_failures_without_raising(tmp_path):
    class Boom:
        async def __call__(self, *args, **kwargs):
            raise RuntimeError("app-server down")

    outcome, _, _ = _run(_settings(tmp_path), Boom())
    assert outcome.status == "failed"
    assert "app-server down" in outcome.reason

    def broken_synthesize(text: str) -> bytes:
        raise RuntimeError("quota")

    completer = FakeCompleter('{"estimated_seconds": 300}', "Breathe.")
    outcome, _, _ = _run(_settings(tmp_path), completer, synthesize=broken_synthesize)
    assert outcome.status == "failed" and "quota" in outcome.reason

    def broken_publish(path: Path) -> None:
        raise RuntimeError("no room")

    outcome, _, _ = _run(_settings(tmp_path), completer, publish=broken_publish)
    assert outcome.status == "failed" and "no room" in outcome.reason
    assert Path(outcome.audio_path).is_file()


def test_run_rejects_script_without_speech(tmp_path):
    completer = FakeCompleter(
        '{"estimated_seconds": 300}', "<pause 5 seconds><pause 5 seconds>"
    )
    outcome, synthesized, _ = _run(_settings(tmp_path), completer)
    assert outcome.status == "failed"
    assert "no spoken text" in outcome.reason
    assert synthesized == []


def test_run_survives_conversation_reader_failure(tmp_path):
    completer = FakeCompleter('{"estimated_seconds": 10}', "unused")

    async def broken_reader() -> str:
        raise RuntimeError("no route")

    outcome = asyncio.run(
        tm.run_task_meditation(
            thread_id="t",
            thread_name="task",
            agent_name=None,
            settings=_settings(tmp_path),
            complete=completer,
            synthesize=None,
            publish=None,
            read_conversation=broken_reader,
        )
    )
    assert outcome.status == "skipped" and outcome.reason == "under threshold"
    assert "Recent voice conversation" not in completer.calls[0]["prompt"]


# --- run lock ---------------------------------------------------------------


def test_run_lock_blocks_live_runs_and_takes_over_stale_ones(tmp_path, monkeypatch):
    lock = tm.acquire_run_lock(tmp_path)
    assert lock is not None and lock.is_file()
    # Our own pid holds it: a second acquire in-process succeeds (re-entrant).
    assert tm.acquire_run_lock(tmp_path) is not None
    # A live foreign pid blocks.
    lock.write_text(json.dumps({"pid": os.getpid() + 1, "started_at": 1e12}))
    monkeypatch.setattr(tm, "_pid_alive", lambda pid: True)
    assert tm.acquire_run_lock(tmp_path) is None
    # A dead pid or stale lock is taken over.
    monkeypatch.setattr(tm, "_pid_alive", lambda pid: False)
    assert tm.acquire_run_lock(tmp_path) is not None
    tm.release_run_lock(lock)
    assert not lock.exists()
    tm.release_run_lock(None)


# --- detached worker --------------------------------------------------------


def test_worker_argv_and_spawn(tmp_path, monkeypatch):
    argv = tm.meditation_worker_argv(
        thread_id="t-1",
        thread_name="Fix login",
        agent_name="Dottie",
        command="/x/openbase-coder",
    )
    assert argv == [
        "/x/openbase-coder",
        "meditation",
        "run",
        "--thread-id",
        "t-1",
        "--thread-name",
        "Fix login",
        "--agent-name",
        "Dottie",
    ]
    assert "--agent-name" not in tm.meditation_worker_argv(
        thread_id="t", thread_name="n", agent_name=None, command="c"
    )

    recorded = {}

    class FakeProcess:
        pid = 4242

    def fake_popen(args, **kwargs):
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(tm.subprocess, "Popen", fake_popen)
    log_path = tmp_path / "logs" / "task-meditation.log"
    pid = tm.spawn_meditation_worker(argv, log_path=log_path, env={"A": "1"})
    assert pid == 4242
    assert recorded["args"] == argv
    assert recorded["kwargs"]["start_new_session"] is True
    assert recorded["kwargs"]["env"] == {"A": "1"}
    assert log_path.parent.is_dir()


def test_openbase_coder_command_prefers_sibling_of_interpreter(tmp_path, monkeypatch):
    fake_python = tmp_path / "bin" / "python"
    fake_python.parent.mkdir()
    fake_python.write_text("")
    sibling = tmp_path / "bin" / "openbase-coder"
    sibling.write_text("")
    monkeypatch.setattr(tm.sys, "executable", str(fake_python))
    assert tm.openbase_coder_command() == str(sibling)
    sibling.unlink()
    monkeypatch.setattr(tm.shutil, "which", lambda name: None)
    assert tm.openbase_coder_command() == "openbase-coder"


# --- providers --------------------------------------------------------------


def test_elevenlabs_synthesizer_posts_pcm_request(monkeypatch):
    import httpx

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return httpx.Response(200, content=b"\x00\x01" * 500)

    monkeypatch.setattr(httpx, "post", fake_post)
    synth = tm.ElevenLabsSynthesizer(api_key="k", voice_id="v", model_id="m")
    assert synth("hello") == b"\x00\x01" * 500
    assert captured["url"] == "https://api.elevenlabs.io/v1/text-to-speech/v"
    assert captured["kwargs"]["params"] == {"output_format": "pcm_24000"}
    assert captured["kwargs"]["headers"]["xi-api-key"] == "k"
    assert captured["kwargs"]["json"]["text"] == "hello"
    assert captured["kwargs"]["json"]["model_id"] == "m"


def test_elevenlabs_synthesizer_raises_on_http_error_and_empty_audio(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda url, **kwargs: httpx.Response(401, text="bad key")
    )
    synth = tm.ElevenLabsSynthesizer(api_key="k")
    with pytest.raises(RuntimeError, match="HTTP 401"):
        synth("hello")
    monkeypatch.setattr(
        httpx, "post", lambda url, **kwargs: httpx.Response(200, content=b"")
    )
    with pytest.raises(RuntimeError, match="no audio"):
        synth("hello")


def test_publish_meditation_audio_posts_to_play_api(monkeypatch, tmp_path):
    import httpx

    from openbase_coder_cli.cli import local_server

    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs["json"]))
        return httpx.Response(202, json={"message_id": "a", "room_name": "r"})

    class FakeTokenManager:
        def get_access_token(self) -> str:
            return "jwt"

    monkeypatch.setattr(local_server, "get_token_manager", lambda: FakeTokenManager())
    monkeypatch.setattr(local_server.httpx, "request", fake_request)
    audio = tmp_path / "m.wav"
    tm.publish_meditation_audio(audio, room_name="r")
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/api/user/play/")
    assert calls[0][2] == {"audio_path": str(audio), "room_name": "r"}

    monkeypatch.setattr(
        local_server.httpx,
        "request",
        lambda method, url, **kwargs: httpx.Response(
            502, json={"status": "no_active_room", "detail": "No call."}
        ),
    )
    with pytest.raises(RuntimeError, match="No call."):
        tm.publish_meditation_audio(audio)


def test_codex_one_shot_completer_runs_a_read_only_turn(monkeypatch):
    from openbase_coder_cli.livekit_agent import codex_app_client

    created = {}

    class FakeClient:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.closed = False

        async def run_turn(self, prompt):
            created["prompt"] = prompt
            return {"_livekit_speech_text": "reply text"}

        async def aclose(self):
            self.closed = True

    class FakeBase:
        pass

    # The completer subclasses CodexAppServerClient; swap in a fake base whose
    # subclass keeps the override methods but never opens a socket.
    monkeypatch.setattr(codex_app_client, "CodexAppServerClient", FakeClient)
    completer = tm.CodexOneShotCompleter(endpoint="ws://example.invalid", cwd="/tmp")
    reply = asyncio.run(
        completer(
            "hello",
            model="sol",
            reasoning_effort="medium",
            developer_instructions="be brief",
        )
    )
    assert reply == "reply text"
    assert created["model_name"] == "sol"
    assert created["sandbox"] == "read-only"
    assert created["approval_policy"] == "never"
    assert created["persist_thread"] is False
    assert created["developer_instructions"] == "be brief"
    assert created["prompt"] == "hello"


def test_one_shot_client_overrides_use_fixed_effort_and_raw_message(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from openbase_coder_cli.livekit_agent import codex_app_client

    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "codex")
    holder = {}
    original = codex_app_client.CodexAppServerClient

    class Capture(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            holder["client"] = self

        async def run_turn(self, prompt):
            return {
                "_livekit_speech_text": self._speech_text_for_turn(
                    SimpleNamespace(
                        agent_messages=["# Heading\n\nfull **markdown** text"]
                    )
                )
                + "|"
                + str(self._configured_reasoning_effort())
            }

        async def aclose(self):
            return None

    monkeypatch.setattr(codex_app_client, "CodexAppServerClient", Capture)
    completer = tm.CodexOneShotCompleter(
        endpoint="ws://example.invalid", cwd=str(tmp_path)
    )
    reply = asyncio.run(
        completer(
            "p", model="sol", reasoning_effort="medium", developer_instructions="d"
        )
    )
    assert reply == "# Heading\n\nfull **markdown** text|medium"
    assert holder["client"]._thread_params()["sandbox"] == "read-only"


def test_codex_one_shot_completer_raises_on_failed_turn(monkeypatch):
    from openbase_coder_cli.livekit_agent import codex_app_client

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def run_turn(self, prompt):
            return {
                "status": "failed",
                "error": {
                    "message": "The 'sol' model is not supported when using Codex with a ChatGPT account."
                },
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(codex_app_client, "CodexAppServerClient", FakeClient)
    completer = tm.CodexOneShotCompleter(endpoint="ws://example.invalid", cwd="/tmp")
    with pytest.raises(RuntimeError, match="not supported"):
        asyncio.run(
            completer(
                "p", model="sol", reasoning_effort="low", developer_instructions="d"
            )
        )

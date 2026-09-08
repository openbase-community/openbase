import asyncio
import json
import time
from pathlib import Path

from openbase_coder_cli.livekit_agent import codex_app_client as client_module
from openbase_coder_cli.livekit_agent import codex_transport as transport_module
from openbase_coder_cli.livekit_agent.codex_app_client import (
    LIVEKIT_DUPLICATE_TURN_GRACE_SECONDS,
    CodexAppServerClient,
    _active_turn_id_mismatch,
    _ActiveTurn,
    _is_no_active_turn_error,
    _prompt_debug_fields,
    _speech_excerpt,
    _undelivered_suffix,
)
from openbase_coder_cli.livekit_voice_route import (
    get_livekit_voice_route_state,
    prepare_livekit_dispatcher_recreation,
)


def test_returns_full_text_when_nothing_was_delivered():
    assert _undelivered_suffix("", "hello") == "hello"


def test_model_name_for_role_ignores_env_model_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "claude-code")
    monkeypatch.setenv("CODEX_MODEL", "gpt-5.5")
    config_path = tmp_path / "missing-dispatcher-config.json"

    assert client_module._model_name_for_role(config_path) == "gpt-5.5"


def test_model_name_for_role_uses_backend_and_role(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(
        json.dumps(
            {
                "backend_models": {
                    "claude_code": {
                        "dispatcher": "haiku",
                        "super_agents": "opus",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "claude_code")

    assert client_module._model_name_for_role(config_path) == "haiku"
    assert (
        client_module._model_name_for_role(
            config_path,
            use_super_agent_model=True,
        )
        == "opus"
    )


def test_returns_only_new_suffix_when_final_text_extends_streamed_text():
    assert _undelivered_suffix("hello", "hello world") == " world"


def test_returns_full_text_when_streamed_text_does_not_match_prefix():
    assert _undelivered_suffix("hello", "goodbye") == "goodbye"


def test_speech_excerpt_uses_all_safe_prose_paragraphs():
    text = """I checked the launchd services and found the issue.

```python
print("do not read code")
```

The agent is now using local VAD interruption, and console mode is ready for another test."""

    assert (
        _speech_excerpt(text)
        == "I checked the launchd services and found the issue. Omitted. The agent is now using local V A D interruption, and console mode is ready for another test."
    )


def test_speech_excerpt_skips_tool_chatter():
    text = """setsummary failed because the command was unavailable.

The current working directory is /Users/example."""

    assert (
        _speech_excerpt(text)
        == "The current working directory is example in users. Technical output omitted, shown on screen."
    )


def test_speech_excerpt_does_not_read_code_only_responses():
    text = """```swift
struct ContentView: View {
    var body: some View { Text("Hello") }
}
```"""

    assert _speech_excerpt(text) == "Omitted."


def test_speech_excerpt_truncates_from_beginning_by_sentence():
    text = "First sentence. Second sentence. Third sentence."

    assert _speech_excerpt(text, max_chars=33) == "First sentence. Second sentence."


def test_should_join_brand_new_silent_turn():
    async def check():
        client = CodexAppServerClient(ws_url="ws://example.invalid", cwd="/tmp")
        turn = _ActiveTurn(
            turn_id="turn-1",
            completed=asyncio.get_running_loop().create_future(),
            prompt="do the thing",
            started_at=time.monotonic(),
        )

        assert client._should_join_existing_turn(turn, "do the thing")

    asyncio.run(check())


def test_should_join_punctuation_only_transcript_duplicate():
    async def check():
        client = CodexAppServerClient(ws_url="ws://example.invalid", cwd="/tmp")
        turn = _ActiveTurn(
            turn_id="turn-1",
            completed=asyncio.get_running_loop().create_future(),
            prompt="hello, are you there?",
            started_at=time.monotonic(),
        )

        assert client._should_join_existing_turn(turn, "hello are you there")

    asyncio.run(check())


def test_duplicate_prompt_joins_cancelled_pending_turn_start():
    class SlowStartClient(CodexAppServerClient):
        def __init__(self):
            super().__init__(ws_url="ws://example.invalid", cwd="/tmp")
            self.started_event = asyncio.Event()
            self.start_requests = 0

        async def _ensure_thread(self):
            return "thread-1"

        async def _send_request(self, method, params):
            assert method == "turn/start"
            assert params["serviceTier"] == "standard"
            self.start_requests += 1
            self.started_event.set()
            await asyncio.sleep(0.01)
            return {"turn": {"id": "turn-1"}}

    async def check():
        client = SlowStartClient()
        first = asyncio.create_task(client.run_turn("Say, yes, I'm here."))
        await client.started_event.wait()
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass

        second = asyncio.create_task(client.run_turn("Say yes I'm here"))
        while client._active_turn is None:
            await asyncio.sleep(0.001)

        assert client.start_requests == 1
        client._record_agent_message(client._active_turn.turn_id, "Yes, I'm here.")
        await client._handle_notification(
            "turn/completed",
            {"turn": {"id": client._active_turn.turn_id, "status": "completed"}},
        )
        result = await second

        assert result["_livekit_speech_text"] == "Yes, I'm here."

    asyncio.run(check())


def test_run_turn_emits_dispatch_timing_logs(tmp_path: Path, caplog):
    class FastClient(CodexAppServerClient):
        async def _ensure_thread(self):
            return "thread-1"

        async def _send_request(self, method, params):
            assert method == "turn/start"
            assert params["serviceTier"] == "standard"
            assert params["model"] == "gpt-5.5"
            return {"turn": {"id": "turn-1"}}

    async def check():
        client = FastClient(
            ws_url="ws://example.invalid",
            cwd="/tmp/project",
            dispatcher_config_path=tmp_path / "missing-dispatcher-config.json",
        )
        task = asyncio.create_task(client.run_turn("wait briefly"))
        while client._active_turn is None:
            await asyncio.sleep(0.001)
        client._record_agent_message(client._active_turn.turn_id, "Done.")
        await client._handle_notification(
            "turn/completed",
            {"turn": {"id": client._active_turn.turn_id, "status": "completed"}},
        )
        return await task

    caplog.set_level("INFO", logger="openbase_coder_cli.livekit_agent.codex_app_client")
    result = asyncio.run(check())

    assert result["_livekit_speech_text"] == "Done."
    messages = [record.getMessage() for record in caplog.records]
    assert any("stage=voice_request_received" in message for message in messages)
    assert any("stage=turn_start_response" in message for message in messages)
    assert any("stage=first_agent_message" in message for message in messages)
    assert any("stage=turn_completed" in message for message in messages)
    turn_start_logs = [
        message for message in messages if "stage=turn_start_request" in message
    ]
    assert any(
        "reasoning_effort=app-server-default" in message for message in turn_start_logs
    )
    assert any("model=gpt-5.5" in message for message in turn_start_logs)
    assert any("service_tier=standard" in message for message in turn_start_logs)


def test_dispatcher_reasoning_config_applies_to_new_turns_without_restart(
    tmp_path: Path, caplog
):
    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(
        json.dumps({"dispatcher_reasoning_effort": "low"}), encoding="utf-8"
    )

    class ConfigClient(CodexAppServerClient):
        def __init__(self):
            super().__init__(
                ws_url="ws://example.invalid",
                cwd="/tmp/project",
                dispatcher_config_path=config_path,
            )
            self.requests = []
            self.turn_index = 0

        async def _ensure_thread(self):
            return "thread-1"

        async def _send_request(self, method, params):
            assert method == "turn/start"
            self.requests.append(params)
            self.turn_index += 1
            return {"turn": {"id": f"turn-{self.turn_index}"}}

    async def check():
        client = ConfigClient()

        first = asyncio.create_task(client.run_turn("first"))
        while client._active_turn is None:
            await asyncio.sleep(0.001)
        await client._handle_notification(
            "turn/completed",
            {"turn": {"id": client._active_turn.turn_id, "status": "completed"}},
        )
        await first

        config_path.write_text(
            json.dumps({"dispatcher_reasoning_effort": "high"}), encoding="utf-8"
        )
        second = asyncio.create_task(client.run_turn("second"))
        while client._active_turn is None:
            await asyncio.sleep(0.001)
        await client._handle_notification(
            "turn/completed",
            {"turn": {"id": client._active_turn.turn_id, "status": "completed"}},
        )
        await second

        return client.requests

    caplog.set_level("INFO", logger="openbase_coder_cli.livekit_agent.codex_app_client")
    requests = asyncio.run(check())

    assert requests[0]["effort"] == "low"
    assert requests[1]["effort"] == "high"
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "stage=turn_start_request" in message and "reasoning_effort=low" in message
        for message in messages
    )
    assert any(
        "stage=turn_start_request" in message and "reasoning_effort=high" in message
        for message in messages
    )


def test_target_thread_uses_super_agents_reasoning_instead_of_dispatcher(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "codex")
    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(
        json.dumps(
            {
                "dispatcher_reasoning_effort": "low",
                "super_agents_reasoning_effort": "high",
                "backend_models": {
                    "codex": {
                        "dispatcher": "gpt-dispatcher",
                        "super_agents": "gpt-super-agent",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class TargetClient(CodexAppServerClient):
        def __init__(self):
            super().__init__(
                ws_url="ws://example.invalid",
                cwd="/tmp/project",
                dispatcher_config_path=config_path,
                developer_instructions="target instructions",
                persist_thread=False,
                initial_thread_id="thread-1",
                use_super_agent_reasoning=True,
            )
            self.requests = []

        async def _ensure_thread(self):
            return "thread-1"

        async def _send_request(self, method, params):
            assert method == "turn/start"
            self.requests.append(params)
            return {"turn": {"id": "turn-1"}}

    async def check():
        client = TargetClient()
        task = asyncio.create_task(client.run_turn("work"))
        while client._active_turn is None:
            await asyncio.sleep(0.001)
        await client._handle_notification(
            "turn/completed",
            {"turn": {"id": client._active_turn.turn_id, "status": "completed"}},
        )
        await task
        return client.requests[0]

    request = asyncio.run(check())

    assert request["effort"] == "high"
    assert request["model"] == "gpt-super-agent"
    assert request["collaborationMode"]["settings"]["model"] == "gpt-super-agent"


def test_livekit_turn_instructions_are_sent_per_turn_only(tmp_path: Path):
    config_path = tmp_path / "dispatcher-config.json"

    class RecordingClient(CodexAppServerClient):
        def __init__(self):
            super().__init__(
                ws_url="ws://example.invalid",
                cwd="/tmp/project",
                developer_instructions="dispatcher instructions",
                dispatcher_config_path=config_path,
            )
            self.requests = []

        async def _ensure_thread(self):
            return "thread-1"

        async def _send_request(self, method, params):
            assert method == "turn/start"
            self.requests.append(params)
            return {"turn": {"id": "turn-1"}}

    async def check():
        client = RecordingClient()
        task = asyncio.create_task(
            client.run_turn(
                "what is the random animal?",
                developer_instructions="voice instructions",
            )
        )
        while client._active_turn is None:
            await asyncio.sleep(0.001)
        await client._handle_notification(
            "turn/completed",
            {"turn": {"id": client._active_turn.turn_id, "status": "completed"}},
        )
        await task
        return client.requests[0]

    params = asyncio.run(check())

    assert "developerInstructions" not in params
    assert params["model"] == "gpt-5.5"
    assert params["collaborationMode"]["settings"]["model"] == "gpt-5.5"
    assert params["collaborationMode"]["settings"]["developer_instructions"] == (
        "dispatcher instructions\n\nvoice instructions"
    )


def test_livekit_super_agent_thread_and_agent_names_are_appended_to_instructions():
    client = CodexAppServerClient(
        ws_url="ws://example.invalid",
        cwd="/tmp/project",
        developer_instructions="direct LiveKit instructions",
        persist_thread=False,
        initial_thread_id="target-1",
        super_agent_name="Build feature",
        super_agent_agent_name="Dottie",
    )

    assert client._thread_params(thread_id="target-1")["developerInstructions"] == (
        "direct LiveKit instructions\n\nSuper Agent thread name: Build feature\nYour name is Dottie."
    )
    assert client._turn_developer_instructions("voice instructions") == (
        "direct LiveKit instructions\n\nvoice instructions\n\nSuper Agent thread name: Build feature\nYour name is Dottie."
    )


def test_livekit_super_agent_agent_name_is_omitted_when_missing():
    client = CodexAppServerClient(
        ws_url="ws://example.invalid",
        cwd="/tmp/project",
        developer_instructions="direct LiveKit instructions",
        persist_thread=False,
        initial_thread_id="target-1",
        super_agent_name="Build feature",
    )

    assert client._thread_params(thread_id="target-1")["developerInstructions"] == (
        "direct LiveKit instructions\n\nSuper Agent thread name: Build feature"
    )


def test_initialize_advertises_experimental_api_capability(monkeypatch):
    class RecordingClient(CodexAppServerClient):
        def __init__(self):
            super().__init__(ws_url="ws://example.invalid", cwd="/tmp")
            self.requests = []
            self.notifications = []

        async def _send_request_locked(self, method, params):
            self.requests.append((method, params))
            return {}

        async def _send_notification_locked(self, method, params):
            self.notifications.append((method, params))

    async def check():
        client = RecordingClient()

        class FakeWebSocket:
            async def __aiter__(self):
                if False:
                    yield ""

            async def close(self):
                return None

        async def fake_connect(*args, **kwargs):
            return FakeWebSocket()

        monkeypatch.setattr(
            transport_module, "open_app_server_connection", fake_connect
        )
        await client._ensure_connected_locked()
        await client.aclose()
        return client.requests, client.notifications

    requests, notifications = asyncio.run(check())

    assert requests == [
        (
            "initialize",
            {
                "clientInfo": {
                    "name": "openbase-livekit",
                    "title": "Openbase LiveKit",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
    ]
    assert notifications == [("initialized", {})]


def test_livekit_client_uses_unix_endpoint_connector(monkeypatch, tmp_path: Path):
    calls = []

    class RecordingClient(CodexAppServerClient):
        async def _send_request_locked(self, method, params):
            return {}

        async def _send_notification_locked(self, method, params):
            return None

    class FakeWebSocket:
        async def __aiter__(self):
            if False:
                yield ""

        async def close(self):
            return None

    async def fake_connect(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return FakeWebSocket()

    monkeypatch.setattr(transport_module, "open_app_server_connection", fake_connect)

    async def check():
        client = RecordingClient(
            ws_url=f"unix://{tmp_path / 'control.sock'}",
            cwd="/tmp",
        )
        await client._ensure_connected_locked()
        await client.aclose()

    asyncio.run(check())

    endpoint, kwargs = calls[0]
    assert endpoint.transport == "unix"
    assert endpoint.socket_path == tmp_path / "control.sock"
    assert kwargs == {
        "max_size": None,
        "ping_interval": 20,
        "ping_timeout": 20,
        "open_timeout": 5,
    }


def test_dispatcher_reasoning_config_ignores_legacy_shared_key(tmp_path: Path):
    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(json.dumps({"reasoning_effort": "low"}), encoding="utf-8")
    client = CodexAppServerClient(
        ws_url="ws://example.invalid",
        cwd="/tmp/project",
        dispatcher_config_path=config_path,
    )

    assert client._dispatcher_reasoning_effort() is None


def test_should_not_join_old_or_started_turn():
    async def check():
        client = CodexAppServerClient(ws_url="ws://example.invalid", cwd="/tmp")
        old_turn = _ActiveTurn(
            turn_id="turn-1",
            completed=asyncio.get_running_loop().create_future(),
            prompt="do the thing",
            started_at=time.monotonic() - LIVEKIT_DUPLICATE_TURN_GRACE_SECONDS - 0.1,
        )
        started_turn = _ActiveTurn(
            turn_id="turn-2",
            completed=asyncio.get_running_loop().create_future(),
            prompt="do the thing",
            started_at=time.monotonic(),
            delivered_text="Already started.",
        )

        assert not client._should_join_existing_turn(old_turn, "do the thing")
        assert not client._should_join_existing_turn(started_turn, "do the thing")

    asyncio.run(check())


def test_should_not_join_different_prompt_during_grace_period():
    async def check():
        client = CodexAppServerClient(ws_url="ws://example.invalid", cwd="/tmp")
        turn = _ActiveTurn(
            turn_id="turn-1",
            completed=asyncio.get_running_loop().create_future(),
            prompt="do the thing",
            started_at=time.monotonic(),
        )

        assert not client._should_join_existing_turn(turn, "also narrow the scope")

    asyncio.run(check())


def test_no_active_turn_error_matches_steer_and_interrupt_messages():
    assert _is_no_active_turn_error(RuntimeError("no active turn to interrupt"))
    assert _is_no_active_turn_error(RuntimeError("No active turn to steer"))
    assert not _is_no_active_turn_error(RuntimeError("unrelated failure"))


def test_active_turn_id_mismatch_extracts_server_turn_id():
    exc = RuntimeError(
        '{"message":"expected active turn id `turn-old` but found `turn-server`"}'
    )

    assert _active_turn_id_mismatch(exc) == "turn-server"
    assert _active_turn_id_mismatch(RuntimeError("unrelated failure")) is None


def test_prompt_debug_fields_are_normalized_and_bounded():
    first = _prompt_debug_fields("  Hello   World  ")
    second = _prompt_debug_fields("hello world")

    assert first["hash"] == second["hash"]
    assert first["length"] == len("  Hello   World  ")
    assert first["excerpt"] == "hello world"
    assert len(_prompt_debug_fields("x" * 120)["excerpt"]) == 90


def test_prompt_debug_fields_ignore_onboarding_reminder():
    from openbase_coder_cli.onboarding_reminder import ONBOARDING_REMINDER

    bare = _prompt_debug_fields("hey are you there")
    with_note = _prompt_debug_fields(f"hey are you there\n\n{ONBOARDING_REMINDER}")

    assert bare["hash"] == with_note["hash"]
    assert with_note["excerpt"] == "hey are you there"


def test_steer_turn_uses_expected_turn_id():
    class RecordingClient(CodexAppServerClient):
        def __init__(self):
            super().__init__(ws_url="ws://example.invalid", cwd="/tmp")
            self.requests = []

        async def _send_request(self, method, params):
            self.requests.append((method, params))
            return {"turnId": params["expectedTurnId"]}

    async def check():
        client = RecordingClient()
        turn = _ActiveTurn(
            turn_id="turn-1",
            completed=asyncio.get_running_loop().create_future(),
            prompt="do the thing",
            started_at=time.monotonic(),
        )

        assert await client._steer_turn("thread-1", turn, "narrow the scope")
        assert client.requests == [
            (
                "turn/steer",
                {
                    "threadId": "thread-1",
                    "expectedTurnId": "turn-1",
                    "input": [{"type": "text", "text": "narrow the scope"}],
                },
            )
        ]

    asyncio.run(check())


def test_steer_turn_clears_inactive_turn():
    class InactiveTurnClient(CodexAppServerClient):
        async def _send_request(self, method, params):
            raise RuntimeError("No active turn to steer")

    async def check():
        client = InactiveTurnClient(ws_url="ws://example.invalid", cwd="/tmp")
        turn = _ActiveTurn(
            turn_id="turn-1",
            completed=asyncio.get_running_loop().create_future(),
            prompt="do the thing",
            started_at=time.monotonic(),
        )
        client._active_turn = turn

        assert not await client._steer_turn("thread-1", turn, "narrow the scope")
        assert client._active_turn is None
        assert turn.completed.done()

    asyncio.run(check())


def test_steer_turn_resyncs_active_turn_id_mismatch():
    class MismatchTurnClient(CodexAppServerClient):
        def __init__(self):
            super().__init__(ws_url="ws://example.invalid", cwd="/tmp")
            self.requests = []

        async def _send_request(self, method, params):
            self.requests.append((method, params))
            if len(self.requests) == 1:
                raise RuntimeError(
                    '{"message":"expected active turn id `turn-local` but found `turn-server`"}'
                )
            return {"ok": True}

    async def check():
        client = MismatchTurnClient()
        turn = _ActiveTurn(
            turn_id="turn-local",
            completed=asyncio.get_running_loop().create_future(),
            prompt="do the thing",
            started_at=time.monotonic(),
        )
        client._active_turn = turn

        assert await client._steer_turn("thread-1", turn, "narrow the scope")
        assert turn.turn_id == "turn-server"
        assert client.requests[1] == (
            "turn/steer",
            {
                "threadId": "thread-1",
                "expectedTurnId": "turn-server",
                "input": [{"type": "text", "text": "narrow the scope"}],
            },
        )

    asyncio.run(check())


def test_thread_params_use_configured_approval_and_sandbox(monkeypatch):
    # Isolate from the machine's backend selection and dispatcher config,
    # which otherwise pick the configured Claude model.
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "codex")
    client = CodexAppServerClient(
        ws_url="ws://example.invalid",
        cwd="/tmp",
        approval_policy="on-request",
        sandbox="read-only",
        dispatcher_config_path="/nonexistent-dispatcher-config.json",
    )

    assert client._thread_params() == {
        "cwd": "/tmp",
        "model": "gpt-5.5",
        "approvalPolicy": "on-request",
        "sandbox": "read-only",
    }


def test_thread_params_default_to_existing_voice_coder_behavior(monkeypatch):
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "codex")
    client = CodexAppServerClient(
        ws_url="ws://example.invalid",
        cwd="/tmp",
        dispatcher_config_path="/nonexistent-dispatcher-config.json",
    )

    assert client._thread_params() == {
        "cwd": "/tmp",
        "model": "gpt-5.5",
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
    }


def test_sandbox_policy_translates_for_turn_start():
    client = CodexAppServerClient(
        ws_url="ws://example.invalid",
        cwd="/tmp",
        sandbox="workspace-write",
    )

    assert client._sandbox_policy() == {"type": "workspaceWrite"}


def test_completed_turn_result_includes_livekit_speech_text():
    async def check():
        client = CodexAppServerClient(ws_url="ws://example.invalid", cwd="/tmp")
        turn = _ActiveTurn(
            turn_id="turn-1",
            completed=asyncio.get_running_loop().create_future(),
            prompt="do the thing",
            started_at=time.monotonic(),
        )
        client._active_turn = turn
        client._record_agent_message(turn.turn_id, "Yes, I'm here.")

        await client._handle_notification(
            "turn/completed",
            {"turn": {"id": turn.turn_id, "status": "completed"}},
        )

        assert turn.completed.result()["_livekit_speech_text"] == "Yes, I'm here."
        assert turn.completed.result()["_livekit_turn_id"] == turn.turn_id
        assert turn.delivered_text == "Yes, I'm here."
        assert client._active_turn is None

    asyncio.run(check())


def test_claim_speech_allows_one_livekit_stream_to_speak():
    client = CodexAppServerClient(ws_url="ws://example.invalid", cwd="/tmp")

    assert client.claim_speech("turn-1")
    assert not client.claim_speech("turn-1")
    client.release_speech_claim("turn-1")
    assert client.claim_speech("turn-1")


def test_persist_thread_id_writes_dispatcher_route_state(tmp_path: Path):
    state_path = tmp_path / "livekit-voice-route.json"
    client = CodexAppServerClient(
        ws_url="ws://example.invalid",
        cwd="/tmp",
        state_path=str(state_path),
    )

    client._thread_id = "dispatcher-1"
    client._persist_thread_id("dispatcher-1")

    route_payload = json.loads(
        (tmp_path / "livekit-voice-route.json").read_text(encoding="utf-8")
    )
    assert route_payload["dispatcher_thread_id"] == "dispatcher-1"
    assert (
        route_payload["dispatcher_voice_id"] == "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
    )
    assert route_payload["dispatcher_voice_name"] == "Jacqueline"
    assert route_payload["active_target_thread_id"] is None
    assert route_payload["active_target_voice_name"] is None
    assert route_payload["instruction_override_supported"] is True


def test_persist_thread_id_writes_configured_dispatcher_voice(tmp_path: Path):
    state_path = tmp_path / "livekit-voice-route.json"
    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(
        json.dumps(
            {
                "dispatcher_voice_id": "voice-thandi",
                "dispatcher_voice_name": "Thandi",
            }
        ),
        encoding="utf-8",
    )
    client = CodexAppServerClient(
        ws_url="ws://example.invalid",
        cwd="/tmp",
        state_path=str(state_path),
        dispatcher_config_path=config_path,
    )

    client._thread_id = "dispatcher-1"
    client._persist_thread_id("dispatcher-1")

    route_payload = json.loads(
        (tmp_path / "livekit-voice-route.json").read_text(encoding="utf-8")
    )
    assert route_payload["dispatcher_voice_id"] == "voice-thandi"
    assert route_payload["dispatcher_voice_name"] == "Thandi"


def test_ensure_thread_adopts_canonical_dispatcher_from_disk(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "codex")
    state_path = tmp_path / "livekit-voice-route.json"
    state_path.write_text(
        json.dumps({"dispatcher_thread_id": "stale-dispatcher"}), encoding="utf-8"
    )

    class RecordingClient(CodexAppServerClient):
        def __init__(self):
            super().__init__(
                ws_url="ws://example.invalid",
                cwd="/tmp",
                state_path=str(state_path),
                dispatcher_config_path="/nonexistent-dispatcher-config.json",
            )
            self.requests = []

        async def _ensure_connected_locked(self):
            return None

        async def _send_request_locked(self, method, params):
            self.requests.append((method, dict(params)))
            assert method == "thread/resume"
            return {"thread": {"id": params["threadId"]}}

    client = RecordingClient()
    client._thread_loaded = True
    state_path.write_text(
        json.dumps({"dispatcher_thread_id": "canonical-dispatcher"}),
        encoding="utf-8",
    )

    thread_id = asyncio.run(client._ensure_thread())

    assert thread_id == "canonical-dispatcher"
    assert client.requests == [
        (
            "thread/resume",
            {
                "cwd": "/tmp",
                "model": "gpt-5.5",
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "threadId": "canonical-dispatcher",
            },
        )
    ]
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["dispatcher_thread_id"]
        == "canonical-dispatcher"
    )


def test_ensure_thread_creates_one_canonical_dispatcher_state(tmp_path: Path):
    state_path = tmp_path / "livekit-voice-route.json"

    class StartClient(CodexAppServerClient):
        def __init__(self):
            super().__init__(
                ws_url="ws://example.invalid",
                cwd="/tmp",
                state_path=str(state_path),
            )
            self.requests = []

        async def _ensure_connected_locked(self):
            return None

        async def _send_request_locked(self, method, params):
            self.requests.append((method, dict(params)))
            assert method == "thread/start"
            return {"thread": {"id": "canonical-dispatcher"}}

    thread_id = asyncio.run(StartClient()._ensure_thread())

    assert thread_id == "canonical-dispatcher"
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["dispatcher_thread_id"]
        == "canonical-dispatcher"
    )
    route_payload = json.loads(
        (tmp_path / "livekit-voice-route.json").read_text(encoding="utf-8")
    )
    assert route_payload["dispatcher_thread_id"] == "canonical-dispatcher"


def test_ensure_thread_replaces_unresumable_canonical_dispatcher(tmp_path: Path):
    state_path = tmp_path / "livekit-voice-route.json"
    state_path.write_text(
        json.dumps({"dispatcher_thread_id": "missing-dispatcher"}),
        encoding="utf-8",
    )

    class FallbackClient(CodexAppServerClient):
        def __init__(self):
            super().__init__(
                ws_url="ws://example.invalid",
                cwd="/tmp",
                state_path=str(state_path),
            )
            self.requests = []

        async def _ensure_connected_locked(self):
            return None

        async def _send_request_locked(self, method, params):
            self.requests.append((method, dict(params)))
            if method == "thread/resume":
                raise RuntimeError("no rollout found for thread id missing-dispatcher")
            assert method == "thread/start"
            return {"thread": {"id": "replacement-dispatcher"}}

    client = FallbackClient()
    thread_id = asyncio.run(client._ensure_thread())

    assert thread_id == "replacement-dispatcher"
    assert [method for method, _params in client.requests] == [
        "thread/resume",
        "thread/start",
    ]
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["dispatcher_thread_id"]
        == "replacement-dispatcher"
    )


def test_recreated_dispatcher_resets_route_until_replacement_warms(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    state_path = tmp_path / "livekit-voice-route.json"
    state_path.write_text(
        json.dumps(
            {
                "dispatcher_thread_id": "dispatcher-1",
                "active_target_thread_id": "target-1",
                "active_target_kind": "codex_thread",
                "active_target_label": "Target",
                "active_target_voice_id": "voice-1",
                "active_target_voice_name": "Alice",
                "updated_at": 1,
            }
        ),
        encoding="utf-8",
    )

    class StartClient(CodexAppServerClient):
        def __init__(self):
            super().__init__(
                ws_url="ws://example.invalid",
                cwd="/tmp",
                state_path=str(state_path),
            )
            self.requests = []

        async def _ensure_connected_locked(self):
            return None

        async def _send_request_locked(self, method, params):
            self.requests.append((method, dict(params)))
            assert method == "thread/start"
            return {"thread": {"id": "dispatcher-2"}}

    prepare_livekit_dispatcher_recreation()
    preserved_state = get_livekit_voice_route_state()

    assert state_path.exists()
    assert preserved_state.dispatcher_thread_id is None
    assert preserved_state.active_route == "dispatcher"

    thread_id = asyncio.run(StartClient()._ensure_thread())
    replacement_state = get_livekit_voice_route_state()

    assert thread_id == "dispatcher-2"
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["dispatcher_thread_id"]
        == "dispatcher-2"
    )
    assert replacement_state.dispatcher_thread_id == "dispatcher-2"
    assert replacement_state.active_route == "dispatcher"
    assert replacement_state.active_target_thread_id is None


def test_initial_thread_id_is_resumed_with_developer_instructions(monkeypatch):
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "codex")
    client = CodexAppServerClient(
        ws_url="ws://example.invalid",
        cwd="/tmp/project",
        developer_instructions="direct LiveKit instructions",
        persist_thread=False,
        initial_thread_id="target-1",
        dispatcher_config_path="/nonexistent-dispatcher-config.json",
    )

    assert client._thread_id == "target-1"
    assert client._thread_params(thread_id="target-1") == {
        "cwd": "/tmp/project",
        "model": "gpt-5.5",
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
        "developerInstructions": "direct LiveKit instructions",
        "threadId": "target-1",
    }

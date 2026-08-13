from openbase_coder_cli.thread_sync.thread_payloads import _session_from_thread


def test_session_from_thread_maps_backend_session_id() -> None:
    session = _session_from_thread(
        {
            "threadId": "s_abc123",
            "name": "fix-things",
            "cwd": "/tmp/project",
            "backend": "claude_code",
            "backendSessionId": "44bc456e-3f2c-4130-bb68-55ef84ea6d55",
        },
        include_turns=False,
    )

    assert session.backend == "claude_code"
    assert session.backend_session_id == "44bc456e-3f2c-4130-bb68-55ef84ea6d55"
    payload = session.model_dump(mode="json")
    assert payload["backend"] == "claude_code"
    assert payload["backend_session_id"] == "44bc456e-3f2c-4130-bb68-55ef84ea6d55"


def test_session_from_thread_defaults_backend_session_id_to_none() -> None:
    session = _session_from_thread(
        {"threadId": "0199aaaa-bbbb-cccc-dddd-eeeeffff0000", "cwd": "/tmp/project"},
        include_turns=False,
    )

    assert session.backend_session_id is None


def test_session_from_thread_maps_model_and_reasoning_effort() -> None:
    session = _session_from_thread(
        {
            "threadId": "s_model",
            "cwd": "/tmp/project",
            "model": "gpt-5.5",
            "reasoningEffort": "high",
        },
        include_turns=False,
    )

    payload = session.model_dump(mode="json")
    assert payload["model"] == "gpt-5.5"
    assert payload["reasoning_effort"] == "high"


def test_session_from_thread_backfills_model_and_effort_from_newest_turn() -> None:
    session = _session_from_thread(
        {
            "threadId": "s_turns",
            "cwd": "/tmp/project",
            "turns": [
                {
                    "id": "t1",
                    "status": "completed",
                    "startedAt": "2026-08-01T10:00:00Z",
                    "completedAt": "2026-08-01T10:01:00Z",
                    "model": "sonnet",
                    "reasoningEffort": "low",
                },
                {
                    "id": "t2",
                    "status": "completed",
                    "startedAt": "2026-08-02T10:00:00Z",
                    "completedAt": "2026-08-02T10:01:00Z",
                    "model": "claude-fable-5",
                    "reasoningEffort": "high",
                },
            ],
        },
        include_turns=True,
    )

    assert session.model == "claude-fable-5"
    assert session.reasoning_effort == "high"
    assert session.run_history[-1].model == "claude-fable-5"
    payload = session.model_dump(mode="json")
    assert payload["turn_history"][-1]["model"] == "claude-fable-5"
    assert payload["turn_history"][-1]["reasoning_effort"] == "high"

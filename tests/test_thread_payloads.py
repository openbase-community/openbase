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

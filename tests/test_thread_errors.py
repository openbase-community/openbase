from openbase_coder_cli.openbase_coder_cli_app.thread_errors import (
    THREAD_DATA_UNAVAILABLE_CODE,
    is_thread_data_unavailable_error,
    thread_error_code,
    thread_error_message,
)


def test_unreadable_rollout_error_hides_local_paths() -> None:
    exc = RuntimeError(
        '{"code": -32603, "message": "failed to read thread: '
        "thread-store internal error: failed to read session metadata "
        "private-rollout-location: rollout at private-rollout-location "
        'does not start with session metadata"}'
    )

    assert is_thread_data_unavailable_error(exc) is True
    assert thread_error_code(exc, fallback="fallback") == THREAD_DATA_UNAVAILABLE_CODE
    message = thread_error_message(exc)
    assert "current Codex version" in message
    assert "private-rollout-location" not in message


def test_other_thread_errors_keep_their_message_and_fallback_code() -> None:
    exc = RuntimeError('{"code": -32000, "message": "Thread not found"}')

    assert is_thread_data_unavailable_error(exc) is False
    assert thread_error_message(exc) == "Thread not found"
    assert thread_error_code(exc, fallback="fallback") == "fallback"

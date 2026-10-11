"""turn_recovery: classification and the bounded retry loop."""

from __future__ import annotations

import pytest

from openbase_coder_cli.livekit_agent import turn_recovery as tr


@pytest.mark.parametrize(
    ("detail", "kind"),
    [
        ("Control request timeout: initialize", tr.KIND_START_TIMEOUT),
        (
            "RuntimeError: Codex app-server is not running or not reachable at unix:/x. "
            "Start the Openbase-managed codex-app-server service.",
            tr.KIND_BACKEND_DOWN,
        ),
        ("502 Bad Gateway from the model proxy", tr.KIND_MODEL_SERVICE),
        ("Monthly Openbase model proxy spend limit reached", tr.KIND_TERMINAL),
        (
            "This model is not available on your plan. Choose Claude Haiku.",
            tr.KIND_TERMINAL,
        ),
        ("something odd", tr.KIND_UNKNOWN),
    ],
)
def test_error_text_classification(detail, kind):
    assert tr.classify_error_text(detail).kind == kind


def test_timeouts_are_slow_backends():
    assert tr.classify_exception(TimeoutError("poll")).kind == tr.KIND_BACKEND_SLOW
    assert tr.classify_error_text("x").retryable
    assert not tr.classify_error_text("spend limit reached").retryable


def test_backend_error_kinds_win_over_text():
    failed = tr.failure_from_result(
        {
            "_livekit_turn_failed": True,
            "_livekit_turn_error": "Click Allow in the dialog on your Mac.",
            "_livekit_turn_error_kind": "blocked_permission_dialog",
            "_livekit_turn_id": "t1",
        }
    )
    assert failed is not None
    assert failed.kind == tr.KIND_TERMINAL
    assert failed.say == "Click Allow in the dialog on your Mac."
    assert (
        failed.spoken_line("Cooper", elapsed_seconds=3, attempts=1, kept=False)
        == "Click Allow in the dialog on your Mac."
    )
    down = tr.failure_from_result(
        {
            "_livekit_turn_failed": True,
            "_livekit_turn_error": "",
            "_livekit_turn_error_kind": "backend_unavailable",
        }
    )
    assert down is not None and down.kind == tr.KIND_BACKEND_DOWN and down.say is None


def test_auth_failures_and_successes_are_not_recovery_cases():
    assert tr.failure_from_result({"status": "completed"}) is None
    assert (
        tr.failure_from_result(
            {"_livekit_turn_failed": True, "_livekit_backend_auth_failure": True}
        )
        is None
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_retries_until_a_result_is_not_a_failure():
    clock = _Clock()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds

    attempts: list[int] = []

    async def run(attempt: int) -> dict:
        attempts.append(attempt)
        if attempt == 0:
            raise RuntimeError("Codex app-server is not running or not reachable")
        if attempt == 1:
            return {"_livekit_turn_failed": True, "_livekit_turn_error": "timeout"}
        return {"status": "completed"}

    retries: list[tuple[str, int, float]] = []
    result = await tr.run_with_recovery(
        run,
        should_continue=lambda: True,
        window_seconds=60,
        backoff_seconds=(3, 6, 12),
        on_retry=lambda failure, n, delay: retries.append((failure.kind, n, delay)),
        clock=clock,
        sleep=sleep,
    )
    assert result == {"status": "completed"}
    assert attempts == [0, 1, 2]
    assert slept == [3, 6]
    assert retries == [(tr.KIND_BACKEND_DOWN, 1, 3), (tr.KIND_BACKEND_SLOW, 2, 6)]


async def test_gives_up_when_the_window_closes_or_the_caller_moves_on():
    clock = _Clock()

    async def sleep(seconds: float) -> None:
        clock.now += seconds

    async def always_fails(attempt: int) -> dict:
        return {
            "_livekit_turn_failed": True,
            "_livekit_turn_error": "Control request timeout: initialize",
        }

    with pytest.raises(tr.TurnRecoveryFailed) as exhausted:
        await tr.run_with_recovery(
            always_fails,
            should_continue=lambda: True,
            window_seconds=10,
            backoff_seconds=(4,),
            clock=clock,
            sleep=sleep,
        )
    assert exhausted.value.reason == "window_exhausted"
    assert (
        exhausted.value.attempts == 3
    )  # 0 s, 4 s, 8 s: a third backoff would pass 10 s
    assert exhausted.value.failure.kind == tr.KIND_START_TIMEOUT

    with pytest.raises(tr.TurnRecoveryFailed) as moved_on:
        await tr.run_with_recovery(
            always_fails,
            should_continue=lambda: False,
            window_seconds=10,
            backoff_seconds=(1,),
            clock=clock,
            sleep=sleep,
        )
    assert moved_on.value.reason == "caller_moved_on"
    assert moved_on.value.attempts == 1


async def test_terminal_failures_are_not_retried():
    calls = 0

    async def refused(attempt: int) -> dict:
        nonlocal calls
        calls += 1
        raise RuntimeError("This model is not available on your plan.")

    with pytest.raises(tr.TurnRecoveryFailed) as terminal:
        await tr.run_with_recovery(
            refused,
            should_continue=lambda: True,
            window_seconds=60,
            backoff_seconds=(1,),
        )
    assert terminal.value.reason == "terminal"
    assert calls == 1


def test_spoken_lines_are_specific_and_mention_the_kept_request():
    failure = tr.TurnFailure(kind=tr.KIND_START_TIMEOUT, detail="x")
    line = failure.spoken_line("Cooper", elapsed_seconds=71.2, attempts=2, kept=True)
    assert line.startswith("Cooper couldn't start on your computer")
    assert "2 times in a row" in line
    assert line.endswith(
        "I kept your request in Cooper's thread, so it runs once the backend is back."
    )
    unknown = tr.TurnFailure(kind=tr.KIND_UNKNOWN, detail="RuntimeError: boom")
    assert "boom" not in unknown.spoken_line(
        "Cooper", elapsed_seconds=1, attempts=1, kept=False
    )

"""Watchdog/self-heal behaviors: zombie agent workers, poisoned Django state,
and AssemblyAI idle-warning noise filtering."""

from __future__ import annotations

import logging

import pytest

from openbase_coder_cli.config import self_heal as self_heal_module
from openbase_coder_cli.config.self_heal import (
    IMPORT_FAILURE_THRESHOLD,
    ImportFailureSelfHeal,
    wrap_asgi_application,
)
from openbase_coder_cli.livekit_agent import worker_watchdog as watchdog_module
from openbase_coder_cli.livekit_agent.stt_log_noise import (
    AssemblyAiIdleNoiseFilter,
)
from openbase_coder_cli.livekit_agent.worker_watchdog import (
    WORKER_EXIT_RATE_LIMIT_THRESHOLD,
    WORKER_INIT_FAILURE_THRESHOLD,
    WORKER_INIT_FAILURE_WINDOW_SECONDS,
    WorkerFailureWatchdog,
)


def _log_record(message: str, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord(
        name="livekit.agents",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_worker_watchdog_exits_after_repeated_init_failures(monkeypatch):
    exits: list[bool] = []
    monkeypatch.setattr(
        WorkerFailureWatchdog,
        "_initiate_exit",
        lambda self: exits.append(True),
    )
    watchdog = WorkerFailureWatchdog()

    for _ in range(WORKER_INIT_FAILURE_THRESHOLD - 1):
        watchdog.emit(_log_record("error initializing process"))
    assert exits == []

    watchdog.emit(_log_record("error initializing process"))
    assert exits == [True]

    # Further failures do not re-trigger the exit.
    watchdog.emit(_log_record("error initializing process"))
    assert exits == [True]


def test_worker_watchdog_ignores_other_errors_and_old_failures(monkeypatch):
    exits: list[bool] = []
    monkeypatch.setattr(
        WorkerFailureWatchdog,
        "_initiate_exit",
        lambda self: exits.append(True),
    )
    clock = {"now": 1000.0}
    monkeypatch.setattr(watchdog_module.time, "monotonic", lambda: clock["now"])
    watchdog = WorkerFailureWatchdog()

    watchdog.emit(_log_record("process exited with non-zero exit code 1"))
    watchdog.emit(_log_record("some unrelated error"))
    assert exits == []

    # Failures spread wider than the window never accumulate to the threshold.
    for _ in range(WORKER_INIT_FAILURE_THRESHOLD * 2):
        watchdog.emit(_log_record("error initializing process"))
        clock["now"] += WORKER_INIT_FAILURE_WINDOW_SECONDS + 1
    assert exits == []


def test_import_failure_self_heal_exits_on_repeated_import_errors(monkeypatch):
    exits: list[bool] = []
    monkeypatch.setattr(
        ImportFailureSelfHeal,
        "_initiate_exit",
        lambda self: exits.append(True),
    )
    self_heal = ImportFailureSelfHeal()

    try:
        raise RuntimeError("wrapper") from ImportError("cannot import name 'urls'")
    except RuntimeError as exc:
        chained = exc

    for _ in range(IMPORT_FAILURE_THRESHOLD - 1):
        self_heal.record_exception(chained)
    assert exits == []

    self_heal.record_exception(ImportError("cannot import name 'x'"))
    assert exits == [True]

    self_heal.record_exception(ImportError("again"))
    assert exits == [True]


def test_import_failure_self_heal_ignores_ordinary_errors(monkeypatch):
    exits: list[bool] = []
    monkeypatch.setattr(
        ImportFailureSelfHeal,
        "_initiate_exit",
        lambda self: exits.append(True),
    )
    self_heal = ImportFailureSelfHeal()

    for _ in range(IMPORT_FAILURE_THRESHOLD * 2):
        self_heal.record_exception(ValueError("bad request payload"))
    assert exits == []


@pytest.mark.asyncio
async def test_wrap_asgi_application_records_and_reraises(monkeypatch):
    monkeypatch.setattr(self_heal_module, "is_standalone_runtime", lambda: False)
    exits: list[bool] = []
    monkeypatch.setattr(
        ImportFailureSelfHeal,
        "_initiate_exit",
        lambda self: exits.append(True),
    )

    async def broken_app(scope, receive, send):
        raise ImportError("cannot import name 'urls' from 'django.conf'")

    wrapped = wrap_asgi_application(broken_app)
    for _ in range(IMPORT_FAILURE_THRESHOLD):
        with pytest.raises(ImportError):
            await wrapped({"type": "http"}, None, None)
    assert exits == [True]


def test_wrap_asgi_application_is_passthrough_for_standalone(monkeypatch):
    monkeypatch.setattr(self_heal_module, "is_standalone_runtime", lambda: True)

    async def app(scope, receive, send):
        return None

    assert wrap_asgi_application(app) is app


def _assemblyai_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="livekit.plugins.assemblyai",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_idle_noise_filter_keeps_first_warning_and_drops_escalations():
    noise_filter = AssemblyAiIdleNoiseFilter()

    assert noise_filter.filter(
        _assemblyai_record("AssemblyAI no messages received for 15s session=abc")
    )
    assert not noise_filter.filter(
        _assemblyai_record("AssemblyAI no messages received for 30s session=abc")
    )
    assert not noise_filter.filter(
        _assemblyai_record("AssemblyAI no messages received for 165s session=abc")
    )
    # Unrelated plugin warnings always pass.
    assert noise_filter.filter(_assemblyai_record("websocket reconnecting"))


def test_worker_watchdog_exits_immediately_when_connection_task_dies(monkeypatch):
    exits: list[bool] = []
    monkeypatch.setattr(
        WorkerFailureWatchdog,
        "_initiate_exit",
        lambda self: exits.append(True),
    )
    watchdog = WorkerFailureWatchdog()

    watchdog.emit(_log_record("Error in _connection_task"))
    assert exits == [True]

    # The exit fires once even if the message repeats.
    watchdog.emit(_log_record("Error in _connection_task"))
    assert exits == [True]


def test_worker_watchdog_exits_when_agent_session_closes_unrecoverably(monkeypatch):
    exits: list[bool] = []
    monkeypatch.setattr(
        WorkerFailureWatchdog,
        "_initiate_exit",
        lambda self: exits.append(True),
    )
    watchdog = WorkerFailureWatchdog()

    watchdog.emit(
        _log_record(
            "AgentSession is closing due to unrecoverable error",
            level=logging.CRITICAL,
        )
    )
    assert exits == [True]

    # The exit fires once even if both unrecoverable signatures are logged.
    watchdog.emit(_log_record("Error in _connection_task"))
    assert exits == [True]


def test_worker_watchdog_rate_limits_agent_session_restart_churn(
    monkeypatch, tmp_path
):
    exits: list[bool] = []
    monkeypatch.setattr(
        WorkerFailureWatchdog,
        "_initiate_exit",
        lambda self: exits.append(True),
    )
    monkeypatch.setattr(
        watchdog_module,
        "_EXIT_RATE_LIMIT_STATE_PATH",
        tmp_path / "livekit-agent-watchdog-exits.json",
    )
    clock = {"now": 1000.0}
    monkeypatch.setattr(watchdog_module.time, "time", lambda: clock["now"])

    for _ in range(WORKER_EXIT_RATE_LIMIT_THRESHOLD):
        WorkerFailureWatchdog().emit(
            _log_record(
                "AgentSession is closing due to unrecoverable error",
                level=logging.CRITICAL,
            )
        )
    assert len(exits) == WORKER_EXIT_RATE_LIMIT_THRESHOLD

    WorkerFailureWatchdog().emit(
        _log_record(
            "AgentSession is closing due to unrecoverable error",
            level=logging.CRITICAL,
        )
    )
    assert len(exits) == WORKER_EXIT_RATE_LIMIT_THRESHOLD

    clock["now"] += watchdog_module.WORKER_EXIT_RATE_LIMIT_WINDOW_SECONDS + 1
    WorkerFailureWatchdog().emit(
        _log_record(
            "AgentSession is closing due to unrecoverable error",
            level=logging.CRITICAL,
        )
    )
    assert len(exits) == WORKER_EXIT_RATE_LIMIT_THRESHOLD + 1

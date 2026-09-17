import asyncio
from types import SimpleNamespace

import pytest
from livekit.agents import APIConnectionError, stt
from livekit.agents.voice.events import ErrorEvent, CloseEvent, CloseReason
from openbase_coder_cli.livekit_agent.session_diagnostics import _register_session_diagnostics


@pytest.mark.asyncio
async def test_real_sdk_provider_error_is_reported_only_when_terminal():
    handlers = {}
    session = SimpleNamespace(on=lambda name, handler: handlers.__setitem__(name, handler))
    reports = []
    async def report(error):
        reports.append(error)
    _register_session_diagnostics(session, SimpleNamespace(delivery_ledger=None),
        enable_logging=False, on_unrecoverable_error=report)
    underlying = APIConnectionError("controlled recognition connection loss")
    transient = stt.STTError(timestamp=1, label="recognition", error=underlying, recoverable=True)
    handlers["error"](ErrorEvent(error=transient, source=None))
    await asyncio.sleep(0)
    assert not reports
    terminal = stt.STTError(timestamp=2, label="recognition", error=underlying, recoverable=False)
    handlers["error"](ErrorEvent(error=terminal, source=None))
    handlers["close"](CloseEvent(error=terminal, reason=CloseReason.ERROR))
    await asyncio.sleep(0)
    assert reports == [underlying]


def test_close_reason_is_retained_with_verbose_logging_disabled(caplog):
    handlers = {}
    session = SimpleNamespace(on=lambda name, handler: handlers.__setitem__(name, handler))
    _register_session_diagnostics(session, SimpleNamespace(delivery_ledger=None), enable_logging=False)
    with caplog.at_level("INFO"):
        handlers["close"](CloseEvent(reason=CloseReason.JOB_SHUTDOWN))
    assert "stage=session_close" in caplog.text
    assert "JOB_SHUTDOWN" in caplog.text or "job_shutdown" in caplog.text

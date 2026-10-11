"""Preconnected GPT-Live sessions handed to the framework on first use."""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def wait_live_session_started(session: Any, *, timeout: float) -> None:
    """Wait for the gateway acknowledgement, or surface a failed connection."""
    started = getattr(session, "_session_started_fut", None)
    connection = getattr(session, "_main_atask", None)
    if started is None or connection is None:
        return
    done, _pending = await asyncio.wait(
        (started, connection), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
    )
    if connection in done:
        await connection
        raise RuntimeError("GPT-Live connection closed before startup completed")
    if started not in done:
        raise TimeoutError("GPT-Live did not acknowledge session startup")
    if started.cancelled():
        raise RuntimeError("GPT-Live session closed during startup")
    started.result()


_preconnecting_model_classes: dict[type, type] = {}


def _preconnecting_model_class(base: type) -> type:
    """``base`` (the GPT-Live model class) with a ``preconnect`` step.

    The plugin opens the gateway websocket when a session object is created,
    which normally happens inside ``AgentSession.start``, after the room has
    connected. ``preconnect`` creates that session earlier so the connection
    overlaps the room connect and the start route; ``session`` then hands the
    open session to the framework once. A preconnected session that already
    failed (its connection task ended, or it reported an error before the
    framework attached its handlers) is dropped and a fresh one is created,
    so the error path is the same as without preconnect.
    """
    cls = _preconnecting_model_classes.get(base)
    if cls is not None:
        return cls

    class PreconnectingLiveModel(base):  # type: ignore[misc, valid-type]
        _pending_session: Any = None
        _pending_errors: list = []

        def preconnect(self) -> Any:
            if self._pending_session is not None:
                return self._pending_session
            create = getattr(super(), "session", None)
            if create is None:
                return None
            session = create()
            errors: list = []
            on = getattr(session, "on", None)
            if callable(on):
                on("error", errors.append)
            self._pending_session = session
            self._pending_errors = errors
            return session

        def session(self) -> Any:
            session, self._pending_session = self._pending_session, None
            errors, self._pending_errors = self._pending_errors, []
            if session is None:
                return super().session()
            if errors or not _live_session_is_connecting(session):
                logger.warning(
                    "dispatch_timing stage=live_session_preconnect_discarded errors=%d",
                    len(errors),
                )
                _schedule_live_session_close(session)
                return super().session()
            return session

        async def discard_preconnected(self) -> None:
            session, self._pending_session = self._pending_session, None
            self._pending_errors = []
            if session is not None:
                await _close_live_session(session)

    PreconnectingLiveModel.__name__ = f"Preconnecting{base.__name__}"
    _preconnecting_model_classes[base] = PreconnectingLiveModel
    return PreconnectingLiveModel


def _live_session_is_connecting(session: Any) -> bool:
    task = getattr(session, "_main_atask", None)
    return task is None or not task.done()


async def _close_live_session(session: Any) -> None:
    aclose = getattr(session, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        logger.debug("preconnected live session close failed", exc_info=True)


def _schedule_live_session_close(session: Any) -> None:
    try:
        asyncio.get_running_loop().create_task(_close_live_session(session))
    except RuntimeError:
        logger.debug("no running loop to close the preconnected live session")
